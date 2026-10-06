from sqlalchemy import select, update

from app.applications.models import Badge, BadgeCounter, BusinessAudit
from app.config import settings


def allocate(db, event_id, actor_id, *, application_id=None, helper_id=None):
    """Caller holds the event lock and owns the earning-state transaction."""
    if (application_id is None) == (helper_id is None):
        raise ValueError("Exactly one badge owner is required")
    owner = (
        Badge.application_id == application_id
        if application_id is not None
        else Badge.helper_id == helper_id
    )
    existing = db.scalar(select(Badge).where(owner))
    if existing:
        return existing
    if db.get(BadgeCounter, event_id) is None:
        db.add(
            BadgeCounter(event_id=event_id, next_number=settings.badge_sequence_start)
        )
        db.flush()
    number = (
        db.scalar(
            update(BadgeCounter)
            .where(BadgeCounter.event_id == event_id)
            .values(next_number=BadgeCounter.next_number + 1)
            .returning(BadgeCounter.next_number)
        )
        - 1
    )
    badge = Badge(
        application_id=application_id,
        helper_id=helper_id,
        event_id=event_id,
        badge_number=number,
    )
    db.add(badge)
    db.add(
        BusinessAudit(
            actor_id=actor_id,
            event_id=event_id,
            entity="application" if application_id else "helper",
            entity_id=application_id or helper_id,
            action="badge_allocated",
            changes={"badge_number": number},
        )
    )
    return badge
