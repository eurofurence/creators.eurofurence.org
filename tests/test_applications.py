import asyncio
import base64
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from io import BytesIO
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from PIL import Image
from sqlalchemy import create_engine, delete, func, select, text
from sqlalchemy import event as sql_event
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.applications import workflow
from app.applications.input import ApplicationInput, https_url, normalize_channel
from app.applications.models import (
    Badge,
    BadgeCounter,
    BusinessAudit,
    CreatorApplication,
    CreatorChannel,
    LocalRoleAssignment,
    NotificationOutbox,
)
from app.config import settings
from app.creators import images
from app.database import Base, get_db
from app.events.models import Event
from app.identity.models import ExternalIdentity, LocalUser
from app.identity.profile import get_identity_profile_client
from app.identity.roles import set_admin
from app.main import app
from app.registration.client import (
    RegistrationResult,
    RegistrationStatus,
    RegistrationUnavailable,
    get_registration_client,
)


class RegistrationFake:
    status = RegistrationStatus.PAID
    failure = None
    calls = 0
    hook = None

    async def lookup(self, lookup):
        self.calls += 1
        if self.hook:
            self.hook()
        if self.failure:
            raise self.failure
        return RegistrationResult(lookup, self.status, "REG-123", "Test attendee")


class IdentityFake:
    async def email(self, identity):
        return "attendee@example.test"


@lru_cache
def application_picture():
    output = BytesIO()
    Image.new("RGB", (600, 600), "blue").save(output, format="PNG")
    return output.getvalue()


class SubmissionStore:
    def __init__(self):
        self.objects = {}

    def put(self, key, data):
        self.objects[key] = data

    def get(self, key):
        if key not in self.objects:
            raise images.ImageStorageUnavailable
        return self.objects[key]

    def delete(self, key):
        self.objects.pop(key, None)


def picture_input():
    return {"picture": application_picture(), "store": SubmissionStore()}


def input_data(handle="@creator"):
    return ApplicationInput(
        ("VLOGS",), (normalize_channel("Twitch", handle, True),), ()
    )


@pytest.fixture(scope="session")
def postgres_engine():
    url = os.environ.get("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Set TEST_POSTGRES_URL to run PostgreSQL integration verification")
    admin_engine = create_engine(url)
    schema = "test_creators_" + uuid4().hex
    with admin_engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    scoped_url = make_url(url).update_query_dict({"options": f"-csearch_path={schema}"})
    engine = create_engine(scoped_url)
    env = {
        **os.environ,
        "DATABASE_URL": scoped_url.render_as_string(hide_password=False),
    }
    try:
        for command in (
            ("upgrade", "head"),
            ("check",),
            ("downgrade", "a2c4e6f81012"),
            ("upgrade", "head"),
            ("check",),
        ):
            result = subprocess.run(
                [sys.executable, "-m", "alembic", *command],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            assert result.returncode == 0, result.stdout + result.stderr
        yield engine
    finally:
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin_engine.dispose()


@pytest.fixture(params=["sqlite", "postgres"])
def application_engine(request, monkeypatch):
    monkeypatch.setattr(settings, "active_event_id", 1)
    if request.param == "postgres":
        engine = request.getfixturevalue("postgres_engine")
        with engine.begin() as connection:
            for table in reversed(Base.metadata.sorted_tables):
                connection.execute(table.delete())
    else:
        engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )

        @sql_event.listens_for(engine, "connect")
        def foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")

        Base.metadata.create_all(engine)
    now = datetime.now(UTC)
    with Session(engine) as db:
        db.add(
            Event(
                id=1,
                year=2028,
                name="Test EF",
                starts_at=now,
                ends_at=now + timedelta(days=2),
                application_open_at=now - timedelta(days=1),
                application_close_at=now + timedelta(days=1),
                badge_print_at=now + timedelta(days=2),
                badge_change_deadline_at=now + timedelta(days=1),
                data_delete_at=now + timedelta(days=32),
            )
        )
        db.add_all([LocalUser(id=i) for i in (1, 2, 3)])
        db.flush()
        db.add_all(
            [
                ExternalIdentity(
                    user_id=i, issuer=settings.oidc_issuer_url, subject=f"person-{i}"
                )
                for i in (1, 2, 3)
            ]
        )
        db.add(LocalRoleAssignment(user_id=2, role="ADMIN"))
        db.add(LocalRoleAssignment(user_id=3, role="BADGE_STAFF", event_id=1))
        db.commit()
    yield engine
    if request.param == "sqlite":
        engine.dispose()


def login(client, user_id):
    client.cookies.clear()
    database = app.dependency_overrides[get_db]()
    db = next(database)
    try:
        identity_key = db.get(LocalUser, user_id).session_key
    finally:
        database.close()
    payload = base64.b64encode(
        json.dumps({"user_id": user_id, "identity_key": identity_key}).encode()
    )
    client.cookies.set(
        "creator_session",
        TimestampSigner(settings.session_secret.get_secret_value())
        .sign(payload)
        .decode(),
    )


def csrf(client, path):
    response = client.get(path)
    assert response.status_code == 200, response.text
    return re.search(r'name="csrf_token"[^>]*value="([^"]+)"', response.text)[1]


@pytest.fixture
def browser(application_engine):
    registration = RegistrationFake()
    store = SubmissionStore()

    def database():
        with Session(application_engine, expire_on_commit=False) as db:
            yield db

    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_registration_client] = lambda: registration
    app.dependency_overrides[get_identity_profile_client] = IdentityFake
    app.dependency_overrides[images.get_image_store] = lambda: store
    with TestClient(app) as client:
        login(client, 1)
        yield client, registration, application_engine
    app.dependency_overrides.clear()


def submit_form(client, **changes):
    data = {
        "csrf_token": csrf(client, "/applications/form"),
        "content_type": ["VLOGS", "SHORTS"],
        "platform": ["Twitch", "Mastodon"],
        "account": ["@Creator", "https://social.example/@Other"],
        "primary": "0",
        "videos": "https://video.example/watch?v=123",
    }
    data.update(changes)
    return client.post(
        "/applications/form",
        data=data,
        files={"picture": ("picture.png", application_picture(), "image/png")},
        follow_redirects=False,
    )


def submit_record(engine, user=1):
    with Session(engine, expire_on_commit=False) as db:
        return asyncio.run(
            workflow.submit(
                db,
                user,
                input_data(),
                RegistrationFake(),
                IdentityFake(),
                **picture_input(),
            )
        )


def review_record(engine, record_id, version, target, **options):
    with Session(engine, expire_on_commit=False) as db:
        return asyncio.run(
            workflow.review(
                db,
                options.pop("actor", 2),
                record_id,
                version,
                target,
                options.pop("reason", "Review reason"),
                "private notes",
                options.pop("exceptional", False),
                options.pop("registration", RegistrationFake()),
                IdentityFake(),
            )
        )


@pytest.mark.parametrize(
    "platform,value,account",
    [
        ("Twitch", "@Creator", "creator"),
        ("X", "https://twitter.com/Creator/", "creator"),
        ("Bluesky", "@Creator.bsky.social", "creator.bsky.social"),
        ("Facebook", "https://www.facebook.com/profile.php?id=123", "id:123"),
        ("Instagram", "https://instagram.com/Creator/", "creator"),
        ("Threads", "https://threads.net/@Creator", "creator"),
        ("TikTok", "@Creator", "creator"),
        ("Mastodon", "https://SOCIAL.example/@Creator", "creator@social.example"),
    ],
)
def test_platform_normalization(platform, value, account):
    channel = normalize_channel(platform, value, True)
    assert channel.original_representation == value
    assert channel.normalized_account == account
    assert channel.canonical_url.startswith("https://")


@pytest.mark.parametrize(
    "platform,value",
    [
        ("Twitch", "http://twitch.tv/test"),
        ("Twitch", "https://evil.test/test"),
        ("X", "https://x.com/test/status/123"),
        ("Mastodon", "@test"),
        ("Mastodon", "https://user:pass@social.example/@test"),
        ("YouTube", "test"),
        ("Bluesky", "short"),
        ("Twitch", "javascript:alert(1)"),
        ("Twitch", "https://twitch.tv:444/test"),
    ],
)
def test_bad_channel_input(platform, value):
    with pytest.raises(ValueError):
        normalize_channel(platform, value, True)


@pytest.mark.parametrize(
    "value",
    [
        "http://video.test/",
        "javascript:alert(1)",
        "https://u:p@video.test/",
        "https://video.test/\nfoo",
        "https://video.test\\@evil.test",
        "https://",
    ],
)
def test_bad_video_url(value):
    with pytest.raises(ValueError):
        https_url(value)


def test_form_submission_dashboard_edit_and_identity_privacy(browser):
    client, registration, engine = browser
    assert submit_form(client).status_code == 303
    page = client.get("/applications")
    assert page.status_code == 200 and "NEW" in page.text
    assert "attendee@example.test" not in page.text and "REG-123" not in page.text
    assert page.headers["cache-control"] == "no-store"
    with Session(engine) as db:
        record = db.scalar(select(CreatorApplication))
        record_id = record.id
        assert record.reg_id == "REG-123" and record.nickname == "Test attendee"
        assert db.scalar(select(func.count()).select_from(CreatorChannel)) == 2
    response = submit_form(
        client,
        application_id=str(record_id),
        version="1",
        videos="",
        account=["@Changed", "https://social.example/@Other"],
    )
    assert response.status_code == 303
    assert registration.calls == 1  # Editing NEW does not submit a second application.
    login(client, 2)
    admin = client.get(f"/admin/applications/{record_id}")
    assert all(
        v in admin.text
        for v in ("REG-123", "Test attendee", "attendee@example.test", "@Changed")
    )


@pytest.mark.parametrize(
    "change",
    [
        {"content_type": []},
        {"primary": "99"},
        {"videos": "\n".join(["https://video.example/x"] * 11)},
        {"account": ["@same", "@same"], "platform": ["Twitch", "Twitch"]},
        {"videos": "http://video.example/x"},
    ],
)
def test_invalid_form_preserves_input_without_writes(browser, change):
    client, _, engine = browser
    response = submit_form(client, **change)
    assert 'role="alert"' in response.text
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(CreatorApplication)) == 0


def test_csrf_ownership_roles_and_current_role_revocation(browser):
    client, _, engine = browser
    assert client.post("/applications/form", data={}).status_code == 403
    assert submit_form(client).status_code == 303
    with Session(engine) as db:
        record_id = db.scalar(select(CreatorApplication.id))
    assert client.get("/admin/applications").status_code == 403
    login(client, 3)
    assert client.get("/admin/applications").status_code == 403
    assert (
        submit_form(client, application_id=str(record_id), version="1").status_code
        == 404
    )
    login(client, 2)
    token = csrf(client, f"/admin/applications/{record_id}")
    with Session(engine) as db:
        db.execute(
            delete(LocalRoleAssignment).where(LocalRoleAssignment.role == "ADMIN")
        )
        db.commit()
    assert (
        client.post(
            f"/admin/applications/{record_id}",
            data={"csrf_token": token, "version": "1", "status": "ON_REVIEW"},
        ).status_code
        == 403
    )
    client.cookies.clear()
    assert client.get("/applications").status_code == 401


def test_workflow_side_effects_reserved_badge_and_edit_locks(application_engine):
    record_id = submit_record(application_engine)
    review_record(application_engine, record_id, 1, "ON_REVIEW")
    with (
        Session(application_engine) as db,
        pytest.raises(HTTPException, match="locked"),
    ):
        workflow.edit(db, 1, record_id, 2, input_data())
    review_record(application_engine, record_id, 2, "APPROVED")
    review_record(application_engine, record_id, 3, "ON_REVIEW")
    review_record(application_engine, record_id, 4, "APPROVED")
    with Session(application_engine) as db:
        assert db.scalar(select(Badge.badge_number)) == 1
        assert db.scalar(select(BadgeCounter.next_number)) == 2
        assert db.scalar(select(func.count()).select_from(NotificationOutbox)) == 3
        assert (
            db.scalar(
                select(func.count())
                .select_from(BusinessAudit)
                .where(BusinessAudit.action == "badge_allocated")
            )
            == 1
        )
        assert db.get(CreatorApplication, record_id).status == "APPROVED"


@pytest.mark.parametrize(
    "state,code",
    [(RegistrationStatus.UNKNOWN, 503), (RegistrationStatus.INELIGIBLE, 409)],
)
def test_submission_and_approval_fail_closed_without_state_changes(
    application_engine, state, code
):
    fake = RegistrationFake()
    fake.status = state
    with Session(application_engine) as db:
        with pytest.raises(HTTPException) as error:
            asyncio.run(
                workflow.submit(
                    db, 1, input_data(), fake, IdentityFake(), **picture_input()
                )
            )
        assert error.value.status_code == code
    record_id = submit_record(application_engine)
    review_record(application_engine, record_id, 1, "ON_REVIEW")
    with pytest.raises(HTTPException) as error:
        review_record(application_engine, record_id, 2, "APPROVED", registration=fake)
    assert error.value.status_code == code
    with Session(application_engine) as db:
        assert db.get(CreatorApplication, record_id).status == "ON_REVIEW"
        assert db.scalar(select(func.count()).select_from(Badge)) == 0
        assert db.scalar(select(func.count()).select_from(NotificationOutbox)) == 0


def test_rejection_reason_reopen_stale_review_and_self_review(application_engine):
    record_id = submit_record(application_engine, user=2)
    with pytest.raises(HTTPException):
        review_record(application_engine, record_id, 1, "APPROVED")
    with pytest.raises(HTTPException):
        review_record(application_engine, record_id, 1, "NOT_ACCEPTED", reason=" ")
    review_record(application_engine, record_id, 1, "NOT_ACCEPTED")
    assert submit_record(application_engine, user=2) == record_id
    review_record(application_engine, record_id, 2, "ON_REVIEW")
    with pytest.raises(HTTPException):
        review_record(application_engine, record_id, 2, "APPROVED")
    review_record(application_engine, record_id, 3, "APPROVED")


def test_window_deadline_and_exceptional_admin_correction(application_engine):
    record_id = submit_record(application_engine)
    with Session(application_engine) as db:
        event = db.get(Event, 1)
        event.application_close_at = datetime.now(UTC) - timedelta(seconds=1)
        event.badge_change_deadline_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()
        with pytest.raises(HTTPException):
            workflow.edit(db, 1, record_id, 1, input_data())
    review_record(
        application_engine, record_id, 1, "ON_REVIEW"
    )  # Review after close remains possible.
    with pytest.raises(HTTPException):
        review_record(application_engine, record_id, 2, "APPROVED")
    review_record(application_engine, record_id, 2, "APPROVED", exceptional=True)
    with Session(application_engine) as db:
        workflow.edit(
            db,
            2,
            record_id,
            3,
            input_data("@corrected"),
            administrative=True,
            reason="Corrected input",
        )
        assert db.scalar(select(CreatorChannel.normalized_account)) == "corrected"


def test_boundaries():
    now = datetime.now(UTC)
    event = Event(
        application_open_at=now, application_close_at=now + timedelta(seconds=1)
    )
    assert workflow.window_open(event, now)
    assert not workflow.window_open(event, now + timedelta(seconds=1))
    assert not workflow.window_open(event, now - timedelta(seconds=1))


def test_outside_transaction_lookup_and_role_recheck(application_engine):
    record_id = submit_record(application_engine)
    review_record(application_engine, record_id, 1, "ON_REVIEW")
    with Session(application_engine) as db:
        fake = RegistrationFake()

        def revoke():
            assert not db.in_transaction()
            with Session(application_engine) as other:
                other.execute(
                    delete(LocalRoleAssignment).where(
                        LocalRoleAssignment.role == "ADMIN"
                    )
                )
                other.commit()

        fake.hook = revoke
        with pytest.raises(HTTPException) as error:
            asyncio.run(
                workflow.review(
                    db, 2, record_id, 2, "APPROVED", "", "", False, fake, IdentityFake()
                )
            )
        assert error.value.status_code == 403


def test_operator_bootstrap_explicit_audited_and_idempotent(application_engine):
    with Session(application_engine) as db:
        set_admin(db, settings.oidc_issuer_url, "person-1", True, "Operator bootstrap")
        set_admin(db, settings.oidc_issuer_url, "person-1", True, "Repeated action")
        db.commit()
        assert (
            db.scalar(
                select(func.count())
                .select_from(LocalRoleAssignment)
                .where(LocalRoleAssignment.user_id == 1)
            )
            == 1
        )
        set_admin(db, settings.oidc_issuer_url, "person-1", False, "Removed")
        db.commit()
        assert (
            db.scalar(
                select(func.count())
                .select_from(BusinessAudit)
                .where(BusinessAudit.entity == "local_role")
            )
            == 2
        )


def test_html_escaping_private_notes_and_missing_email(browser):
    client, _, engine = browser
    submit_form(client)
    with Session(engine) as db:
        record = db.scalar(select(CreatorApplication))
        record.nickname = '<script>alert("x")</script>'
        record.staff_notes = "SECRET STAFF NOTE"
        record.email = None
        db.commit()
    assert "SECRET STAFF NOTE" not in client.get("/applications").text
    login(client, 2)
    page = client.get("/admin/applications")
    assert "Unavailable" in page.text and "&lt;script&gt;" in page.text
    assert '<script>alert("x")</script>' not in page.text


def test_postgres_concurrent_submits_and_approvals(postgres_engine, monkeypatch):
    monkeypatch.setattr(settings, "active_event_id", 910)
    now = datetime.now(UTC)
    with Session(postgres_engine) as db:
        db.add(
            Event(
                id=910,
                year=2030,
                name="Concurrency",
                starts_at=now,
                ends_at=now,
                application_open_at=now - timedelta(days=1),
                application_close_at=now + timedelta(days=1),
                badge_change_deadline_at=now + timedelta(days=1),
                badge_print_at=now,
                data_delete_at=now + timedelta(days=30),
            )
        )
        db.add_all([LocalUser(id=i) for i in (910, 911)])
        db.flush()
        db.add_all(
            [
                ExternalIdentity(
                    user_id=i,
                    issuer=settings.oidc_issuer_url,
                    subject=f"concurrent-{i}",
                )
                for i in (910, 911)
            ]
        )
        db.add(LocalRoleAssignment(user_id=910, role="ADMIN"))
        db.commit()
    barrier = Barrier(2)

    def submit_once(_):
        fake = RegistrationFake()
        fake.hook = lambda: barrier.wait(timeout=10)
        with Session(postgres_engine) as db:
            return asyncio.run(
                workflow.submit(
                    db, 911, input_data(), fake, IdentityFake(), **picture_input()
                )
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        ids = list(executor.map(submit_once, range(2)))
    assert ids[0] == ids[1]
    from app.creators.models import ProfileImage

    with Session(postgres_engine) as db:
        event_images = db.scalars(
            select(ProfileImage).where(ProfileImage.event_id == 910)
        ).all()
        assert len(event_images) == 1 and event_images[0].state == "ACTIVE"
    review_record(postgres_engine, ids[0], 1, "ON_REVIEW", actor=910)

    def approve_once(_):
        fake = RegistrationFake()
        fake.hook = lambda: barrier.wait(timeout=10)
        try:
            review_record(
                postgres_engine, ids[0], 2, "APPROVED", actor=910, registration=fake
            )
            return 200
        except HTTPException as error:
            return error.status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(approve_once, range(2))) == [200, 409]
    with Session(postgres_engine) as db:
        assert (
            db.scalar(
                select(func.count()).select_from(Badge).where(Badge.event_id == 910)
            )
            == 1
        )
        assert (
            db.scalar(
                select(func.count())
                .select_from(NotificationOutbox)
                .where(NotificationOutbox.event_id == 910)
            )
            == 1
        )
        assert db.get(BadgeCounter, 910).next_number == 2
        channel = db.scalar(
            select(CreatorChannel).where(CreatorChannel.application_id == ids[0])
        )
        channel.is_primary = False
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()


def test_review_and_correction_pages(browser):
    client, _, engine = browser
    submit_form(client)
    with Session(engine) as db:
        record_id = db.scalar(select(CreatorApplication.id))
    login(client, 2)
    path = f"/admin/applications/{record_id}"
    response = client.post(
        path,
        data={
            "csrf_token": csrf(client, path),
            "version": "1",
            "status": "ON_REVIEW",
            "staff_notes": "Private review",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    response = client.post(
        path,
        data={
            "csrf_token": csrf(client, path),
            "version": "2",
            "status": "NOT_APPROVED",
            "reason": "Applicant-visible rejection",
            "staff_notes": "Private review",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    correction_path = path + "/correct"
    response = client.post(
        correction_path,
        data={
            "csrf_token": csrf(client, correction_path),
            "version": "3",
            "content_type": "VLOGS",
            "platform": "Twitch",
            "account": "@corrected",
            "primary": "0",
            "correction_reason": "Correcting a typo",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    login(client, 1)
    page = client.get("/applications")
    assert "Applicant-visible rejection" in page.text
    assert "Private review" not in page.text and "@corrected" in page.text
    assert client.get("/applications/form").status_code == 409


def test_csrf_token_cannot_cross_sessions(browser):
    client, _, _ = browser
    token = csrf(client, "/applications/form")
    login(client, 3)
    csrf(client, "/applications/form")
    response = client.post("/applications/form", data={"csrf_token": token})
    assert response.status_code == 403


def test_approval_transaction_rolls_back_on_outbox_write_failure(application_engine):
    record_id = submit_record(application_engine)
    review_record(application_engine, record_id, 1, "ON_REVIEW")

    def fail_outbox(*_):
        raise RuntimeError("Simulated outbox persistence failure")

    sql_event.listen(NotificationOutbox, "before_insert", fail_outbox)
    try:
        with pytest.raises(RuntimeError):
            review_record(application_engine, record_id, 2, "APPROVED")
    finally:
        sql_event.remove(NotificationOutbox, "before_insert", fail_outbox)
    with Session(application_engine) as db:
        assert db.get(CreatorApplication, record_id).status == "ON_REVIEW"
        assert db.scalar(select(func.count()).select_from(Badge)) == 0
        assert db.scalar(select(func.count()).select_from(BadgeCounter)) == 0
        assert db.scalar(select(func.count()).select_from(NotificationOutbox)) == 0
        assert (
            db.scalar(
                select(func.count())
                .select_from(BusinessAudit)
                .where(BusinessAudit.action == "badge_allocated")
            )
            == 0
        )


def test_missing_deadline_blocks_approval(application_engine):
    record_id = submit_record(application_engine)
    review_record(application_engine, record_id, 1, "ON_REVIEW")
    with Session(application_engine) as db:
        db.get(Event, 1).badge_change_deadline_at = None
        db.commit()
    with pytest.raises(HTTPException) as error:
        review_record(application_engine, record_id, 2, "APPROVED", exceptional=True)
    assert error.value.status_code == 503


def test_database_constraints(application_engine):
    record_id = submit_record(application_engine)
    with Session(application_engine) as db:
        for changes in (
            {"livestream": False, "shorts": False, "vlogs": False},
            {"status": "DRAFT"},
            {"status": "NOT_APPROVED", "outcome_reason": ""},
        ):
            record = db.get(CreatorApplication, record_id)
            for name, value in changes.items():
                setattr(record, name, value)
            with pytest.raises(IntegrityError):
                db.commit()
            db.rollback()
        db.add(
            CreatorChannel(
                application_id=record_id,
                platform="Twitch",
                original_representation="@second",
                normalized_account="second",
                canonical_url="https://www.twitch.tv/second",
                is_primary=True,
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()


def test_outage_preserves_form_and_missing_email_does_not_block(browser):
    client, registration, _ = browser
    registration.failure = RegistrationUnavailable("private provider error")
    response = submit_form(client)
    assert response.status_code == 503
    assert "@Creator" in response.text and "retry" in response.text
    assert "private provider error" not in response.text
    registration.failure = None
    app.dependency_overrides.pop(get_identity_profile_client)
    assert submit_form(client).status_code == 303
    login(client, 2)
    assert "Unavailable" in client.get("/admin/applications").text


def test_postgres_distinct_approvals_allocate_unique_numbers(
    postgres_engine, monkeypatch
):
    monkeypatch.setattr(settings, "active_event_id", 920)
    now = datetime.now(UTC)
    with Session(postgres_engine) as db:
        db.add(
            Event(
                id=920,
                year=2031,
                name="Concurrent counter",
                starts_at=now,
                ends_at=now,
                application_open_at=now - timedelta(days=1),
                application_close_at=now + timedelta(days=1),
                badge_change_deadline_at=now + timedelta(days=1),
                badge_print_at=now,
                data_delete_at=now + timedelta(days=30),
            )
        )
        db.add_all([LocalUser(id=i) for i in (920, 921)])
        db.flush()
        db.add_all(
            [
                ExternalIdentity(
                    user_id=i, issuer=settings.oidc_issuer_url, subject=f"counter-{i}"
                )
                for i in (920, 921)
            ]
        )
        db.add(LocalRoleAssignment(user_id=920, role="ADMIN"))
        db.commit()
    ids = [submit_record(postgres_engine, user=i) for i in (920, 921)]
    for record_id in ids:
        review_record(postgres_engine, record_id, 1, "ON_REVIEW", actor=920)
    barrier = Barrier(2)

    def approve(record_id):
        fake = RegistrationFake()
        fake.hook = lambda: barrier.wait(timeout=10)
        review_record(
            postgres_engine, record_id, 2, "APPROVED", actor=920, registration=fake
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(approve, ids))
    with Session(postgres_engine) as db:
        assert sorted(
            db.scalars(select(Badge.badge_number).where(Badge.event_id == 920))
        ) == [1, 2]
        assert db.get(BadgeCounter, 920).next_number == 3
