from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class RegistrationVerification(Base):
    """Successful, event-bound evidence only; never OAuth tokens or failed attempts."""

    __tablename__ = "registration_verifications"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "event_id", "purpose", name="uq_registration_verification"
        ),
        CheckConstraint(
            "purpose IN ('creator','helper')", name="ck_verification_purpose"
        ),
        CheckConstraint(
            "status IN ('PAID','CHECKED_IN')", name="ck_verification_status"
        ),
        CheckConstraint("length(trim(reg_id)) > 0", name="ck_verification_reg_id"),
        CheckConstraint(
            "length(issuer) > 0 AND length(subject) > 0",
            name="ck_verification_identity",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("local_users.id", ondelete="CASCADE")
    )
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"))
    event_year: Mapped[int]
    purpose: Mapped[str]
    issuer: Mapped[str]
    subject: Mapped[str]
    status: Mapped[str]
    reg_id: Mapped[str]
    nickname: Mapped[str | None]
    email: Mapped[str | None]
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
