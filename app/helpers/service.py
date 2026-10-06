import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import func, select

from app.applications.models import Badge, BusinessAudit, NotificationOutbox
from app.applications.workflow import (
    active_event,
    get_application,
    utc,
    verified_snapshot,
)
from app.badges.service import allocate
from app.config import settings
from app.creators.policy import (
    authorize,
    change_allowed,
    check_version,
    require_active,
)
from app.helpers.models import HelperInvitation, HelperRegistration, RedemptionThrottle
from app.identity.models import LocalUser


def record_change(
    db, actor_id, application, entity, entity_id, action, changes, reason=""
):
    db.add(
        BusinessAudit(
            actor_id=actor_id,
            event_id=application.event_id,
            entity=entity,
            entity_id=entity_id,
            action=action,
            changes=changes,
            reason=reason.strip(),
        )
    )


def notify(db, application, recipient_id, kind):
    db.add(
        NotificationOutbox(
            event_id=application.event_id,
            application_id=application.id,
            application_version=application.version,
            recipient_id=recipient_id,
            notification_type=kind,
        )
    )


def touch(application):
    application.version += 1
    application.updated_at = datetime.now(UTC)


def check_evidence(event, evidence):
    if (event.id, event.year) != (evidence[0].event_id, evidence[0].event_year):
        raise HTTPException(409, "The event changed. Retry verification.")


def snapshot(record, evidence):
    _, result, email = evidence
    record.reg_id, record.nickname, record.email = result.reg_id, result.nickname, email
    record.eligibility_checked_at = datetime.now(UTC)


def helper_record(db, helper_id, event_id):
    helper = db.scalar(
        select(HelperRegistration).where(
            HelperRegistration.id == helper_id, HelperRegistration.event_id == event_id
        )
    )
    if helper is None:
        raise HTTPException(404, "Helper relationship not found")
    return helper


def capacity(db, event, application_id, *, excluding=None):
    query = (
        select(func.count())
        .select_from(HelperRegistration)
        .where(
            HelperRegistration.application_id == application_id,
            HelperRegistration.status.in_(("PENDING", "CONFIRMED")),
            HelperRegistration.withdrawn_at.is_(None),
        )
    )
    if excluding is not None:
        query = query.where(HelperRegistration.id != excluding)
    if event.helper_limit is not None and db.scalar(query) >= event.helper_limit:
        raise HTTPException(409, "The creator's helper limit has been reached.")


async def invite(
    db,
    actor_id,
    application_id,
    registration,
    identity,
    *,
    replace_id=None,
    administrative=False,
    exceptional=False,
    reason="",
):
    event = active_event(db)
    application = get_application(db, application_id, event.id)
    authorize(
        db, actor_id, application.user_id, administrative=administrative, reason=reason
    )
    require_active(application)
    creator_id = application.user_id
    evidence = await verified_snapshot(db, creator_id, registration, identity)
    secret = secrets.token_urlsafe(32)
    with db.begin():
        event = active_event(db, lock=True)
        application = get_application(db, application_id, event.id)
        authorize(
            db,
            actor_id,
            application.user_id,
            administrative=administrative,
            reason=reason,
        )
        require_active(application)
        change_allowed(event, administrative=administrative, exceptional=exceptional)
        check_evidence(event, evidence)
        now = datetime.now(UTC)
        # Even an administrator cannot create a redeemable invitation after its absolute cap.
        if now >= utc(event.badge_change_deadline_at):
            raise HTTPException(
                409, "Invitations cannot outlive the badge change deadline."
            )
        if replace_id is not None:
            old = db.scalar(
                select(HelperInvitation).where(
                    HelperInvitation.id == replace_id,
                    HelperInvitation.application_id == application_id,
                )
            )
            if old is None or old.consumed_at is not None or old.revoked_at is not None:
                raise HTTPException(
                    409, "Only an unused, unrevoked invitation may be replaced."
                )
            old.revoked_at = now
            record_change(
                db,
                actor_id,
                application,
                "invitation",
                old.id,
                "invitation_replaced",
                {},
                reason,
            )
        invitation = HelperInvitation(
            application_id=application_id,
            event_id=event.id,
            token_digest=hashlib.sha256(secret.encode()).hexdigest(),
            expires_at=min(
                now + timedelta(days=7), utc(event.badge_change_deadline_at)
            ),
        )
        db.add(invitation)
        db.flush()
        record_change(
            db,
            actor_id,
            application,
            "invitation",
            invitation.id,
            "invitation_created",
            {},
            reason,
        )
        return invitation.id, secret


def revoke(db, actor_id, invitation_id):
    db.rollback()
    with db.begin():
        event = active_event(db, lock=True)
        invitation = db.scalar(
            select(HelperInvitation).where(
                HelperInvitation.id == invitation_id,
                HelperInvitation.event_id == event.id,
            )
        )
        if invitation is None:
            raise HTTPException(404, "Invitation not found")
        application = get_application(db, invitation.application_id, event.id)
        authorize(db, actor_id, application.user_id)
        if invitation.consumed_at:
            raise HTTPException(409, "Invitation is already used.")
        if invitation.revoked_at is None:
            invitation.revoked_at = datetime.now(UTC)
            record_change(
                db,
                actor_id,
                application,
                "invitation",
                invitation.id,
                "invitation_revoked",
                {},
            )


def throttle(db, user_id):
    db.rollback()
    with db.begin():
        db.scalar(select(LocalUser).where(LocalUser.id == user_id).with_for_update())
        now = datetime.now(UTC)
        entry = db.get(RedemptionThrottle, user_id)
        if entry is None:
            entry = RedemptionThrottle(user_id=user_id, window_start=now, attempts=0)
            db.add(entry)
        elif now >= utc(entry.window_start) + timedelta(minutes=1):
            entry.window_start, entry.attempts = now, 0
        entry.attempts += 1
        blocked = entry.attempts > settings.invitation_attempts_per_minute
    if blocked:
        raise HTTPException(
            429,
            "Too many invitation attempts. Retry in a minute.",
            headers={"Retry-After": "60"},
        )


def valid_invitation(db, secret, event_id):
    if not isinstance(secret, str) or len(secret) != 43:
        raise HTTPException(409, "Invitation is invalid, expired or already used.")
    digest = hashlib.sha256(secret.encode()).hexdigest()
    invitation = db.scalar(
        select(HelperInvitation).where(
            HelperInvitation.token_digest == digest,
            HelperInvitation.event_id == event_id,
        )
    )
    if (
        invitation is None
        or not secrets.compare_digest(invitation.token_digest, digest)
        or invitation.revoked_at
        or invitation.consumed_at
        or datetime.now(UTC) >= utc(invitation.expires_at)
    ):
        raise HTTPException(409, "Invitation is invalid, expired or already used.")
    return invitation


async def redeem(db, user_id, secret, registration, identity):
    throttle(db, user_id)
    event = active_event(db)
    invitation = valid_invitation(db, secret, event.id)
    application = get_application(db, invitation.application_id, event.id)
    require_active(application)
    change_allowed(event)
    if application.user_id == user_id:
        raise HTTPException(409, "A creator cannot be their own helper.")
    evidence = await verified_snapshot(db, user_id, registration, identity)
    with db.begin():
        event = active_event(db, lock=True)
        check_evidence(event, evidence)
        invitation = valid_invitation(db, secret, event.id)
        application = get_application(db, invitation.application_id, event.id)
        require_active(application)
        change_allowed(event)
        if application.user_id == user_id:
            raise HTTPException(409, "A creator cannot be their own helper.")
        if db.scalar(
            select(HelperRegistration.id).where(
                HelperRegistration.application_id == application.id,
                HelperRegistration.user_id == user_id,
            )
        ):
            raise HTTPException(
                409, "You already have a relationship with this creator."
            )
        capacity(db, event, application.id)
        invitation.consumed_at, invitation.consumed_by = datetime.now(UTC), user_id
        helper = HelperRegistration(
            application_id=application.id,
            event_id=event.id,
            creator_user_id=application.user_id,
            user_id=user_id,
            invitation_id=invitation.id,
        )
        snapshot(helper, evidence)
        db.add(helper)
        db.flush()
        touch(application)
        record_change(
            db,
            user_id,
            application,
            "helper",
            helper.id,
            "helper_registered",
            {"status": "PENDING"},
        )
        notify(db, application, application.user_id, "HELPER_REQUESTED")
        return helper.id


async def decide(
    db,
    actor_id,
    helper_id,
    version,
    target,
    registration,
    identity,
    *,
    administrative=False,
    exceptional=False,
    reason="",
):
    event = active_event(db)
    helper = helper_record(db, helper_id, event.id)
    application = get_application(db, helper.application_id, event.id)
    authorize(
        db, actor_id, application.user_id, administrative=administrative, reason=reason
    )
    user_id = helper.user_id
    check_version(helper, version)
    evidence = (
        await verified_snapshot(db, user_id, registration, identity)
        if target == "CONFIRMED"
        else None
    )
    db.rollback()
    with db.begin():
        event = active_event(db, lock=True)
        helper = helper_record(db, helper_id, event.id)
        application = get_application(db, helper.application_id, event.id)
        authorize(
            db,
            actor_id,
            application.user_id,
            administrative=administrative,
            reason=reason,
        )
        require_active(application)
        change_allowed(event, administrative=administrative, exceptional=exceptional)
        check_version(helper, version)
        badge = db.scalar(select(Badge).where(Badge.helper_id == helper.id))
        allowed = {
            "PENDING": ("CONFIRMED", "DECLINED"),
            "CONFIRMED": ("DECLINED",),
            "DECLINED": ("CONFIRMED",) if badge else (),
        }
        if helper.withdrawn_at or target not in allowed[helper.status]:
            raise HTTPException(409, "This helper transition is not allowed.")
        if evidence:
            check_evidence(event, evidence)
            capacity(db, event, application.id, excluding=helper.id)
            snapshot(helper, evidence)
        previous = helper.status
        helper.status, helper.version = target, helper.version + 1
        touch(application)
        if target == "CONFIRMED":
            allocate(db, event.id, actor_id, helper_id=helper.id)
        record_change(
            db,
            actor_id,
            application,
            "helper",
            helper.id,
            "helper_decided",
            {"before": previous, "after": target, "exceptional": exceptional},
            reason,
        )
        notify(db, application, helper.user_id, "HELPER_" + target)


def creator_activity_changed(db, actor_id, application, active, reason=""):
    if not active:
        for invitation in db.scalars(
            select(HelperInvitation).where(
                HelperInvitation.application_id == application.id,
                HelperInvitation.consumed_at.is_(None),
                HelperInvitation.revoked_at.is_(None),
            )
        ):
            invitation.revoked_at = datetime.now(UTC)
            record_change(
                db,
                actor_id,
                application,
                "invitation",
                invitation.id,
                "invitation_revoked",
                {},
                reason,
            )
    for helper in db.scalars(
        select(HelperRegistration).where(
            HelperRegistration.application_id == application.id,
            HelperRegistration.withdrawn_at.is_(None),
            HelperRegistration.status.in_(("PENDING", "CONFIRMED")),
        )
    ):
        helper.version += 1
        record_change(
            db,
            actor_id,
            application,
            "helper",
            helper.id,
            "creator_participation_changed",
            {"creator_active": active},
            reason,
        )
        notify(
            db,
            application,
            helper.user_id,
            "CREATOR_RESTORED" if active else "CREATOR_INACTIVE",
        )


async def withdraw(
    db,
    actor_id,
    entity_id,
    version,
    registration,
    identity,
    *,
    helper=False,
    restore=False,
    administrative=False,
    exceptional=False,
    reason="",
):
    event = active_event(db)
    record = (
        helper_record(db, entity_id, event.id)
        if helper
        else get_application(db, entity_id, event.id)
    )
    authorize(
        db, actor_id, record.user_id, administrative=administrative, reason=reason
    )
    if restore and not administrative:
        raise HTTPException(403, "Only an administrator may restore participation.")
    check_version(record, version)
    evidence = (
        await verified_snapshot(db, record.user_id, registration, identity)
        if restore
        else None
    )
    db.rollback()
    with db.begin():
        event = active_event(db, lock=True)
        record = (
            helper_record(db, entity_id, event.id)
            if helper
            else get_application(db, entity_id, event.id)
        )
        application = (
            get_application(db, record.application_id, event.id) if helper else record
        )
        authorize(
            db, actor_id, record.user_id, administrative=administrative, reason=reason
        )
        change_allowed(event, administrative=administrative, exceptional=exceptional)
        check_version(record, version)
        if (record.withdrawn_at is None) == restore:
            raise HTTPException(409, "Participation is already in the requested state.")
        if evidence:
            check_evidence(event, evidence)
            if helper and record.status in ("PENDING", "CONFIRMED"):
                capacity(db, event, application.id, excluding=record.id)
            snapshot(record, evidence)
        record.withdrawn_at = None if restore else datetime.now(UTC)
        if helper:
            record.version += 1
        touch(application)
        record_change(
            db,
            actor_id,
            application,
            "helper" if helper else "application",
            record.id,
            "restored" if restore else "withdrawn",
            {"exceptional": exceptional},
            reason,
        )
        if helper:
            notify(
                db,
                application,
                record.user_id,
                "HELPER_RESTORED" if restore else "HELPER_WITHDRAWN",
            )
        else:
            creator_activity_changed(
                db,
                actor_id,
                application,
                application.status == "APPROVED" and restore,
                reason,
            )
