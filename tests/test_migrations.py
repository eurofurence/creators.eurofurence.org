import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session

from app.applications.models import Badge, CreatorApplication, CreatorChannel
from app.creators.models import CreatorProfile
from app.events.models import Event
from app.identity.models import LocalUser

ROOT = Path(__file__).resolve().parents[1]


def test_identity_migration_upgrade_downgrade_and_metadata(tmp_path):
    url = f"sqlite:///{tmp_path / 'migration.db'}"
    env = {**os.environ, "DATABASE_URL": url}

    def alembic(*args):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    alembic("upgrade", "6cf2b87d401a")
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO local_users (id) VALUES (1), (2)"))
    alembic("upgrade", "head")
    with engine.connect() as connection:
        keys = list(connection.scalars(text("SELECT session_key FROM local_users")))
        assert len(set(keys)) == 2 and all(len(key) == 32 for key in keys)
    schema = inspect(engine)
    assert {"events", "local_users", "external_identities"} <= set(
        schema.get_table_names()
    )
    assert any(
        item["column_names"] == ["issuer", "subject"]
        for item in schema.get_unique_constraints("external_identities")
    )
    assert (
        schema.get_foreign_keys("external_identities")[0]["referred_table"]
        == "local_users"
    )
    alembic("check")
    alembic("downgrade", "f1de09a2a1d9")
    assert "local_users" not in inspect(engine).get_table_names()
    assert "events" in inspect(engine).get_table_names()
    alembic("upgrade", "head")
    engine.dispose()


def test_youtube_migration_preserves_data_and_refuses_lossy_downgrade(tmp_path):
    url = f"sqlite:///{tmp_path / 'youtube-migration.db'}"
    env = {**os.environ, "DATABASE_URL": url}

    def migrate(*args, success=True):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert (result.returncode == 0) == success, result.stdout + result.stderr
        return result

    migrate("upgrade", "18df24a910ce")
    engine = create_engine(url)
    now = datetime.now(UTC)
    with Session(engine) as db:
        db.add(
            Event(
                id=1,
                year=2028,
                name="Retained event",
                starts_at=now,
                ends_at=now + timedelta(days=1),
                application_open_at=now,
                application_close_at=now,
                badge_print_at=now,
                badge_change_deadline_at=now,
            )
        )
        db.add(LocalUser(id=1))
        db.flush()
        db.add(
            CreatorApplication(
                id=1,
                user_id=1,
                event_id=1,
                status="APPROVED",
                vlogs=True,
                eligibility_checked_at=now,
            )
        )
        db.flush()
        db.add(CreatorProfile(application_id=1, channel_name="Retained creator"))
        db.add(Badge(application_id=1, event_id=1, badge_number=1))
        db.add(
            CreatorChannel(
                application_id=1,
                platform="Twitch",
                original_representation="@Creator",
                normalized_account="creator",
                canonical_url="https://www.twitch.tv/creator",
                is_primary=True,
            )
        )
        db.commit()

    def retained_rows():
        with engine.connect() as db:
            return {
                table: db.execute(text(f'SELECT * FROM "{table}"')).all()
                for table in (
                    "events",
                    "local_users",
                    "creator_applications",
                    "creator_profiles",
                    "badges",
                    "creator_channels",
                )
            }

    before = retained_rows()
    migrate("upgrade", "head")
    assert retained_rows() == before
    migrate("check")
    migrate("downgrade", "18df24a910ce")
    assert retained_rows() == before
    migrate("upgrade", "head")
    with Session(engine) as db:
        channel = db.scalar(select(CreatorChannel))
        channel.platform = "YouTube"
        channel.canonical_url = "https://www.youtube.com/@creator"
        db.commit()
    before_youtube_downgrade = retained_rows()
    failed = migrate("downgrade", "18df24a910ce", success=False)
    assert "Cannot downgrade while YouTube channels exist" in failed.stderr
    assert retained_rows() == before_youtube_downgrade
    # SQLite may have already downgraded later, empty revisions before the guard.
    migrate("upgrade", "head")
    migrate("check")
    engine.dispose()


def test_registration_evidence_migration_preserves_legacy_rows_without_inventing_proof(
    tmp_path,
):
    url = f"sqlite:///{tmp_path / 'registration-migration.db'}"
    env = {**os.environ, "DATABASE_URL": url}

    def migrate(*args, success=True):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert (result.returncode == 0) == success, result.stdout + result.stderr
        return result

    migrate("upgrade", "5c4d8a2f901e")
    engine = create_engine(url)
    now = datetime.now(UTC)
    with Session(engine) as db:
        db.add(
            Event(
                id=1,
                year=2028,
                name="Legacy",
                starts_at=now,
                ends_at=now,
                application_open_at=now,
                application_close_at=now,
                badge_print_at=now,
                data_delete_at=now,
            )
        )
        db.add(LocalUser(id=1))
        db.flush()
        db.add(
            CreatorApplication(
                id=1,
                user_id=1,
                event_id=1,
                status="APPROVED",
                reg_id="42",
                vlogs=True,
                eligibility_checked_at=now,
            )
        )
        db.flush()
        db.add(CreatorProfile(application_id=1, channel_name="Legacy creator"))
        db.add(Badge(application_id=1, event_id=1, badge_number=1))
        db.commit()
    with engine.connect() as connection:
        before = connection.execute(text("SELECT * FROM creator_applications")).all()
    migrate("upgrade", "head")
    migrate("check")
    with engine.connect() as connection:
        assert (
            connection.execute(text("SELECT * FROM creator_applications")).all()
            == before
        )
        assert (
            connection.scalar(text("SELECT count(*) FROM registration_verifications"))
            == 0
        )
        assert connection.scalar(text("SELECT badge_number FROM badges")) == 1
        assert (
            connection.scalar(text("SELECT channel_name FROM creator_profiles"))
            == "Legacy creator"
        )
    migrate("downgrade", "5c4d8a2f901e")
    migrate("upgrade", "head")
    from app.registration.models import RegistrationVerification

    with Session(engine) as db:
        db.add(
            RegistrationVerification(
                user_id=1,
                event_id=1,
                event_year=2028,
                purpose="creator",
                issuer="https://identity.example/",
                subject="person-1",
                status="PAID",
                reg_id="42",
                verified_at=now,
            )
        )
        db.commit()
    result = migrate("downgrade", "5c4d8a2f901e", success=False)
    assert (
        "Cannot downgrade while Registration verification evidence exists"
        in result.stderr
    )
    migrate("check")
    engine.dispose()
