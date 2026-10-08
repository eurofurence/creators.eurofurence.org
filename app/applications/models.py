from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class CreatorApplication(Base):
    __tablename__ = "creator_applications"
    __table_args__ = (
        UniqueConstraint("user_id", "event_id", name="uq_application_user_event"),
        UniqueConstraint("id", "event_id", name="uq_application_id_event"),
        UniqueConstraint("id", "event_id", "user_id", name="uq_application_owner"),
        CheckConstraint(
            "status IN ('NEW','ON_REVIEW','APPROVED','NOT_APPROVED','NOT_ACCEPTED')",
            name="ck_application_status",
        ),
        CheckConstraint("livestream OR shorts OR vlogs", name="ck_application_content"),
        CheckConstraint("version > 0", name="ck_application_version"),
        CheckConstraint(
            "status NOT IN ('NOT_APPROVED','NOT_ACCEPTED') OR length(trim(outcome_reason)) > 0",
            name="ck_application_reason",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("local_users.id", ondelete="CASCADE")
    )
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(default="NEW")
    version: Mapped[int] = mapped_column(default=1)
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    livestream: Mapped[bool] = mapped_column(Boolean, default=False)
    shorts: Mapped[bool] = mapped_column(Boolean, default=False)
    vlogs: Mapped[bool] = mapped_column(Boolean, default=False)
    outcome_reason: Mapped[str] = mapped_column(default="")
    staff_notes: Mapped[str] = mapped_column(default="")
    reg_id: Mapped[str | None]
    nickname: Mapped[str | None]
    email: Mapped[str | None]
    eligibility_checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class CreatorChannel(Base):
    __tablename__ = "creator_channels"
    __table_args__ = (
        CheckConstraint(
            "platform IN ('Bluesky','Facebook','Instagram','Mastodon','Threads','TikTok','Twitch','X','YouTube')",
            name="ck_channel_platform",
        ),
        UniqueConstraint(
            "application_id",
            "platform",
            "normalized_account",
            name="uq_channel_account",
        ),
        Index(
            "uq_channel_primary",
            "application_id",
            unique=True,
            postgresql_where=text("is_primary"),
            sqlite_where=text("is_primary = 1"),
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(
        ForeignKey("creator_applications.id", ondelete="CASCADE"), index=True
    )
    platform: Mapped[str]
    original_representation: Mapped[str]
    normalized_account: Mapped[str]
    canonical_url: Mapped[str]
    is_primary: Mapped[bool]
    publicly_hidden: Mapped[bool] = mapped_column(default=False)


class ConventionVideo(Base):
    __tablename__ = "convention_videos"
    __table_args__ = (
        CheckConstraint("position >= 0 AND position < 10", name="ck_video_position"),
    )
    application_id: Mapped[int] = mapped_column(
        ForeignKey("creator_applications.id", ondelete="CASCADE"), primary_key=True
    )
    position: Mapped[int] = mapped_column(primary_key=True)
    url: Mapped[str]


class LocalRoleAssignment(Base):
    __tablename__ = "local_role_assignments"
    __table_args__ = (
        CheckConstraint(
            "(role = 'ADMIN' AND event_id IS NULL) OR (role = 'BADGE_STAFF' AND event_id IS NOT NULL)",
            name="ck_role_scope",
        ),
        Index(
            "uq_global_admin",
            "user_id",
            unique=True,
            postgresql_where=text("role = 'ADMIN'"),
            sqlite_where=text("role = 'ADMIN'"),
        ),
        UniqueConstraint("user_id", "event_id", "role", name="uq_event_role"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("local_users.id", ondelete="CASCADE")
    )
    event_id: Mapped[int | None] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE")
    )
    role: Mapped[str]


class BadgeCounter(Base):
    __tablename__ = "badge_counters"
    __table_args__ = (CheckConstraint("next_number > 0", name="ck_counter_positive"),)
    event_id: Mapped[int] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), primary_key=True
    )
    next_number: Mapped[int] = mapped_column(default=1)


class Badge(Base):
    __tablename__ = "badges"
    __table_args__ = (
        UniqueConstraint("event_id", "badge_number", name="uq_badge_event_number"),
        ForeignKeyConstraint(
            ["application_id", "event_id"],
            ["creator_applications.id", "creator_applications.event_id"],
            ondelete="CASCADE",
        ),
        CheckConstraint("badge_number > 0", name="ck_badge_positive"),
        CheckConstraint(
            "(application_id IS NOT NULL AND helper_id IS NULL) OR (application_id IS NULL AND helper_id IS NOT NULL)",
            name="ck_badge_owner",
        ),
        ForeignKeyConstraint(
            ["helper_id", "event_id"],
            ["helper_registrations.id", "helper_registrations.event_id"],
            ondelete="CASCADE",
            name="fk_badge_helper_event",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int | None] = mapped_column(unique=True)
    helper_id: Mapped[int | None] = mapped_column(unique=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"))
    badge_number: Mapped[int]
    picked_up_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    picked_up_by: Mapped[int | None] = mapped_column(
        ForeignKey("local_users.id", ondelete="SET NULL")
    )
    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class BusinessAudit(Base):
    __tablename__ = "business_audit"
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int | None] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), index=True
    )
    actor_id: Mapped[int | None] = mapped_column(
        ForeignKey("local_users.id", ondelete="SET NULL")
    )
    entity: Mapped[str]
    entity_id: Mapped[int]
    action: Mapped[str]
    reason: Mapped[str] = mapped_column(default="")
    changes: Mapped[dict] = mapped_column(JSON, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class NotificationOutbox(Base):
    __tablename__ = "notification_outbox"
    __table_args__ = (
        CheckConstraint(
            "state IN ('PENDING','SENDING','SENT','FAILED')", name="ck_outbox_state"
        ),
        CheckConstraint("attempts >= 0 AND attempts <= 5", name="ck_outbox_attempts"),
        UniqueConstraint(
            "application_id",
            "application_version",
            "recipient_id",
            "notification_type",
            name="uq_outbox_change_recipient_type",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"))
    application_id: Mapped[int] = mapped_column(
        ForeignKey("creator_applications.id", ondelete="CASCADE")
    )
    application_version: Mapped[int]
    recipient_id: Mapped[int] = mapped_column(
        ForeignKey("local_users.id", ondelete="CASCADE")
    )
    notification_type: Mapped[str]
    state: Mapped[str] = mapped_column(default="PENDING")
    attempts: Mapped[int] = mapped_column(default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claimed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_id: Mapped[str | None]
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
