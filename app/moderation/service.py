from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.applications.input import normalize_channel
from app.applications.models import BusinessAudit, CreatorChannel
from app.applications.security import require_admin
from app.config import settings
from app.creators.models import BannedChannel
from app.events.models import Event


def warnings(db, application_id):
    return db.scalars(
        select(BannedChannel)
        .join(
            CreatorChannel,
            (BannedChannel.platform == CreatorChannel.platform)
            & (BannedChannel.normalized_account == CreatorChannel.normalized_account),
        )
        .where(
            CreatorChannel.application_id == application_id,
            BannedChannel.active.is_(True),
        )
        .order_by(BannedChannel.id)
    ).all()


def save(db, actor_id, *, ban_id=None, platform, account, reason, active=True):
    require_admin(db, actor_id)
    if not reason.strip() or len(reason) > 4000:
        raise HTTPException(422, "A private reason of 1–4000 characters is required")
    try:
        channel = normalize_channel(platform, account, False)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    db.rollback()
    try:
        with db.begin():
            require_admin(db, actor_id, lock=True)
            row = (
                db.get(BannedChannel, ban_id, with_for_update=True) if ban_id else None
            )
            if ban_id and row is None:
                raise HTTPException(404, "Ban not found")
            before = (
                {
                    "active": row.active,
                    "platform": row.platform,
                    "account": row.normalized_account,
                }
                if row
                else {}
            )
            if row is None:
                row = BannedChannel(created_at=datetime.now(UTC))
                db.add(row)
            row.platform, row.normalized_account = (
                channel.platform,
                channel.normalized_account,
            )
            row.original_reference, row.private_reason = account, reason.strip()
            row.active, row.updated_at = active, datetime.now(UTC)
            db.flush()
            # The ban is cross-event. Its business audit belongs to the current retention lifecycle.
            event = (
                db.get(Event, settings.active_event_id)
                if settings.active_event_id
                else None
            )
            db.add(
                BusinessAudit(
                    actor_id=actor_id,
                    event_id=event.id if event else None,
                    entity="banned_channel",
                    entity_id=row.id,
                    action="ban_changed" if ban_id else "ban_created",
                    changes={
                        "before": before,
                        "after": {
                            "active": active,
                            "platform": row.platform,
                            "account": row.normalized_account,
                        },
                        "reason_changed": True,
                    },
                )
            )
            return row.id
    except IntegrityError:
        raise HTTPException(
            409, "This normalized account already has a ban record; edit that record"
        ) from None


def delete_ban(db, actor_id, ban_id, reason):
    if not reason.strip() or len(reason) > 4000:
        raise HTTPException(422, "A deletion reason of 1–4000 characters is required")
    db.rollback()
    with db.begin():
        require_admin(db, actor_id, lock=True)
        row = db.get(BannedChannel, ban_id, with_for_update=True)
        if row is None:
            raise HTTPException(404, "Ban not found")
        event = (
            db.get(Event, settings.active_event_id)
            if settings.active_event_id
            else None
        )
        db.add(
            BusinessAudit(
                actor_id=actor_id,
                event_id=event.id if event else None,
                entity="banned_channel",
                entity_id=row.id,
                action="ban_deleted",
                reason=reason.strip(),
                changes={"active": row.active},
            )
        )
        db.delete(row)
