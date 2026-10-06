import warnings
from datetime import UTC, datetime, timedelta
from io import BytesIO
from typing import Protocol
from uuid import uuid4

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException
from PIL import Image, UnidentifiedImageError
from sqlalchemy import select

from app import storage
from app.applications.workflow import utc
from app.config import settings
from app.creators.models import CreatorProfile, ProfileImage
from app.creators.service import profile_context
from app.events.models import Event
from app.helpers.service import record_change, touch


class ImageStorageUnavailable(Exception):
    pass


class ImageStore(Protocol):
    def put(self, key: str, data: bytes) -> None: ...
    def get(self, key: str) -> bytes: ...
    def delete(self, key: str) -> None: ...


class S3ImageStore:
    def put(self, key, data):
        self._call(storage.upload_object, key, data, "image/png")

    def get(self, key):
        return self._call(storage.get_object, key)

    def delete(self, key):
        self._call(storage.delete_object, key)

    @staticmethod
    def _call(operation, *args):
        try:
            return operation(*args)
        except BotoCoreError, ClientError, RuntimeError, TimeoutError:
            raise ImageStorageUnavailable from None


def get_image_store() -> ImageStore:
    return S3ImageStore()


def normalize_png(data: bytes) -> bytes:
    if len(data) > settings.profile_image_max_bytes:
        raise HTTPException(413, "Profile picture exceeds the upload limit.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data), formats=("PNG",)) as source:
                if source.format != "PNG" or source.size != (600, 600):
                    raise ValueError("dimensions")
                source.verify()
            with Image.open(BytesIO(data), formats=("PNG",)) as source:
                source.load()
                # Fresh pixel image excludes text, EXIF, ICC and other source metadata.
                clean = Image.frombytes(
                    "RGBA", source.size, source.convert("RGBA").tobytes()
                )
            output = BytesIO()
            clean.save(output, format="PNG", dpi=(300, 300))
            return output.getvalue()
    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
        SyntaxError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        raise HTTPException(
            422, "Upload a valid PNG image exactly 600 × 600 pixels."
        ) from None


def replace_picture(db, actor_id, application_id, version, data, store, **policy):
    db.rollback()
    # Authorize before decoding or contacting storage; persist a recoverable object intent.
    with db.begin():
        event, _, _ = profile_context(
            db, actor_id, application_id, version=version, **policy
        )
        event_id = event.id
    normalized = normalize_png(data)
    key = f"profiles/{uuid4().hex}.png"
    with db.begin():
        image = ProfileImage(
            event_id=event_id,
            object_key=key,
            state="STAGED",
            delete_after=datetime.now(UTC) + timedelta(hours=1),
        )
        db.add(image)
        db.flush()
        image_id = image.id
    try:
        store.put(key, normalized)
        with db.begin():
            _, application, profile = profile_context(
                db, actor_id, application_id, version=version, **policy
            )
            image = db.get(ProfileImage, image_id, with_for_update=True)
            if (
                image is None
                or image.state != "STAGED"
                or datetime.now(UTC) >= utc(image.delete_after)
            ):
                raise HTTPException(409, "Upload expired. Please retry.")
            old = db.get(ProfileImage, profile.image_id) if profile.image_id else None
            if old:
                old.state, old.delete_after = "DELETE", datetime.now(UTC)
            profile.image_id, image.state = image.id, "ACTIVE"
            touch(application)
            record_change(
                db,
                actor_id,
                application,
                "profile",
                application.id,
                "picture_replaced",
                {
                    "version": application.version,
                    "exceptional": policy.get("exceptional", False),
                },
                policy.get("reason", ""),
            )
    except Exception as error:
        db.rollback()
        # The committed STAGED record also survives a DB outage or process crash.
        with db.begin():
            image = db.get(ProfileImage, image_id)
            if image and image.state != "ACTIVE":
                image.state, image.delete_after = "DELETE", datetime.now(UTC)
        cleanup_images(db, store, image_id=image_id)
        if isinstance(error, ImageStorageUnavailable):
            raise HTTPException(
                503,
                "Image storage is unavailable. Your previous picture is unchanged; retry later.",
            ) from None
        raise
    cleanup_images(db, store)


def cleanup_images(db, store, *, image_id=None):
    """Retry orphan/replaced objects and expired event images; return failure count."""
    db.rollback()
    now = datetime.now(UTC)
    query = (
        select(ProfileImage.id)
        .join(Event)
        .where(
            ((ProfileImage.state != "ACTIVE") & (ProfileImage.delete_after <= now))
            | (Event.data_delete_at <= now)
        )
    )
    if image_id is not None:
        query = query.where(ProfileImage.id == image_id)
    ids = list(db.scalars(query))
    db.rollback()
    failures = 0
    for key_id in ids:
        with db.begin():
            image = db.get(ProfileImage, key_id, with_for_update=True)
            if image is None:
                continue
            event = db.get(Event, image.event_id)
            if image.state == "ACTIVE" and utc(event.data_delete_at) > now:
                continue
            if image.state == "STAGED" and utc(image.delete_after) > now:
                continue
            image.state = "DELETE"
            key = image.object_key
            # Expired-event images are no longer accessible; retain object reference for retries.
            for profile in db.scalars(
                select(CreatorProfile).where(CreatorProfile.image_id == key_id)
            ):
                profile.image_id = None
        try:
            store.delete(key)
        except ImageStorageUnavailable:
            failures += 1
            with db.begin():
                image = db.get(ProfileImage, key_id)
                if image:
                    image.deletion_failed = True
        else:
            with db.begin():
                image = db.get(ProfileImage, key_id)
                if image:
                    db.delete(image)
    return failures


def main():
    from sqlalchemy.orm import Session

    from app.database import engine

    with Session(engine) as db:
        failures = cleanup_images(db, get_image_store())
    if failures:
        raise SystemExit(f"{failures} image deletions failed; retained for retry.")


if __name__ == "__main__":
    main()
