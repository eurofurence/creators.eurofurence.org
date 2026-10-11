from fastapi import HTTPException

from app.applications.input import validate_channels
from app.applications.workflow import (
    active_event,
    get_application,
    replace_channels,
)
from app.creators.models import CreatorProfile
from app.creators.policy import authorize, change_allowed, check_version, require_active
from app.helpers.service import record_change, touch
from app.moderation.service import warnings


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
    return warnings(db, application_id)


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
    validate_channels(channels)
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
        replace_channels(db, application.id, channels)
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
