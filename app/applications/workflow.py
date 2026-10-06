from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.applications.input import ApplicationInput
from app.applications.models import (
    BusinessAudit,
    ConventionVideo,
    CreatorApplication,
    CreatorChannel,
    NotificationOutbox,
)
from app.applications.security import require_admin
from app.applications.service import registration_lookup_for_user
from app.config import settings
from app.events.models import Event
from app.identity.profile import IdentityProfileClient, IdentityProfileUnavailable
from app.registration.client import RegistrationClient, RegistrationUnavailable
from app.registration.eligibility import Eligibility, classify_result

TRANSITIONS = {
    "NEW": ("ON_REVIEW", "NOT_ACCEPTED"),
    "ON_REVIEW": ("APPROVED", "NOT_APPROVED"),
    "NOT_APPROVED": ("ON_REVIEW",),
    "NOT_ACCEPTED": ("ON_REVIEW",),
    "APPROVED": ("ON_REVIEW",),
}


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def active_event(db: Session, *, lock=False) -> Event:
    query = select(Event).where(Event.id == settings.active_event_id)
    if lock:
        query = query.with_for_update()
    event = db.scalar(query.execution_options(populate_existing=True))
    if event is None:
        raise HTTPException(
            503, "The application event is unavailable. Please retry later."
        )
    if event.cleanup_started_at is not None or utc(
        event.data_delete_at
    ) <= datetime.now(UTC):
        raise HTTPException(410, "This event is no longer available.")
    return event


def window_open(event: Event, now: datetime) -> bool:
    return utc(event.application_open_at) <= now < utc(event.application_close_at)


def applicant_can_edit(event: Event, record: CreatorApplication, now: datetime) -> bool:
    return (
        record.status == "NEW"
        and record.withdrawn_at is None
        and window_open(event, now)
        and (
            event.badge_change_deadline_at is None
            or now < utc(event.badge_change_deadline_at)
        )
    )


def application_for_user(db: Session, user_id: int, event_id: int):
    return db.scalar(
        select(CreatorApplication).where(
            CreatorApplication.user_id == user_id,
            CreatorApplication.event_id == event_id,
        )
    )


def get_application(
    db: Session, application_id: int, event_id: int
) -> CreatorApplication:
    record = db.scalar(
        select(CreatorApplication)
        .where(
            CreatorApplication.id == application_id,
            CreatorApplication.event_id == event_id,
        )
        .with_for_update()
    )
    if record is None:
        raise HTTPException(404, "Application not found")
    return record


async def verified_snapshot(
    db: Session,
    user_id: int,
    registration: RegistrationClient,
    identity: IdentityProfileClient,
):
    lookup = registration_lookup_for_user(db, user_id)
    db.rollback()  # End read transaction before calling external services.
    if lookup is None:
        raise HTTPException(
            503, "Eligibility verification is unavailable. Please retry."
        )
    try:
        result = await registration.lookup(lookup)
    except RegistrationUnavailable, TimeoutError:
        raise HTTPException(
            503, "Eligibility verification is unavailable. Please retry."
        ) from None
    state = classify_result(result, lookup)
    if state is Eligibility.UNAVAILABLE:
        raise HTTPException(
            503, "Eligibility verification is unavailable. Please retry."
        )
    if state is Eligibility.INELIGIBLE:
        raise HTTPException(
            409,
            "Registration is currently ineligible. Workflow status has not changed.",
        )
    try:
        email = await identity.email(lookup)
    except IdentityProfileUnavailable, TimeoutError:
        email = None
    return lookup, result, email


def audit(db, actor_id, event_id, entity_id, action, changes, reason=""):
    db.add(
        BusinessAudit(
            actor_id=actor_id,
            event_id=event_id,
            entity="application",
            entity_id=entity_id,
            action=action,
            changes=changes,
            reason=reason,
        )
    )


def replace_input(db: Session, record: CreatorApplication, data: ApplicationInput):
    data.validate()
    hidden = {
        (channel.platform, channel.normalized_account): channel.publicly_hidden
        for channel in db.scalars(
            select(CreatorChannel).where(CreatorChannel.application_id == record.id)
        )
    }
    for name in ("livestream", "shorts", "vlogs"):
        setattr(record, name, name.upper() in data.content_types)
    db.execute(delete(CreatorChannel).where(CreatorChannel.application_id == record.id))
    db.execute(
        delete(ConventionVideo).where(ConventionVideo.application_id == record.id)
    )
    for channel in data.channels:
        db.add(
            CreatorChannel(
                application_id=record.id,
                platform=channel.platform,
                original_representation=channel.original_representation,
                normalized_account=channel.normalized_account,
                canonical_url=channel.canonical_url,
                is_primary=channel.is_primary,
                publicly_hidden=hidden.get(
                    (channel.platform, channel.normalized_account), False
                ),
            )
        )
    for i, url in enumerate(data.videos):
        db.add(ConventionVideo(application_id=record.id, position=i, url=url))


async def submit(
    db: Session,
    user_id: int,
    data: ApplicationInput,
    registration: RegistrationClient,
    identity: IdentityProfileClient,
    *,
    picture: bytes | None = None,
    store=None,
) -> int:
    data.validate()
    event = active_event(db)
    existing = application_for_user(db, user_id, event.id)
    if existing:
        return (
            existing.id
        )  # Repeated submit never edits or creates another application.
    if not window_open(event, datetime.now(UTC)):
        raise HTTPException(409, "The application window is closed.")
    if not picture or store is None:
        raise HTTPException(
            422, "A PNG profile picture is required when submitting your application."
        )
    lookup, result, email = await verified_snapshot(db, user_id, registration, identity)
    from app.creators.images import (
        attach_picture,
        discard_staged_picture,
        stage_picture,
    )
    from app.creators.models import CreatorProfile

    def upload_context():
        current = active_event(db, lock=True)
        if (current.id, current.year) != (lookup.event_id, lookup.event_year):
            raise HTTPException(409, "The active event changed. Reload and retry.")
        if not window_open(current, datetime.now(UTC)):
            raise HTTPException(409, "The application window is closed.")
        return current

    image_id = stage_picture(db, picture, store, upload_context)
    try:
        with db.begin():
            event = active_event(db, lock=True)
            if (event.id, event.year) != (lookup.event_id, lookup.event_year):
                raise HTTPException(409, "The active event changed. Reload and retry.")
            existing = application_for_user(db, user_id, event.id)
            if existing:
                return existing.id
            now = datetime.now(UTC)
            if not window_open(event, now):
                raise HTTPException(409, "The application window is closed.")
            record = CreatorApplication(
                user_id=user_id,
                event_id=event.id,
                reg_id=result.reg_id,
                nickname=result.nickname,
                email=email,
                eligibility_checked_at=now,
                livestream="LIVESTREAM" in data.content_types,
                shorts="SHORTS" in data.content_types,
                vlogs="VLOGS" in data.content_types,
            )
            db.add(record)
            db.flush()
            replace_input(db, record, data)
            profile = CreatorProfile(application_id=record.id)
            db.add(profile)
            attach_picture(db, profile, image_id, event.id)
            audit(
                db,
                user_id,
                event.id,
                record.id,
                "submitted",
                {"status": "NEW", "picture_supplied": True},
            )
            return record.id
    finally:
        # ACTIVE images stay attached; duplicate submits/failed commits leave no live orphan.
        discard_staged_picture(db, store, image_id)


def edit(
    db: Session,
    user_id: int,
    application_id: int,
    version: int,
    data: ApplicationInput,
    *,
    administrative=False,
    reason="",
):
    data.validate()
    db.rollback()
    with db.begin():
        if administrative:
            require_admin(db, user_id, lock=True)
            if not reason.strip():
                raise HTTPException(
                    422, "An administrative correction requires a reason."
                )
        event = active_event(db, lock=True)
        record = get_application(db, application_id, event.id)
        if not administrative and record.user_id != user_id:
            raise HTTPException(404, "Application not found")
        if record.version != version:
            raise HTTPException(409, "The application changed. Reload before editing.")
        now = datetime.now(UTC)
        if not administrative and not applicant_can_edit(event, record, now):
            raise HTTPException(409, "Applicant editing is locked.")
        replace_input(db, record, data)
        record.version += 1
        record.updated_at = datetime.now(UTC)
        audit(
            db,
            user_id,
            event.id,
            record.id,
            "admin_corrected" if administrative else "edited",
            {"version": record.version},
            reason,
        )


async def review(
    db: Session,
    actor_id: int,
    application_id: int,
    version: int,
    target: str,
    reason: str,
    staff_notes: str,
    exceptional: bool,
    registration: RegistrationClient,
    identity: IdentityProfileClient,
):
    if target not in TRANSITIONS or len(reason) > 4000 or len(staff_notes) > 10000:
        raise HTTPException(422, "Invalid review input")
    require_admin(db, actor_id)
    event = active_event(db)
    record = get_application(db, application_id, event.id)
    applicant_id = record.user_id
    if record.version != version:
        raise HTTPException(409, "The application changed. Reload before reviewing.")
    evidence = (
        await verified_snapshot(db, applicant_id, registration, identity)
        if target == "APPROVED"
        else None
    )
    db.rollback()
    with db.begin():
        require_admin(db, actor_id, lock=True)
        event = active_event(db, lock=True)
        record = get_application(db, application_id, event.id)
        if record.version != version:
            raise HTTPException(
                409, "The application changed. Reload before reviewing."
            )
        if target == "APPROVED" or (
            target == "ON_REVIEW" and record.status != "APPROVED"
        ):
            from app.creators.models import CreatorProfile, ProfileImage

            profile = db.get(CreatorProfile, record.id)
            image = (
                db.get(ProfileImage, profile.image_id)
                if profile and profile.image_id
                else None
            )
            if image is None or image.state != "ACTIVE" or image.event_id != event.id:
                raise HTTPException(
                    409,
                    "A valid profile picture is required before review or approval.",
                )
        previous = record.status
        if target not in TRANSITIONS[previous]:
            raise HTTPException(409, "This status transition is not allowed.")
        now = datetime.now(UTC)
        if target == "APPROVED" or previous == "APPROVED":
            if event.badge_change_deadline_at is None:
                raise HTTPException(
                    503,
                    "The badge change deadline must be configured before approval changes.",
                )
            if now >= utc(event.badge_change_deadline_at) and not exceptional:
                raise HTTPException(
                    409,
                    "The badge change deadline has passed. An explicit administrative correction is required.",
                )
        if (
            target in ("NOT_APPROVED", "NOT_ACCEPTED")
            or previous == "APPROVED"
            or exceptional
        ) and not reason.strip():
            raise HTTPException(422, "An applicant-visible reason is required.")
        if evidence:
            lookup, result, email = evidence
            if (lookup.event_id, lookup.event_year) != (
                event.id,
                event.year,
            ) or record.user_id != applicant_id:
                raise HTTPException(
                    409, "The application context changed. Retry verification."
                )
            record.reg_id, record.nickname, record.email = (
                result.reg_id,
                result.nickname,
                email,
            )
            record.eligibility_checked_at = now
        record.status, record.outcome_reason, record.staff_notes = (
            target,
            reason.strip(),
            staff_notes.strip(),
        )
        record.version += 1
        record.updated_at = now
        from app.badges.service import allocate
        from app.creators.models import CreatorProfile
        from app.helpers.service import creator_activity_changed

        if target == "APPROVED":
            if record.withdrawn_at is not None:
                raise HTTPException(
                    409, "Restore the withdrawn application before approving it."
                )
            allocate(db, event.id, actor_id, application_id=record.id)
            if db.get(CreatorProfile, record.id) is None:
                db.add(CreatorProfile(application_id=record.id))
            creator_activity_changed(db, actor_id, record, True, reason)
        elif previous == "APPROVED":
            creator_activity_changed(db, actor_id, record, False, reason)
        audit(
            db,
            actor_id,
            event.id,
            record.id,
            "status_changed",
            {"before": previous, "after": target, "exceptional": exceptional},
            reason.strip(),
        )
        if (
            target in ("APPROVED", "NOT_APPROVED", "NOT_ACCEPTED")
            or previous == "APPROVED"
        ):
            db.add(
                NotificationOutbox(
                    event_id=event.id,
                    application_id=record.id,
                    application_version=record.version,
                    recipient_id=record.user_id,
                    notification_type="APPROVAL_REVOKED"
                    if previous == "APPROVED"
                    else target,
                )
            )
