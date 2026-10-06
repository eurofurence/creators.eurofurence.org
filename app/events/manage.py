"""Explicit operator configuration of event dates; no calendar-year inference."""

import argparse
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.applications.models import BusinessAudit
from app.database import SessionLocal
from app.events.models import Event
from app.helpers import models as helper_models  # noqa: F401
from app.identity import models as identity_models  # noqa: F401


class EventConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int = Field(gt=0)
    year: int = Field(ge=2000, le=9999)
    name: str = Field(min_length=1, max_length=200)
    starts_at: datetime
    ends_at: datetime
    application_open_at: datetime
    application_close_at: datetime
    badge_change_deadline_at: datetime
    badge_print_at: datetime
    data_delete_at: datetime | None = None
    helper_limit: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def dates(self):
        for field in (
            "starts_at",
            "ends_at",
            "application_open_at",
            "application_close_at",
            "badge_change_deadline_at",
            "badge_print_at",
            "data_delete_at",
        ):
            value = getattr(self, field)
            if value is not None:
                if value.tzinfo is None:
                    raise ValueError(
                        "Dates require explicit UTC or Europe/Berlin UTC offset"
                    )
                setattr(self, field, value.astimezone(UTC))
        if (
            self.ends_at < self.starts_at
            or self.application_close_at <= self.application_open_at
        ):
            raise ValueError("Event and application date ranges must be ordered")
        self.data_delete_at = self.data_delete_at or self.ends_at + timedelta(days=30)
        if self.data_delete_at < self.ends_at:
            raise ValueError("Deletion cannot precede the end of the event")
        return self


def configure(db, configuration, reason):
    if not reason.strip() or len(reason) > 4000:
        raise ValueError("A bounded operator reason is required")
    event = db.get(Event, configuration.id, with_for_update=True)
    if event and event.cleanup_started_at is not None:
        raise ValueError("Cleanup has started; its event cannot be reconfigured")
    if event is None:
        event = Event(id=configuration.id)
        db.add(event)
    before = {
        key: str(getattr(event, key, None)) for key in EventConfiguration.model_fields
    }
    for key, value in configuration.model_dump().items():
        setattr(event, key, value)
    db.flush()
    db.add(
        BusinessAudit(
            actor_id=None,
            event_id=event.id,
            entity="event",
            entity_id=event.id,
            action="operator_event_configured",
            reason=reason.strip(),
            changes={
                "fields": [
                    key for key in before if before[key] != str(getattr(event, key))
                ]
            },
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("configuration", type=Path)
    parser.add_argument("--reason", required=True)
    args = parser.parse_args()
    configuration = EventConfiguration.model_validate_json(
        args.configuration.read_text(encoding="utf-8")
    )
    with SessionLocal.begin() as db:
        configure(db, configuration, args.reason)


if __name__ == "__main__":
    main()
