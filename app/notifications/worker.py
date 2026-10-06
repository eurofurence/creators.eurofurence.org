"""Bounded, durable outbox delivery outside business transactions."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

from app.applications.models import BusinessAudit, NotificationOutbox
from app.applications.workflow import utc
from app.events.models import Event
from app.notifications.client import (
    DeliveryFailure,
    Notification,
    ProviderUnavailable,
    UnavailableNotificationProvider,
    get_notification_provider,
)

MESSAGES = {
    "APPROVED": "Your Creator Application was approved.",
    "NOT_APPROVED": "Your Creator Application was reviewed and not approved.",
    "NOT_ACCEPTED": "Your Creator Application was not accepted for review.",
    "APPROVAL_REVOKED": "An administrator reversed your Creator Application approval.",
    "HELPER_REQUESTED": "A new helper request is waiting for your decision.",
    "HELPER_CONFIRMED": "Your helper request was confirmed.",
    "HELPER_DECLINED": "Your helper request was declined.",
    "CREATOR_INACTIVE": "Your linked creator is no longer active. Your helper participation for that creator is inactive.",
    "CREATOR_RESTORED": "An administrator restored your linked creator. Check your helper participation status.",
    "HELPER_RESTORED": "An administrator restored your helper participation. Check your current status.",
    "HELPER_WITHDRAWN": "Your helper participation was withdrawn.",
}


def due(now):
    return or_(
        and_(
            NotificationOutbox.state == "PENDING",
            or_(
                NotificationOutbox.next_attempt_at.is_(None),
                NotificationOutbox.next_attempt_at <= now,
            ),
        ),
        and_(
            NotificationOutbox.state == "SENDING",
            NotificationOutbox.claimed_until <= now,
        ),
    )


def failed_audit(db, row):
    db.add(
        BusinessAudit(
            event_id=row.event_id,
            actor_id=None,
            entity="notification",
            entity_id=row.id,
            action="notification_failed",
            changes={"attempts": row.attempts, "error": row.last_error},
        )
    )


def claim(db: Session, now: datetime):
    db.rollback()
    with db.begin():
        event = db.scalar(
            select(Event)
            .where(
                Event.data_delete_at > now,
                Event.cleanup_started_at.is_(None),
                Event.id.in_(select(NotificationOutbox.event_id).where(due(now))),
            )
            .order_by(Event.id)
            .with_for_update(skip_locked=True)
        )
        if event is None:
            return None
        row = db.scalar(
            select(NotificationOutbox)
            .where(
                due(now),
                NotificationOutbox.event_id == event.id,
            )
            .order_by(NotificationOutbox.id)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        if row is None:
            return None
        if row.attempts >= 5:
            row.state, row.last_error = "FAILED", "DELIVERY_UNCONFIRMED"
            row.claim_id = row.claimed_until = None
            row.next_attempt_at = None
            failed_audit(db, row)
            return False
        claim_id = uuid4().hex
        # Conditional update also protects claims on databases without row locking.
        result = db.execute(
            update(NotificationOutbox)
            .where(
                NotificationOutbox.id == row.id,
                due(now),
                NotificationOutbox.attempts == row.attempts,
            )
            .values(
                state="SENDING",
                attempts=NotificationOutbox.attempts + 1,
                claim_id=claim_id,
                claimed_until=now + timedelta(minutes=5),
            ),
            execution_options={"synchronize_session": False},
        )
        if result.rowcount != 1:
            return False
        return (
            row.id,
            claim_id,
            Notification(
                delivery_id=f"notification-{row.id}-{utc(row.created_at).isoformat()}",
                recipient_id=row.recipient_id,
                event_id=row.event_id,
                application_id=row.application_id,
                kind=row.notification_type,
                subject="Creator System update",
                body=MESSAGES.get(row.notification_type, "")
                + " Open the Creator System to see details and any decision reason.",
            ),
        )


async def dispatch(db: Session, provider, *, limit=100, now=None):
    """Process a bounded batch. SENT records are never claimed again.

    A crash after provider acceptance but before recording SENT can be retried.
    Exactly-once external delivery requires provider-side idempotency support.
    """
    processed = 0
    for _ in range(limit):
        claimed = claim(db, now or datetime.now(UTC))
        if claimed is None:
            break
        if claimed is False:
            continue
        row_id, claim_id, message = claimed
        failure = None
        retry_after = 0
        try:
            if message.kind not in MESSAGES:
                raise ProviderUnavailable
            await asyncio.wait_for(provider.send(message), timeout=60)
        except ProviderUnavailable:
            failure = "PROVIDER_UNAVAILABLE"
        except DeliveryFailure as error:
            failure = "PROVIDER_REJECTED"
            retry_after = max(0, error.retry_after or 0)
        except Exception:  # noqa: BLE001 - durable retry without retaining provider secrets
            # Never persist or log arbitrary provider error bodies or secrets.
            failure = "DELIVERY_UNCONFIRMED"
        completed_at = now or datetime.now(UTC)
        with db.begin():
            row = db.get(
                NotificationOutbox, row_id, with_for_update=True, populate_existing=True
            )
            if row is None or row.claim_id != claim_id:
                continue
            row.claim_id = row.claimed_until = None
            row.last_error = failure
            if failure is None:
                row.state, row.sent_at, row.next_attempt_at = "SENT", completed_at, None
            elif row.attempts >= 5:
                row.state, row.next_attempt_at = "FAILED", None
                failed_audit(db, row)
            else:
                row.state = "PENDING"
                row.next_attempt_at = completed_at + timedelta(
                    seconds=max(retry_after, 60 * 2 ** (row.attempts - 1))
                )
        processed += 1
    return processed


def main():
    from app.database import SessionLocal
    from app.helpers import models as helper_models  # noqa: F401

    provider = get_notification_provider()
    with SessionLocal() as db:
        asyncio.run(dispatch(db, provider))
        failed = db.scalar(
            select(NotificationOutbox.id)
            .where(NotificationOutbox.state == "FAILED")
            .limit(1)
        )
    if failed is not None:
        raise SystemExit(
            "Notification delivery has failed items; inspect the administration page."
        )
    if isinstance(provider, UnavailableNotificationProvider):
        raise SystemExit(
            "Notification provider is unavailable; pending intents remain scheduled for retry."
        )


if __name__ == "__main__":
    main()
