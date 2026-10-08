"""Reset host-side LOCAL development workflow data, preserving login identities.

Stop the application and workers first. Without --confirm-local-reset this is a
read-only preview. This command deliberately supports only the repository's
loopback PostgreSQL/S3 development setup, never arbitrary database targets.
"""

import argparse
import json
import os
import re
import socket
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine, delete, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.dev.setup import ROOT, SetupError, require_ignored


def validate_target(configuration):
    if configuration.environment != "development":
        raise SetupError("Reset requires ENVIRONMENT=development")
    try:
        url = make_url(configuration.database_url.get_secret_value())
    except AttributeError, ValueError:
        raise SetupError(
            "Reset requires the expected local PostgreSQL database"
        ) from None
    if (
        url.drivername != "postgresql+psycopg"
        or url.host not in ("127.0.0.1", "::1")
        or url.port != 5432
        or url.database != "creators"
        or url.username != "creators"
        or url.query
    ):
        raise SetupError("Reset refuses non-local or unexpected PostgreSQL targets")
    if (
        configuration.s3_endpoint_url != "http://127.0.0.1:9090"
        or configuration.s3_bucket != "creators"
    ):
        raise SetupError("Reset requires the expected local S3 endpoint and bucket")
    if (
        configuration.active_event_id is None
        or configuration.registration_provider != "manual_test"
    ):
        raise SetupError("Reset requires an active Event and manual_test Registration")
    return url


def _prepare(db, configuration, user_id, name, year, root):
    """Read-only plan, called only after the command's target guard."""
    from app.applications.models import LocalRoleAssignment, NotificationOutbox
    from app.creators.models import ProfileImage
    from app.events.manage import EventConfiguration
    from app.events.models import Event
    from app.identity.models import ExternalIdentity, LocalUser
    from app.registration.manual import load_manual_records

    if (
        db.get(LocalUser, user_id) is None
        or db.scalar(
            select(LocalRoleAssignment.id).where(
                LocalRoleAssignment.user_id == user_id,
                LocalRoleAssignment.role == "ADMIN",
                LocalRoleAssignment.event_id.is_(None),
            )
        )
        is None
    ):
        raise SetupError("Select an existing local ADMIN; reset never grants ADMIN")
    identity = db.scalar(
        select(ExternalIdentity).where(
            ExternalIdentity.user_id == user_id,
            ExternalIdentity.issuer == configuration.oidc_issuer_url,
        )
    )
    event = db.get(Event, configuration.active_event_id)
    if identity is None or event is None:
        raise SetupError("Retained OIDC identity or active Event is missing")
    now = datetime.now(UTC)
    if (
        db.scalar(
            select(NotificationOutbox.id).where(
                NotificationOutbox.state == "SENDING",
                NotificationOutbox.claimed_until > now,
            )
        )
        is not None
        or db.scalar(
            select(ProfileImage.id).where(
                ProfileImage.state == "STAGED", ProfileImage.delete_after > now
            )
        )
        is not None
    ):
        raise SetupError(
            "Wait for in-flight uploads/notifications to finish before reset"
        )
    images = db.scalars(select(ProfileImage)).all()
    if any(
        not re.fullmatch(r"profiles/[0-9a-f]{32}\.png", i.object_key) for i in images
    ):
        raise SetupError("Reset refuses image objects with unclear ownership")
    manual_path = Path(configuration.registration_manual_file)
    if not manual_path.is_absolute():
        manual_path = root / manual_path
    require_ignored(root, manual_path)
    original_manual = manual_path.read_bytes()
    records = list(load_manual_records(manual_path).values())
    matches = [
        r
        for r in records
        if (r.issuer, r.subject, r.event_id)
        == (identity.issuer, identity.subject, event.id)
    ]
    if len(matches) != 1 or matches[0].status not in ("PAID", "CHECKED_IN"):
        raise SetupError(
            "Retained account needs one eligible manual Registration fixture"
        )
    matches[0].event_year = year
    manual_data = (
        json.dumps([r.model_dump() for r in records], indent=2) + "\n"
    ).encode()
    # Normal Event validation/default retention; no production defaults changed.
    starts = max(now + timedelta(days=60), datetime(year, 1, 15, tzinfo=UTC))
    fresh = EventConfiguration(
        id=event.id,
        year=year,
        name=name,
        application_open_at=now - timedelta(days=1),
        application_close_at=now + timedelta(days=30),
        badge_change_deadline_at=starts - timedelta(days=7),
        badge_print_at=starts - timedelta(days=6),
        starts_at=starts,
        ends_at=starts + timedelta(days=3),
        helper_limit=None,
    )
    # Do not infer ownership of cross-event bans. They are preserved.
    return {
        "event": fresh,
        "user_id": user_id,
        "identity_id": identity.id,
        "manual_path": manual_path,
        "original_manual": original_manual,
        "manual_data": manual_data,
        "counts": {
            table.name: db.scalar(select(func.count()).select_from(table))
            for table in Event.metadata.sorted_tables
            if table.name
            not in (
                "local_users",
                "external_identities",
                "local_role_assignments",
                "banned_channels",
                "business_audit",
            )
        },
    }


def _write_manual(path, data):
    descriptor, temporary = tempfile.mkstemp(prefix=".reset-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class _ResetStore:
    def __init__(self, client, bucket):
        self.client, self.bucket = client, bucket

    def delete(self, key):
        from app.creators.images import S3ImageStore

        S3ImageStore._call(
            lambda: self.client.delete_object(Bucket=self.bucket, Key=key)
        )


def _execute(db, store, plan):
    """Guarded command implementation; retain image references on cleanup failure."""
    from app.applications.models import LocalRoleAssignment
    from app.creators.images import cleanup_images
    from app.creators.models import ProfileImage
    from app.events.manage import configure
    from app.events.models import Event
    from app.helpers.models import RedemptionThrottle
    from app.identity.models import ExternalIdentity
    from app.identity.roles import set_role

    db.rollback()
    with db.begin():
        now = datetime.now(UTC)
        for event in db.scalars(select(Event).with_for_update()):
            event.cleanup_started_at = now
        for image in db.scalars(select(ProfileImage).with_for_update()):
            image.state, image.delete_after = "DELETE", now
    if cleanup_images(db, store):
        raise SetupError(
            "Image cleanup incomplete; references retained. Retry this reset"
        )
    db.rollback()
    if db.scalar(select(ProfileImage.id).limit(1)) is not None:
        raise SetupError(
            "Image cleanup incomplete; references retained. Retry this reset"
        )
    db.rollback()
    if plan["manual_path"].read_bytes() != plan["original_manual"]:
        raise SetupError("Manual Registration changed during reset; preview and retry")
    _write_manual(plan["manual_path"], plan["manual_data"])
    try:
        with db.begin():
            # FK cascades remove only Event-owned workflow data. Identity rows,
            # global ADMIN roles/audits and cross-event bans remain untouched.
            db.execute(delete(Event))
            db.execute(delete(RedemptionThrottle))
            configure(db, plan["event"], "Explicit local development workflow reset")
            identity = db.get(ExternalIdentity, plan["identity_id"])
            set_role(
                db,
                identity.issuer,
                identity.subject,
                True,
                "Preserve retained operator access after local development reset",
                event_id=plan["event"].id,
            )
            if (
                db.scalar(
                    select(LocalRoleAssignment.id).where(
                        LocalRoleAssignment.user_id == plan["user_id"],
                        LocalRoleAssignment.role == "ADMIN",
                        LocalRoleAssignment.event_id.is_(None),
                    )
                )
                is None
            ):
                raise SetupError("Retained ADMIN role changed during reset; retry")
    except Exception:
        _write_manual(plan["manual_path"], plan["original_manual"])
        raise


def reset_local(configuration, *, user_id, name, year, confirm=False, root=ROOT):
    url = validate_target(configuration)
    # Register all domain mappings without invoking retention's identity removal.
    from app.main import app  # noqa: F401
    from app.storage import get_s3_client

    engine = create_engine(
        url, hide_parameters=True, connect_args={"connect_timeout": 3}
    )
    try:
        with Session(engine) as db:
            actual = db.execute(text("SELECT current_database(), current_user")).one()
            if tuple(actual) != ("creators", "creators"):
                raise SetupError("Connected database does not match local development")
            plan = _prepare(db, configuration, user_id, name, year, root)
            print("Local development reset preview:")
            for label, count in plan["counts"].items():
                print(f"  {label}: {count}")
            print(
                f"Retained local ADMIN: {user_id}; BADGE_STAFF will cover the fresh Event"
            )
            print(f"Fresh active Event: {name} ({year}); existing active ID retained")
            print(
                "Identities, global ADMIN roles, global audits, bans and .env are preserved"
            )
            if not confirm:
                print(
                    "Preview only. Stop the app/workers, then use --confirm-local-reset"
                )
                return
            try:
                with socket.create_connection(("127.0.0.1", 8000), timeout=1):
                    raise SetupError(
                        "Stop the local application on port 8000 before reset"
                    )
            except ConnectionRefusedError, TimeoutError:
                pass
            client = get_s3_client(configuration)
            client.head_bucket(Bucket=configuration.s3_bucket)

            _execute(db, _ResetStore(client, configuration.s3_bucket), plan)
            print("Reset completed. Manual Registration retargeted; .env unchanged")
    finally:
        engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", type=int, required=True)
    parser.add_argument("--event-name", required=True)
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--confirm-local-reset", action="store_true")
    args = parser.parse_args()
    try:
        from app.config import settings

        reset_local(
            settings,
            user_id=args.user_id,
            name=args.event_name,
            year=args.year,
            confirm=args.confirm_local_reset,
        )
        return 0
    except SetupError as error:
        print(str(error))
    except Exception:  # noqa: BLE001 - never disclose settings, SQL or identity values.
        print(
            "Local reset incomplete. Check local services/configuration and retry; no secrets printed"
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
