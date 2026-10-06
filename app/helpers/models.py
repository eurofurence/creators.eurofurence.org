from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.applications.models import utcnow
from app.database import Base


class HelperInvitation(Base):
    __tablename__ = "helper_invitations"
    __table_args__ = (
        UniqueConstraint(
            "id", "application_id", "event_id", name="uq_invitation_context"
        ),
        ForeignKeyConstraint(
            ["application_id", "event_id"],
            ["creator_applications.id", "creator_applications.event_id"],
            ondelete="CASCADE",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int]
    event_id: Mapped[int]
    token_digest: Mapped[str] = mapped_column(unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consumed_by: Mapped[int | None] = mapped_column(
        ForeignKey("local_users.id", ondelete="SET NULL")
    )


class HelperRegistration(Base):
    __tablename__ = "helper_registrations"
    __table_args__ = (
        UniqueConstraint("application_id", "user_id", name="uq_helper_relationship"),
        UniqueConstraint("id", "event_id", name="uq_helper_event"),
        CheckConstraint("user_id <> creator_user_id", name="ck_helper_not_self"),
        CheckConstraint(
            "status IN ('PENDING','CONFIRMED','DECLINED')", name="ck_helper_status"
        ),
        CheckConstraint("version > 0", name="ck_helper_version"),
        ForeignKeyConstraint(
            ["application_id", "event_id", "creator_user_id"],
            [
                "creator_applications.id",
                "creator_applications.event_id",
                "creator_applications.user_id",
            ],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["invitation_id", "application_id", "event_id"],
            [
                "helper_invitations.id",
                "helper_invitations.application_id",
                "helper_invitations.event_id",
            ],
            ondelete="CASCADE",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int]
    event_id: Mapped[int]
    creator_user_id: Mapped[int]
    user_id: Mapped[int] = mapped_column(
        ForeignKey("local_users.id", ondelete="CASCADE")
    )
    invitation_id: Mapped[int] = mapped_column(unique=True)
    status: Mapped[str] = mapped_column(default="PENDING")
    version: Mapped[int] = mapped_column(default=1)
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reg_id: Mapped[str | None]
    nickname: Mapped[str | None]
    email: Mapped[str | None]
    eligibility_checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class RedemptionThrottle(Base):
    __tablename__ = "redemption_throttles"
    user_id: Mapped[int] = mapped_column(
        ForeignKey("local_users.id", ondelete="CASCADE"), primary_key=True
    )
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int]
