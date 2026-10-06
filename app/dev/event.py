"""Ensure a local browser-test Event without rewriting real event configuration."""

import argparse
import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.dev.setup import ROOT, SetupError, require_ignored, write_values

LOCAL_NAME = "LOCAL browser acceptance"


def select_existing_event(db, configuration, event_id):
    if configuration.environment not in ("development", "test"):
        raise SetupError("Local Event selection requires development/test")
    from app.events.models import Event

    event = db.get(Event, event_id)
    if event is None or event.cleanup_started_at is not None:
        raise SetupError("Selected Event does not exist or cleanup has started")
    return event.id


def choose_existing_event(db, configuration):
    if configuration.environment not in ("development", "test"):
        raise SetupError("Local Event selection requires development/test")
    from app.events.models import Event

    events = db.scalars(select(Event).order_by(Event.year, Event.id)).all()
    if not events:
        return None
    print("Existing Events (selection changes only ACTIVE_EVENT_ID, not Event data):")
    for event in events:
        print(f"  ID {event.id}, year {event.year}, name {json.dumps(event.name)}")
    answer = input("Enter the Event ID to use, or leave blank to cancel: ").strip()
    if not answer.isascii() or not answer.isdecimal():
        raise SetupError("Event selection cancelled; no configuration changed")
    return select_existing_event(db, configuration, int(answer))


def ensure_event(db, configuration, *, refresh=False):
    if configuration.environment not in ("development", "test"):
        raise SetupError("Local Event setup requires development/test")
    from app.events.manage import EventConfiguration, configure
    from app.events.models import Event

    existing = (
        db.get(Event, configuration.active_event_id)
        if configuration.active_event_id
        else None
    )
    if configuration.active_event_id and existing is None:
        raise SetupError(
            "ACTIVE_EVENT_ID does not exist; correct it explicitly before creating local data"
        )
    if existing is not None and not refresh:
        return existing.id
    if existing is not None and existing.name != LOCAL_NAME:
        raise SetupError(
            "Refusing to refresh a non-local Event; use the explicit event management command"
        )
    now = datetime.now(UTC)
    year_event = db.scalar(select(Event).where(Event.year == now.year))
    if existing is None:
        if year_event is not None:
            if year_event.name != LOCAL_NAME:
                raise SetupError(
                    "This year already has a non-local Event; configure ACTIVE_EVENT_ID explicitly"
                )
            existing = year_event
            if not refresh:
                return existing.id
        event_id = (
            existing.id
            if existing
            else (db.scalar(select(func.max(Event.id))) or 0) + 1
        )
    else:
        event_id = existing.id
    configure(
        db,
        EventConfiguration(
            id=event_id,
            year=existing.year if existing else now.year,
            name=LOCAL_NAME,
            starts_at=now + timedelta(days=14),
            ends_at=now + timedelta(days=17),
            application_open_at=now - timedelta(days=1),
            application_close_at=now + timedelta(days=7),
            badge_change_deadline_at=now + timedelta(days=10),
            badge_print_at=now + timedelta(days=11),
            helper_limit=None,
        ),
        "Explicit local browser acceptance setup",
    )
    return event_id


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--refresh",
        action="store_true",
        help="Reset relative dates only for the named local test Event",
    )
    mode.add_argument(
        "--select-id", type=int, help="Select an existing Event without modifying it"
    )
    mode.add_argument(
        "--interactive",
        action="store_true",
        help="Offer existing Events when ACTIVE_EVENT_ID is missing",
    )
    args = parser.parse_args()
    try:
        from app.config import settings

        if settings.environment not in ("development", "test"):
            raise SetupError("Local Event setup requires development/test")
        require_ignored(ROOT, ROOT / ".env")
        from app.database import SessionLocal

        # Register the existing workflow mappings used by Event/audit operations.
        from app.events import manage  # noqa: F401

        with SessionLocal.begin() as db:
            event_id = None
            if args.select_id is not None:
                event_id = select_existing_event(db, settings, args.select_id)
            elif args.interactive and settings.active_event_id is None:
                event_id = choose_existing_event(db, settings)
            if event_id is None:
                event_id = ensure_event(db, settings, refresh=args.refresh)
        write_values(ROOT, {"ACTIVE_EVENT_ID": str(event_id)})
        print(f"Active Event ID: {event_id}")
        return 0
    except SetupError as error:
        print(str(error))
    except Exception:  # noqa: BLE001 - CLI boundary must not dump settings/SQL values.
        print(
            "Local Event setup unavailable; check configuration, database and migrations."
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
