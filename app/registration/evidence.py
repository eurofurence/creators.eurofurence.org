"""One successful verification per participant, event and participation kind."""

from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import select

from app.applications.service import registration_lookup_for_user
from app.identity.profile import IdentityProfileUnavailable
from app.registration.client import (
    RegistrationLookup,
    RegistrationResult,
    RegistrationStatus,
    RegistrationUnavailable,
)
from app.registration.eligibility import Eligibility, classify_result
from app.registration.models import RegistrationVerification

MISSING_EVIDENCE = (
    "Registration verification is missing or does not match this participant and event. "
    "The participant must verify Registration from their application or helper page, then retry."
)


def utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@dataclass(frozen=True)
class VerifiedSnapshot:
    user_id: int
    purpose: str
    lookup: RegistrationLookup
    result: RegistrationResult
    email: str | None
    verified_at: datetime


def stored_snapshot(db, user_id, purpose, *, event_id=None):
    lookup = registration_lookup_for_user(db, user_id, event_id=event_id)
    if lookup is None:
        return None
    row = db.scalar(
        select(RegistrationVerification)
        .where(
            RegistrationVerification.user_id == user_id,
            RegistrationVerification.event_id == lookup.event_id,
            RegistrationVerification.purpose == purpose,
        )
        .execution_options(populate_existing=True)
    )
    if row is None or (row.issuer, row.subject, row.event_year) != (
        lookup.issuer,
        lookup.subject,
        lookup.event_year,
    ):
        return None
    result = RegistrationResult(
        lookup, RegistrationStatus.__members__.get(row.status), row.reg_id, row.nickname
    )
    if (
        classify_result(result, lookup) is not Eligibility.ELIGIBLE
        or row.verified_at is None
    ):
        return None
    return VerifiedSnapshot(
        user_id, purpose, lookup, result, row.email, utc(row.verified_at)
    )


async def verified_snapshot(db, user_id, registration, identity, *, purpose="creator"):
    if purpose not in ("creator", "helper"):
        raise HTTPException(422, "Invalid verification purpose.")
    existing = stored_snapshot(db, user_id, purpose)
    lookup = registration_lookup_for_user(db, user_id)
    db.rollback()  # No transaction or user token is held between workflow requests.
    if existing is not None:
        return existing
    if lookup is None:
        raise HTTPException(
            503, "Eligibility verification is unavailable. Please retry."
        )
    try:
        result = await registration.lookup(lookup)
    except RegistrationUnavailable, TimeoutError:
        raise HTTPException(
            503,
            "Eligibility verification is unavailable. Please verify Registration and retry.",
        ) from None
    state = classify_result(result, lookup)
    if state is Eligibility.UNAVAILABLE:
        raise HTTPException(
            503, "Eligibility verification is unavailable. Please retry."
        )
    if state is Eligibility.INELIGIBLE:
        raise HTTPException(
            409,
            "Registration is currently ineligible. Confirm payment, then retry verification. Workflow status has not changed.",
        )
    verified_at = datetime.now(UTC)
    try:
        email = await identity.email(lookup)
    except IdentityProfileUnavailable, TimeoutError:
        email = None
    return VerifiedSnapshot(user_id, purpose, lookup, result, email, verified_at)


def save_snapshot(db, event, evidence, record=None):
    """Caller holds the Event lock; revalidate binding before storing evidence."""
    lookup = registration_lookup_for_user(db, evidence.user_id, event_id=event.id)
    if lookup != evidence.lookup or (event.id, event.year) != (
        lookup.event_id,
        lookup.event_year,
    ):
        raise HTTPException(409, "The event or identity changed. Retry verification.")
    current = db.scalar(
        select(RegistrationVerification).where(
            RegistrationVerification.user_id == evidence.user_id,
            RegistrationVerification.event_id == event.id,
            RegistrationVerification.purpose == evidence.purpose,
        )
    )
    if current is not None:
        evidence = stored_snapshot(
            db, evidence.user_id, evidence.purpose, event_id=event.id
        )
        if evidence is None:
            raise HTTPException(409, MISSING_EVIDENCE)
    else:
        from app.applications.models import BusinessAudit

        db.add(
            RegistrationVerification(
                user_id=evidence.user_id,
                event_id=event.id,
                event_year=event.year,
                purpose=evidence.purpose,
                issuer=lookup.issuer,
                subject=lookup.subject,
                status=evidence.result.status.name,
                reg_id=evidence.result.reg_id,
                nickname=evidence.result.nickname,
                email=evidence.email,
                verified_at=evidence.verified_at,
            )
        )
        db.add(
            BusinessAudit(
                actor_id=evidence.user_id,
                event_id=event.id,
                entity="registration",
                entity_id=evidence.user_id,
                action="registration_verified",
                changes={
                    "purpose": evidence.purpose,
                    "status": evidence.result.status.name,
                },
            )
        )
    if record is not None:
        if (record.user_id, record.event_id) != (evidence.user_id, event.id):
            raise HTTPException(409, MISSING_EVIDENCE)
        record.reg_id = evidence.result.reg_id
        record.nickname = evidence.result.nickname
        record.email = evidence.email
        record.eligibility_checked_at = evidence.verified_at
    return evidence


def record_verified(db, record, purpose):
    evidence = stored_snapshot(db, record.user_id, purpose, event_id=record.event_id)
    return bool(
        evidence
        and record.reg_id == evidence.result.reg_id
        and record.eligibility_checked_at is not None
        and utc(record.eligibility_checked_at) == evidence.verified_at
    )


def require_record_verified(db, record, purpose):
    if not record_verified(db, record, purpose):
        raise HTTPException(409, MISSING_EVIDENCE)


async def verify_participant(
    db, user_id, purpose, registration, identity, *, expected_event=None
):
    from app.applications.models import CreatorApplication
    from app.applications.workflow import active_event
    from app.helpers.models import HelperRegistration

    evidence = await verified_snapshot(
        db, user_id, registration, identity, purpose=purpose
    )
    with db.begin():
        event = active_event(db, lock=True)
        if expected_event is not None and expected_event != (event.id, event.year):
            raise HTTPException(409, "The event changed. Retry verification.")
        evidence = save_snapshot(db, event, evidence)
        # Legacy records have no explicit binding. Populate only after real verification.
        model = CreatorApplication if purpose == "creator" else HelperRegistration
        for record in db.scalars(
            select(model)
            .where(model.user_id == user_id, model.event_id == event.id)
            .with_for_update()
        ):
            if not record_verified(db, record, purpose):
                save_snapshot(db, event, evidence, record)
                record.version += 1
