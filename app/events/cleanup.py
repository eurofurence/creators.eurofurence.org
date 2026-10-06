"""Retryable retention cleanup. Object deletion always precedes database deletion."""

import logging
from datetime import UTC, datetime

from sqlalchemy import delete, exists, select
from sqlalchemy.exc import SQLAlchemyError

from app.applications.models import (
    BusinessAudit,
    CreatorApplication,
    LocalRoleAssignment,
    NotificationOutbox,
)
from app.applications.workflow import utc
from app.creators.images import cleanup_images, get_image_store
from app.creators.models import ProfileImage
from app.events.models import Event
from app.helpers.models import HelperRegistration
from app.identity.models import ExternalIdentity, LocalUser

logger = logging.getLogger(__name__)


def delete_unneeded_users(db):
    # An overlapping event may still need the same identity. Roles/audit alone
    # do not justify keeping a participant identity indefinitely.
    users = list(
        db.scalars(
            select(LocalUser)
            .where(
                ~exists().where(CreatorApplication.user_id == LocalUser.id),
                ~exists().where(HelperRegistration.user_id == LocalUser.id),
                ~exists().where(NotificationOutbox.recipient_id == LocalUser.id),
                ~exists().where(
                    (LocalRoleAssignment.user_id == LocalUser.id)
                    & LocalRoleAssignment.event_id.is_not(None)
                ),
            )
            .with_for_update()
        )
    )
    for user in users:
        db.execute(
            delete(BusinessAudit).where(
                BusinessAudit.event_id.is_(None),
                (BusinessAudit.actor_id == user.id)
                | (
                    BusinessAudit.entity.in_(("user", "local_role"))
                    & (BusinessAudit.entity_id == user.id)
                ),
            )
        )
        db.execute(delete(ExternalIdentity).where(ExternalIdentity.user_id == user.id))
        db.delete(user)


def cleanup_event(db, store, event_id, *, now=None):
    now = now or datetime.now(UTC)
    db.rollback()
    with db.begin():
        event = db.get(Event, event_id, with_for_update=True, populate_existing=True)
        if event is None:
            return True
        if utc(event.data_delete_at) > now:
            return False
        event.cleanup_started_at = event.cleanup_started_at or now
        # An upload/notification already in flight must finish before its
        # references disappear. New operations are blocked by the event freeze.
        in_flight_image = db.scalar(
            select(ProfileImage.id)
            .where(
                ProfileImage.event_id == event_id,
                ProfileImage.state == "STAGED",
                ProfileImage.delete_after > now,
            )
            .limit(1)
        )
        in_flight_notice = db.scalar(
            select(NotificationOutbox.id)
            .where(
                NotificationOutbox.event_id == event_id,
                NotificationOutbox.state == "SENDING",
                NotificationOutbox.claimed_until > now,
            )
            .limit(1)
        )
        if in_flight_image is not None or in_flight_notice is not None:
            event.cleanup_failed = True
            return False
    cleanup_images(db, store, event_id=event_id)
    db.rollback()
    with db.begin():
        event = db.get(Event, event_id, with_for_update=True, populate_existing=True)
        if event is None:
            return True
        if (
            db.scalar(
                select(ProfileImage.id)
                .where(ProfileImage.event_id == event_id)
                .limit(1)
            )
            is not None
        ):
            event.cleanup_failed = True
            return False
        # Cross-event moderation state is deliberately not Event-owned.
        db.execute(
            delete(BusinessAudit).where(
                BusinessAudit.event_id.is_(None),
                BusinessAudit.occurred_at <= event.ends_at,
            )
        )
        db.delete(event)
        db.flush()
        delete_unneeded_users(db)
        if db.scalar(select(Event.id).limit(1)) is None:
            db.execute(delete(BusinessAudit))
    return True


def cleanup_due_events(db, store):
    db.rollback()
    ids = list(
        db.scalars(
            select(Event.id)
            .where(Event.data_delete_at <= datetime.now(UTC))
            .order_by(Event.id)
        )
    )
    db.rollback()
    failures = 0
    for event_id in ids:
        try:
            if not cleanup_event(db, store, event_id):
                failures += 1
                logger.warning(
                    "Event cleanup incomplete; retained for retry (event %s)", event_id
                )
        except SQLAlchemyError:
            db.rollback()
            failures += 1
            logger.error(
                "Event cleanup database failure; retry required (event %s)", event_id
            )
    return failures


def main():
    from app.database import SessionLocal

    with SessionLocal() as db:
        failures = cleanup_due_events(db, get_image_store())
        failures += cleanup_images(db, get_image_store())
    if failures:
        raise SystemExit(f"{failures} cleanup operations incomplete; retry required.")
    print("Retention cleanup completed.")


if __name__ == "__main__":
    main()
