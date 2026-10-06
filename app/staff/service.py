from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import and_, or_, select

from app.applications.models import (
    Badge,
    BusinessAudit,
    CreatorApplication,
    LocalRoleAssignment,
)
from app.applications.security import require_admin
from app.creators.models import CreatorProfile
from app.creators.policy import creator_active, helper_active
from app.events.models import Event
from app.helpers.models import HelperRegistration


def require_staff(db, actor_id, event_id, *, lock=False):
    query = select(LocalRoleAssignment).where(
        LocalRoleAssignment.user_id == actor_id,
        or_(
            and_(
                LocalRoleAssignment.role == "ADMIN",
                LocalRoleAssignment.event_id.is_(None),
            ),
            and_(
                LocalRoleAssignment.role == "BADGE_STAFF",
                LocalRoleAssignment.event_id == event_id,
            ),
        ),
    )
    if lock:
        query = query.with_for_update(read=True)
    if db.scalar(query) is None:
        raise HTTPException(403, "Badge staff access for this event is required")


def event_record(db, event_id, *, lock=False):
    event = db.get(Event, event_id, with_for_update=lock, populate_existing=True)
    if event is None:
        raise HTTPException(404, "Event not found")
    return event


def badge_context(db, badge):
    helper = db.get(HelperRegistration, badge.helper_id) if badge.helper_id else None
    application = db.get(
        CreatorApplication, helper.application_id if helper else badge.application_id
    )
    profile = db.get(CreatorProfile, application.id)
    return application, helper, profile


def lookup(db, actor_id, event_id, reg_id):
    require_staff(db, actor_id, event_id)
    event_record(db, event_id)
    reg_id = reg_id.strip()
    if not reg_id or len(reg_id) > 200:
        raise HTTPException(422, "Enter a Reg-ID of 1–200 characters")
    creator_ids = select(CreatorApplication.id).where(
        CreatorApplication.event_id == event_id, CreatorApplication.reg_id == reg_id
    )
    helper_ids = select(HelperRegistration.id).where(
        HelperRegistration.event_id == event_id, HelperRegistration.reg_id == reg_id
    )
    badges = db.scalars(
        select(Badge)
        .where(
            Badge.event_id == event_id,
            or_(Badge.application_id.in_(creator_ids), Badge.helper_id.in_(helper_ids)),
        )
        .order_by(Badge.badge_number)
    )
    rows = []
    for badge in badges:
        application, helper, profile = badge_context(db, badge)
        rows.append(
            {
                "id": badge.id,
                "number": badge.badge_number,
                "kind": "Helper" if helper else "Creator",
                "creator": profile.channel_name if profile else "",
                "application_id": application.id,
                "active": helper_active(helper, application)
                if helper
                else creator_active(application),
                "picked_up_at": badge.picked_up_at,
            }
        )
    return rows


def pickup(db, actor_id, event_id, badge_id, *, undo=False, reason=""):
    db.rollback()
    with db.begin():
        require_staff(db, actor_id, event_id, lock=True)
        if undo:
            require_admin(db, actor_id, lock=True)
            if not reason.strip() or len(reason) > 4000:
                raise HTTPException(
                    422, "Pickup correction requires a reason (maximum 4000 characters)"
                )
        event_record(db, event_id, lock=True)
        badge = db.scalar(
            select(Badge)
            .where(Badge.id == badge_id, Badge.event_id == event_id)
            .with_for_update()
        )
        if badge is None:
            raise HTTPException(404, "Badge not found")
        application, helper, _ = badge_context(db, badge)
        if not undo and not (
            helper_active(helper, application)
            if helper
            else creator_active(application)
        ):
            raise HTTPException(409, "This badge is inactive and cannot be collected")
        if (badge.picked_up_at is None) == undo:
            return
        before = badge.picked_up_at.isoformat() if badge.picked_up_at else None
        badge.picked_up_at = None if undo else datetime.now(UTC)
        badge.picked_up_by = None if undo else actor_id
        db.add(
            BusinessAudit(
                actor_id=actor_id,
                event_id=event_id,
                entity="badge",
                entity_id=badge.id,
                action="pickup_corrected" if undo else "picked_up",
                reason=reason.strip() if undo else "",
                changes={
                    "before": before,
                    "after": badge.picked_up_at.isoformat()
                    if badge.picked_up_at
                    else None,
                },
            )
        )
