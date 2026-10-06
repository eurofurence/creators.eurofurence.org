from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.events.models import Event
from app.identity.models import ExternalIdentity
from app.registration.client import RegistrationLookup


def registration_lookup_for_user(
    db: Session, user_id: int
) -> RegistrationLookup | None:
    """Resolve server-owned identity and configured event, never caller-supplied IDs."""
    if settings.active_event_id is None or not settings.oidc_issuer_url:
        return None
    event = db.get(Event, settings.active_event_id)
    if event is None:
        return None
    identities = db.scalars(
        select(ExternalIdentity).where(
            ExternalIdentity.user_id == user_id,
            ExternalIdentity.issuer == settings.oidc_issuer_url,
        )
    ).all()
    if len(identities) != 1 or not identities[0].subject:
        return None
    return RegistrationLookup(
        issuer=identities[0].issuer,
        subject=identities[0].subject,
        event_id=event.id,
        event_year=event.year,
    )
