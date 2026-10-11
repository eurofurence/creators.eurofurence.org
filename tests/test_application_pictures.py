"""Issue #79: approval precedes picture upload, publication and printing."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
import test_applications as m2
import test_browser_acceptance as acceptance
import test_m3 as m3
from fastapi import HTTPException
from sqlalchemy import event as sql_event
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.applications import workflow
from app.applications.models import Badge, BusinessAudit, CreatorApplication
from app.config import settings
from app.creators import images
from app.creators.models import CreatorProfile, ProfileImage
from app.events.models import Event
from app.main import app
from app.staff import exports

application_engine = m2.application_engine
postgres_engine = m2.postgres_engine
browser = m2.browser


def approve_form(client, engine):
    key = acceptance.saved_state(engine)["id"]
    m2.login(client, 2)
    path = f"/admin/applications/{key}"
    for version, status in ((1, "ON_REVIEW"), (2, "APPROVED")):
        response = client.post(
            path,
            data={
                "csrf_token": m2.csrf(client, path),
                "version": version,
                "status": status,
            },
            follow_redirects=False,
        )
        assert response.status_code == 303, response.text
    m2.login(client, 1)
    return key


def upload(client, key, data, **changes):
    path = f"/creators/{key}"
    return client.post(
        path + "/picture",
        data={"csrf_token": m2.csrf(client, path), "version": 3, **changes},
        files={"picture": ("untrusted.exe", data, "application/octet-stream")}
        if data is not None
        else None,
        follow_redirects=False,
    )


def test_submit_review_and_approve_without_picture_or_storage(browser):
    client, _, engine = browser

    def no_storage():
        pytest.fail("Application submission/review must not depend on image storage")

    app.dependency_overrides[images.get_image_store] = no_storage
    form = client.get("/applications/form")
    assert 'name="picture"' not in form.text
    assert "profile picture is requested after approval" in form.text
    assert m2.submit_form(client).status_code == 303
    key = approve_form(client, engine)
    for path in ("/applications", f"/creators/{key}"):
        page = client.get(path)
        assert "No valid profile picture has been supplied" in page.text
        assert "printing" in page.text
    page = client.get(f"/creators/{key}").text
    assert "Use the same profile picture as on your primary publication channel" in page
    assert "Upload picture" in page
    assert client.get("/api/v1/events/2028/creators").json() == []
    with Session(engine) as db:
        assert db.get(CreatorApplication, key).status == "APPROVED"
        assert db.get(CreatorProfile, key).image_id is None
        assert db.scalar(select(Badge.badge_number)) == 1
        assert db.scalar(select(func.count()).select_from(ProfileImage)) == 0


def test_legacy_application_upload_is_rejected_with_instructions(browser):
    client, _, engine = browser
    response = client.post(
        "/applications/form",
        data={
            "csrf_token": m2.csrf(client, "/applications/form"),
            "content_type": "VLOGS",
            "platform": "Twitch",
            "account": "creator",
            "primary": "0",
            "video_url": "https://video.example/convention",
        },
        files={"picture": ("old-form.png", m2.application_picture(), "image/png")},
    )
    assert response.status_code == 422
    assert (
        "Upload your profile picture from your Creator profile after approval"
        in response.text
    )
    assert 'value="creator"' in response.text
    with Session(engine) as db:
        assert db.scalar(select(CreatorApplication)) is None
        assert db.scalar(select(ProfileImage)) is None


@pytest.mark.parametrize("field", ["account", "csrf_token"])
def test_upload_cannot_replace_a_text_form_field(browser, field):
    client, _, engine = browser
    response = client.post(
        "/applications/form",
        data={
            "csrf_token": m2.csrf(client, "/applications/form"),
            "content_type": "VLOGS",
            "platform": "Twitch",
            "primary": "0",
        },
        files={field: ("unexpected.png", m2.application_picture(), "image/png")},
    )
    assert response.status_code == 422
    with Session(engine) as db:
        assert db.scalar(select(CreatorApplication)) is None


@pytest.mark.parametrize("failure", ["database", "closed_window", "changed_event"])
def test_submission_transaction_and_revalidation_without_image_staging(
    application_engine, failure
):
    def fail_audit(*_):
        raise RuntimeError("injected audit failure")

    registration = m2.RegistrationFake()
    if failure != "database":

        def change_context():
            with Session(application_engine) as other:
                event = other.get(Event, 1)
                if failure == "closed_window":
                    event.application_close_at = datetime.now(UTC) - timedelta(
                        seconds=1
                    )
                else:
                    event.year += 1
                other.commit()

        registration.hook = change_context
    else:
        sql_event.listen(BusinessAudit, "before_insert", fail_audit)
    try:
        with Session(application_engine) as db:
            with pytest.raises((HTTPException, RuntimeError)):
                asyncio.run(
                    workflow.submit(
                        db, 1, m2.input_data(), registration, m2.IdentityFake()
                    )
                )
            db.rollback()
            for model in (
                CreatorApplication,
                CreatorProfile,
                ProfileImage,
                BusinessAudit,
            ):
                assert db.scalar(select(func.count()).select_from(model)) == 0
    finally:
        if failure == "database":
            sql_event.remove(BusinessAudit, "before_insert", fail_audit)


@pytest.mark.parametrize("status", ["NEW", "ON_REVIEW", "NOT_APPROVED", "NOT_ACCEPTED"])
@pytest.mark.parametrize("administrative", [False, True])
def test_picture_upload_requires_approval_even_for_direct_requests(
    browser, status, administrative
):
    client, _, engine = browser
    assert m2.submit_form(client).status_code == 303
    with Session(engine) as db:
        record = db.scalar(select(CreatorApplication))
        record.status = status
        record.outcome_reason = "Fixture review decision"
        key = record.id
        db.commit()
    before = acceptance.saved_state(engine)
    m2.login(client, 2 if administrative else 1)
    path = f"/creators/{key}"
    page = path + ("?administrative=true" if administrative else "")
    assert 'name="picture"' not in client.get(page).text
    response = client.post(
        path + "/picture",
        data={
            "csrf_token": m2.csrf(client, page),
            "version": 1,
            "administrative": "yes" if administrative else "no",
            "reason": "Correct picture",
        },
        files={"picture": ("picture.png", m2.application_picture())},
    )
    assert response.status_code == 409
    assert "active approved creator is required" in response.text
    assert acceptance.saved_state(engine) == before
    assert not app.dependency_overrides[images.get_image_store]().objects


@pytest.mark.parametrize(
    "problem", ["missing", "invalid", "jpeg", "dimensions", "oversized"]
)
def test_invalid_approved_upload_preserves_profile_and_shows_error(
    browser, monkeypatch, problem
):
    client, _, engine = browser
    assert m2.submit_form(client).status_code == 303
    key = approve_form(client, engine)
    before = acceptance.saved_state(engine)
    data = {
        "missing": None,
        "invalid": b"not a PNG",
        "jpeg": m3.png(format="JPEG"),
        "dimensions": m3.png(size=(599, 600)),
        "oversized": m2.application_picture(),
    }[problem]
    if problem == "oversized":
        monkeypatch.setattr(settings, "profile_image_max_bytes", len(data) - 1)
    response = upload(client, key, data)
    assert response.status_code == (413 if problem == "oversized" else 422)
    assert 'role="alert"' in response.text
    assert acceptance.saved_state(engine) == before
    assert not app.dependency_overrides[images.get_image_store]().objects


def test_missing_picture_blocks_print_but_not_approval_reversal_or_reapproval(browser):
    client, registration, engine = browser
    assert m2.submit_form(client).status_code == 303
    key = approve_form(client, engine)
    store = app.dependency_overrides[images.get_image_store]()
    with Session(engine) as db:
        profile = db.get(CreatorProfile, key)
        profile.channel_name = "Approved creator"
        public_id = profile.public_id
        db.commit()
        with pytest.raises(HTTPException) as error:
            asyncio.run(exports.export(db, 2, 1, registration, store))
        assert error.value.status_code == 409
        assert "Profile picture missing. Upload a valid PNG" in str(error.value.detail)
        assert error.value.detail["problems"][0]["number"] == 1
        assert db.get(CreatorApplication, key).status == "APPROVED"
        assert asyncio.run(
            exports.export(db, 2, 1, registration, store, print_ready=False)
        )
    assert client.get("/api/v1/events/2028/creators").json() == []
    assert "Approved creator" not in client.get("/gallery/2028").text
    assert (
        client.get(f"/api/v1/events/2028/creators/{public_id}/image").status_code == 404
    )
    m2.review_record(engine, key, 3, "ON_REVIEW")
    m2.review_record(engine, key, 4, "APPROVED")
    with Session(engine) as db:
        assert db.scalar(select(Badge.badge_number)) == 1
        assert db.get(CreatorProfile, key).image_id is None


def test_legacy_record_without_profile_can_be_approved_and_completed(browser):
    client, _, engine = browser
    assert m2.submit_form(client).status_code == 303
    key = acceptance.saved_state(engine)["id"]
    with Session(engine) as db:
        db.delete(db.get(CreatorProfile, key))
        db.commit()
    m2.review_record(engine, key, 1, "ON_REVIEW")
    m2.review_record(engine, key, 2, "APPROVED")
    assert upload(client, key, m2.application_picture()).status_code == 303
    with Session(engine) as db:
        assert db.get(CreatorProfile, key).image_id is not None


def test_approved_upload_requires_csrf_and_reports_storage_outage(browser):
    client, _, engine = browser
    assert m2.submit_form(client).status_code == 303
    key = approve_form(client, engine)
    assert (
        client.post(
            f"/creators/{key}/picture",
            files={"picture": ("profile.png", m2.application_picture())},
        ).status_code
        == 403
    )
    store = app.dependency_overrides[images.get_image_store]()

    def unavailable(*_):
        raise images.ImageStorageUnavailable

    store.put = unavailable
    response = upload(client, key, m2.application_picture())
    assert (
        response.status_code == 503 and "Image storage is unavailable" in response.text
    )
    with Session(engine) as db:
        assert db.get(CreatorProfile, key).image_id is None
        assert db.get(CreatorApplication, key).status == "APPROVED"
        assert db.scalar(select(ProfileImage)) is None
