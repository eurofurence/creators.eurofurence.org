from datetime import UTC, datetime

from fastapi import HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from app.applications.models import BusinessAudit, CreatorApplication, CreatorChannel
from app.applications.security import require_admin
from app.creators.models import CreatorProfile, ProfileImage
from app.events.models import Event
from app.staff.service import event_record


class PublicChannel(BaseModel):
    platform: str
    account: str
    url: str
    primary: bool


class PublicCreator(BaseModel):
    id: str
    name: str
    channels: list[PublicChannel]
    image_url: str


def public_records(db, year, public_id=None):
    """One publication predicate shared by HTML, API and image access."""
    query = (
        select(CreatorProfile, ProfileImage)
        .join(
            CreatorApplication, CreatorProfile.application_id == CreatorApplication.id
        )
        .join(Event, CreatorApplication.event_id == Event.id)
        .join(ProfileImage, CreatorProfile.image_id == ProfileImage.id)
        .where(
            Event.year == year,
            Event.data_delete_at > datetime.now(UTC),
            Event.cleanup_started_at.is_(None),
            CreatorApplication.status == "APPROVED",
            CreatorApplication.withdrawn_at.is_(None),
            CreatorProfile.publicly_hidden.is_(False),
            CreatorProfile.channel_name != "",
            ProfileImage.state == "ACTIVE",
            ProfileImage.event_id == Event.id,
        )
    )
    if public_id is not None:
        query = query.where(CreatorProfile.public_id == public_id)
    result = []
    for profile, image in db.execute(query.execution_options(populate_existing=True)):
        channels = list(
            db.scalars(
                select(CreatorChannel)
                .where(CreatorChannel.application_id == profile.application_id)
                .order_by(CreatorChannel.is_primary.desc(), CreatorChannel.id)
            )
        )
        if not profile.channel_name.strip() or sum(c.is_primary for c in channels) != 1:
            continue
        if not all(
            c.canonical_url.startswith("https://") and c.normalized_account
            for c in channels
        ):
            continue
        public = PublicCreator(
            id=profile.public_id,
            name=profile.channel_name,
            channels=[
                PublicChannel(
                    platform=c.platform,
                    account=c.normalized_account,
                    url=c.canonical_url,
                    primary=c.is_primary,
                )
                for c in channels
                if not c.publicly_hidden
            ],
            image_url=f"/api/v1/events/{year}/creators/{profile.public_id}/image",
        )
        result.append((public, image.object_key))
    return sorted(result, key=lambda row: (row[0].name.casefold(), row[0].id))


def set_visibility(db, actor_id, application_id, hidden, *, channel_id=None):
    db.rollback()
    with db.begin():
        require_admin(db, actor_id, lock=True)
        application = db.get(CreatorApplication, application_id)
        if application is None:
            raise HTTPException(404, "Creator not found")
        event_record(db, application.event_id, lock=True)
        if channel_id is None:
            record = db.get(CreatorProfile, application_id, populate_existing=True)
        else:
            record = db.scalar(
                select(CreatorChannel)
                .where(
                    CreatorChannel.id == channel_id,
                    CreatorChannel.application_id == application_id,
                )
                .execution_options(populate_existing=True)
            )
        if record is None:
            raise HTTPException(404, "Publication record not found")
        if record.publicly_hidden == hidden:
            return
        before = record.publicly_hidden
        record.publicly_hidden = hidden
        db.add(
            BusinessAudit(
                actor_id=actor_id,
                event_id=application.event_id,
                entity="channel" if channel_id is not None else "profile",
                entity_id=channel_id if channel_id is not None else application_id,
                action="public_visibility_changed",
                changes={"hidden_before": before, "hidden_after": hidden},
            )
        )
