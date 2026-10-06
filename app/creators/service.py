from fastapi import HTTPException
from sqlalchemy import delete, select

from app.applications.models import CreatorChannel
from app.applications.workflow import active_event, get_application
from app.creators.models import BannedChannel, CreatorProfile
from app.creators.policy import authorize, change_allowed, check_version, require_active
from app.helpers.service import record_change, touch


def profile_context(
    db,
    actor_id,
    application_id,
    *,
    administrative=False,
    reason="",
    exceptional=False,
    version=None,
):
    event = active_event(db, lock=True)
    application = get_application(db, application_id, event.id)
    authorize(
        db, actor_id, application.user_id, administrative=administrative, reason=reason
    )
    require_active(application)
    change_allowed(event, administrative=administrative, exceptional=exceptional)
    if version is not None:
        check_version(application, version)
    profile = db.get(CreatorProfile, application.id)
    if profile is None:
        profile = CreatorProfile(application_id=application.id)
        db.add(profile)
    return event, application, profile


def banned_matches(db, application_id):
    return db.scalars(
        select(CreatorChannel)
        .join(
            BannedChannel,
            (BannedChannel.platform == CreatorChannel.platform)
            & (BannedChannel.normalized_account == CreatorChannel.normalized_account),
        )
        .where(
            CreatorChannel.application_id == application_id,
            BannedChannel.active.is_(True),
        )
    ).all()


def save_profile(
    db,
    actor_id,
    application_id,
    version,
    name,
    channels,
    *,
    administrative=False,
    exceptional=False,
    reason="",
):
    name = name.strip()
    if not name or len(name) > 200 or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise HTTPException(
            422, "Enter a display name of 1–200 characters without control characters."
        )
    # Reuse application validation for canonical channels and exactly one primary.
    from app.applications.input import ApplicationInput

    ApplicationInput(("VLOGS",), channels, ()).validate()
    db.rollback()
    with db.begin():
        _, application, profile = profile_context(
            db,
            actor_id,
            application_id,
            administrative=administrative,
            exceptional=exceptional,
            reason=reason,
            version=version,
        )
        profile.channel_name = name
        existing = {
            (c.platform, c.normalized_account): c.publicly_hidden
            for c in db.scalars(
                select(CreatorChannel).where(
                    CreatorChannel.application_id == application.id
                )
            )
        }
        db.execute(
            delete(CreatorChannel).where(
                CreatorChannel.application_id == application.id
            )
        )
        for channel in channels:
            db.add(
                CreatorChannel(
                    application_id=application.id,
                    platform=channel.platform,
                    original_representation=channel.original_representation,
                    normalized_account=channel.normalized_account,
                    canonical_url=channel.canonical_url,
                    is_primary=channel.is_primary,
                    publicly_hidden=existing.get(
                        (channel.platform, channel.normalized_account), False
                    ),
                )
            )
        touch(application)
        db.flush()
        warnings = len(banned_matches(db, application.id))
        record_change(
            db,
            actor_id,
            application,
            "profile",
            application.id,
            "profile_changed",
            {
                "version": application.version,
                "banned_matches": warnings,
                "exceptional": exceptional,
            },
            reason,
        )
