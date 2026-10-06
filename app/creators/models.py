from datetime import datetime
from uuid import uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ProfileImage(Base):
    __tablename__ = "profile_images"
    __table_args__ = (
        CheckConstraint("state IN ('STAGED','ACTIVE','DELETE')", name="ck_image_state"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    # Keep object references until deletion succeeds, even during event cleanup.
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="RESTRICT"))
    object_key: Mapped[str] = mapped_column(unique=True)
    state: Mapped[str]
    delete_after: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deletion_failed: Mapped[bool] = mapped_column(default=False)


class CreatorProfile(Base):
    __tablename__ = "creator_profiles"
    __table_args__ = (
        CheckConstraint("length(channel_name) <= 200", name="ck_profile_name_length"),
    )
    application_id: Mapped[int] = mapped_column(
        ForeignKey("creator_applications.id", ondelete="CASCADE"), primary_key=True
    )
    channel_name: Mapped[str] = mapped_column(default="")
    public_id: Mapped[str] = mapped_column(unique=True, default=lambda: uuid4().hex)
    publicly_hidden: Mapped[bool] = mapped_column(default=False, server_default="false")
    image_id: Mapped[int | None] = mapped_column(
        ForeignKey("profile_images.id", ondelete="SET NULL"), unique=True
    )


class BannedChannel(Base):
    __tablename__ = "banned_channels"
    __table_args__ = (
        UniqueConstraint("platform", "normalized_account", name="uq_banned_account"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[str]
    normalized_account: Mapped[str]
    original_reference: Mapped[str | None]
    private_reason: Mapped[str]
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
