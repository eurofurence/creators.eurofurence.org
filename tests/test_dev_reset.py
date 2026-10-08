"""Reset safety guards and domain reset against disposable test schemas only."""

import json
from datetime import UTC, datetime

import pytest
import test_applications as m2
import test_dev as dev
import test_m3 as m3
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.applications.models import (
    Badge,
    BusinessAudit,
    CreatorApplication,
    LocalRoleAssignment,
)
from app.applications.workflow import utc, window_open
from app.config import settings
from app.creators.images import ImageStorageUnavailable
from app.creators.models import BannedChannel, CreatorProfile, ProfileImage
from app.dev import reset
from app.dev.setup import SetupError
from app.events.models import Event
from app.helpers.models import HelperRegistration
from app.identity.models import ExternalIdentity, LocalUser
from app.registration.manual import load_manual_records
from app.staff.service import require_staff

application_engine = m2.application_engine
postgres_engine = m2.postgres_engine
local_repository = dev.local_repository


def test_reset_store_deletes_only_the_requested_key_and_reports_failure():
    from botocore.exceptions import ClientError

    calls = []

    class Client:
        def delete_object(self, **kwargs):
            calls.append(kwargs)
            if kwargs["Key"] == "failed":
                raise ClientError({"Error": {"Code": "Unavailable"}}, "DeleteObject")

    store = reset._ResetStore(Client(), "creators")
    store.delete("profiles/owned.png")
    assert calls == [{"Bucket": "creators", "Key": "profiles/owned.png"}]
    with pytest.raises(ImageStorageUnavailable):
        store.delete("failed")


def configuration(root):
    manual = root / "temp" / "manual-registration.json"
    manual.parent.mkdir(exist_ok=True)
    manual.write_text(
        json.dumps(
            [
                {
                    "issuer": settings.oidc_issuer_url,
                    "subject": "person-2",
                    "event_id": 1,
                    "event_year": 2028,
                    "status": "PAID",
                    "reg_id": "LOCAL-42",
                    "nickname": "Test",
                }
            ]
        ),
        encoding="utf-8",
    )
    return settings.model_copy(
        update={
            "environment": "development",
            "database_url": SecretStr(
                "postgresql+psycopg://creators:private@127.0.0.1:5432/creators"
            ),
            "s3_endpoint_url": "http://127.0.0.1:9090",
            "s3_bucket": "creators",
            "registration_provider": "manual_test",
            "registration_manual_file": manual,
            "active_event_id": 1,
        }
    )


@pytest.mark.parametrize(
    "environment", ["test", "production", "staging", "Development", ""]
)
def test_reset_refuses_every_non_development_environment(local_repository, environment):
    config = configuration(local_repository)
    config.environment = environment
    with pytest.raises(SetupError, match="ENVIRONMENT=development"):
        reset.reset_local(
            config, user_id=2, name="EF DEV TEST 2027", year=2027, confirm=True
        )


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg://creators:private@remote.example:5432/creators",
        "postgresql+psycopg://creators:private@127.0.0.1:5432/production",
        "postgresql+psycopg://creators:private@127.0.0.1:5432/staging",
        "postgresql+psycopg://creators:private@127.0.0.1:55439/creators",
        "postgresql+psycopg://creators:private@127.0.0.1:5432/creators?host=remote.example",
        "postgresql+psycopg://creators:private@127.0.0.1:5432/creators?service=production",
        "postgresql+psycopg://postgres:private@127.0.0.1:5432/creators",
        "postgresql+psycopg://creators:private@localhost.production:5432/creators",
        "sqlite:///local.sqlite",
        "postgresql+psycopg:///creators",
    ],
)
def test_reset_refuses_suspicious_db_before_connecting(
    local_repository, url, monkeypatch
):
    config = configuration(local_repository)
    config.database_url = SecretStr(url)
    monkeypatch.setattr(
        reset, "create_engine", lambda *a, **k: pytest.fail("Must not connect")
    )
    with pytest.raises(SetupError, match="targets") as error:
        reset.reset_local(
            config, user_id=2, name="EF DEV TEST 2027", year=2027, confirm=True
        )
    assert "private" not in str(error.value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("s3_endpoint_url", "https://s3.example.com"),
        ("s3_bucket", "production"),
        ("active_event_id", None),
        ("registration_provider", "unavailable"),
    ],
)
def test_reset_refuses_unexpected_other_configuration(local_repository, field, value):
    config = configuration(local_repository)
    setattr(config, field, value)
    with pytest.raises(SetupError):
        reset.validate_target(config)


def test_reset_preserves_identity_admin_and_scopes_staff_to_fresh_event(
    application_engine, local_repository
):
    config = configuration(local_repository)
    assert reset.validate_target(config).database == "creators"
    env_file = local_repository / ".env"
    env_file.write_bytes(b"private configuration stays byte-for-byte unchanged\n")
    before_env = env_file.read_bytes()
    store = m2.SubmissionStore()
    store.objects["unrelated-object"] = b"must remain"
    with Session(application_engine, expire_on_commit=False) as db:
        own = m3.approve(db, 2)
        other = m3.approve(db, 1)
        helper = m3.register(db, other, user=2, actor=1)
        m3.decide(db, helper, actor=1)
        assert db.get(CreatorProfile, own).image_id
        now = datetime.now(UTC)
        db.add(
            BannedChannel(
                platform="Twitch",
                normalized_account="banned",
                private_reason="Keep moderation",
                created_at=now,
                updated_at=now,
            )
        )
        db.add(
            BusinessAudit(
                entity="local_role", entity_id=2, action="operator_admin_grant"
            )
        )
        db.commit()
        before_users = list(db.execute(select(LocalUser.id, LocalUser.session_key)))
        before_identities = list(
            db.execute(
                select(
                    ExternalIdentity.id,
                    ExternalIdentity.user_id,
                    ExternalIdentity.issuer,
                    ExternalIdentity.subject,
                )
            )
        )
        image_keys = list(db.scalars(select(ProfileImage.object_key)))
        store.objects.update({key: b"owned test picture" for key in image_keys})
        plan = reset._prepare(db, config, 2, "EF DEV TEST 2027", 2027, local_repository)
        # A preview has not changed any rows or manual fixtures.
        assert db.get(CreatorApplication, own).status == "APPROVED"
        assert (
            next(iter(load_manual_records(config.registration_manual_file))).event_year
            == 2028
        )
        reset._execute(db, store, plan)
        db.expire_all()
        assert (
            list(db.execute(select(LocalUser.id, LocalUser.session_key)))
            == before_users
        )
        assert (
            list(
                db.execute(
                    select(
                        ExternalIdentity.id,
                        ExternalIdentity.user_id,
                        ExternalIdentity.issuer,
                        ExternalIdentity.subject,
                    )
                )
            )
            == before_identities
        )
        assert db.scalar(
            select(LocalRoleAssignment.id).where(
                LocalRoleAssignment.user_id == 2, LocalRoleAssignment.role == "ADMIN"
            )
        )
        require_staff(db, 2, 1)
        staff = db.scalars(
            select(LocalRoleAssignment).where(LocalRoleAssignment.role == "BADGE_STAFF")
        ).all()
        assert [(r.user_id, r.event_id) for r in staff] == [(2, 1)]
        for model in (
            CreatorApplication,
            CreatorProfile,
            Badge,
            HelperRegistration,
            ProfileImage,
        ):
            assert db.scalar(select(func.count()).select_from(model)) == 0
        event = db.get(Event, 1)
        assert event.name == "EF DEV TEST 2027" and event.year == 2027
        assert db.scalar(select(func.count()).select_from(Event)) == 1
        assert window_open(event, now)
        assert (
            now
            < utc(event.application_close_at)
            < utc(event.badge_change_deadline_at)
            < utc(event.badge_print_at)
            < utc(event.starts_at)
            < utc(event.ends_at)
        )
        assert (
            event.helper_limit is None
            and (event.data_delete_at - event.ends_at).days == 30
        )
        assert db.scalar(select(func.count()).select_from(BannedChannel)) == 1
        assert db.scalar(
            select(BusinessAudit.id).where(
                BusinessAudit.action == "operator_admin_grant"
            )
        )
        assert store.objects == {"unrelated-object": b"must remain"}
        manual = next(
            iter(load_manual_records(config.registration_manual_file).values())
        )
        assert manual.event_year == 2027 and manual.event_id == config.active_event_id
        assert manual.reg_id == "LOCAL-42" and manual.subject == "person-2"
        assert env_file.read_bytes() == before_env


def test_reset_image_failure_retains_references_for_retry(
    application_engine, local_repository
):
    config = configuration(local_repository)
    with Session(application_engine, expire_on_commit=False) as db:
        app_id = m3.approve(db, 2)
        plan = reset._prepare(db, config, 2, "EF DEV TEST 2027", 2027, local_repository)

        class FailedStore:
            def delete(self, key):
                raise ImageStorageUnavailable

        with pytest.raises(SetupError, match="references retained"):
            reset._execute(db, FailedStore(), plan)
        db.expire_all()
        assert db.scalar(select(ProfileImage)).deletion_failed
        assert db.get(CreatorApplication, app_id)
        assert config.registration_manual_file.read_bytes() == plan["original_manual"]
        assert db.get(ExternalIdentity, plan["identity_id"])
        # The same normal cleanup logic can safely retry a partially completed reset.
        retry = reset._prepare(
            db, config, 2, "EF DEV TEST 2027", 2027, local_repository
        )
        reset._execute(db, m2.SubmissionStore(), retry)
        assert db.scalar(select(ProfileImage.id)) is None
        assert db.scalar(select(CreatorApplication.id)) is None


def test_reset_refuses_arbitrary_user_and_unclear_object_ownership(
    application_engine, local_repository
):
    config = configuration(local_repository)
    with Session(application_engine, expire_on_commit=False) as db:
        with pytest.raises(SetupError, match="existing local ADMIN"):
            reset._prepare(db, config, 1, "EF DEV TEST 2027", 2027, local_repository)
        m3.approve(db, 2)
        db.scalar(select(ProfileImage)).object_key = "unrelated/asset.png"
        db.commit()
        with pytest.raises(SetupError, match="unclear ownership"):
            reset._prepare(db, config, 2, "EF DEV TEST 2027", 2027, local_repository)
        assert not db.get(Event, 1).cleanup_started_at
