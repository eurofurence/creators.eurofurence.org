from datetime import datetime, timedelta

from sqlalchemy import CheckConstraint, DateTime
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        CheckConstraint(
            "helper_limit IS NULL OR helper_limit >= 0", name="ck_event_helper_limit"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    year: Mapped[int] = mapped_column(unique=True)
    name: Mapped[str]
    helper_limit: Mapped[int | None]

    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    badge_print_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    badge_change_deadline_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    application_open_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    application_close_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    data_delete_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda context: (
            context.get_current_parameters()["ends_at"] + timedelta(days=30)
        ),
    )
    cleanup_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cleanup_failed: Mapped[bool] = mapped_column(default=False, server_default="false")
