from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.identity.models import ExternalIdentity, LocalUser


def resolve_user(db: Session, issuer: str, subject: str) -> LocalUser:
    """Resolve a verified external identity; never link accounts by profile data."""
    query = (
        select(LocalUser)
        .join(ExternalIdentity, ExternalIdentity.user_id == LocalUser.id)
        .where(ExternalIdentity.issuer == issuer, ExternalIdentity.subject == subject)
    )
    user = db.scalar(query)
    if user is not None:
        return user

    try:
        user = LocalUser()
        db.add(user)
        db.flush()
        db.add(ExternalIdentity(user_id=user.id, issuer=issuer, subject=subject))
        db.commit()
        return user
    except IntegrityError:
        # A simultaneous first login may have inserted the same identity.
        # Roll back its provisional user too, then use the winning transaction.
        db.rollback()
        user = db.scalar(query)
        if user is None:
            raise
        return user
