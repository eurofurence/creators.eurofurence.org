import asyncio
from datetime import UTC, datetime, timedelta
from io import BytesIO

import pytest
import test_applications as m2
from fastapi import HTTPException
from PIL import Image
from sqlalchemy import event as sql_event
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.applications import workflow
from app.applications.models import BusinessAudit, CreatorApplication
from app.creators import images
from app.creators.models import CreatorProfile, ProfileImage
from app.events.models import Event
from app.main import app

application_engine = m2.application_engine
postgres_engine = m2.postgres_engine
browser = m2.browser


def post_application(client, picture):
    return client.post(
        "/applications/form",
        data={
            "csrf_token": m2.csrf(client, "/applications/form"),
            "content_type": "VLOGS",
            "platform": "Twitch",
            "account": "creator",
            "primary": "0",
        },
        files={"picture": ("untrusted-name.exe", picture, "application/octet-stream")}
        if picture is not None
        else None,
        follow_redirects=False,
    )


@pytest.mark.parametrize("picture", [None, b"not a PNG"])
def test_missing_or_invalid_picture_cannot_submit(browser, picture):
    client, _, engine = browser
    response = post_application(client, picture)
    assert response.status_code == 422
    assert 'value="creator"' in response.text
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(CreatorApplication)) == 0
        assert db.scalar(select(func.count()).select_from(ProfileImage)) == 0


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
        assert db.scalar(select(func.count()).select_from(CreatorApplication)) == 0
        assert db.scalar(select(func.count()).select_from(ProfileImage)) == 0


def test_application_picture_visible_to_reviewer_before_approval(browser):
    client, _, engine = browser
    assert 'name="picture"' in client.get("/applications/form").text
    assert post_application(client, m2.application_picture()).status_code == 303
    with Session(engine) as db:
        record = db.scalar(select(CreatorApplication))
        key = record.id
        assert record.status == "NEW"
        profile = db.get(CreatorProfile, key)
        assert db.get(ProfileImage, profile.image_id).state == "ACTIVE"
    assert f"/creators/{key}/picture" in client.get("/applications").text
    assert client.get("/api/v1/events/2028/creators").json() == []
    m2.login(client, 2)
    review = client.get(f"/admin/applications/{key}")
    assert f"/creators/{key}/picture" in review.text
    assert "Start review" in review.text and "Step 1" in review.text
    response = client.get(f"/creators/{key}/picture")
    assert response.status_code == 200
    with Image.open(BytesIO(response.content)) as image:
        assert image.size == (600, 600)
        assert image.info["dpi"] == pytest.approx((300, 300), abs=0.01)
    assert response.headers["cache-control"] == "no-store"
    m2.review_record(engine, key, 1, "ON_REVIEW")
    review = client.get(f"/admin/applications/{key}")
    assert "Step 2" in review.text and "Approve application" in review.text
    m2.login(client, 3)
    assert client.get(f"/creators/{key}/picture").status_code == 403
    client.cookies.clear()
    assert client.get(f"/creators/{key}/picture").status_code == 401


@pytest.mark.parametrize("failure", ["storage", "database", "closed_window"])
def test_failed_submission_keeps_no_partial_application_or_live_orphan(
    application_engine, failure
):
    with Session(application_engine, expire_on_commit=False) as db:

        class Store(m2.SubmissionStore):
            def put(self, key, data):
                assert not db.in_transaction()
                super().put(key, data)
                if failure == "storage":
                    raise images.ImageStorageUnavailable
                if failure == "closed_window":
                    with Session(application_engine) as other:
                        other.get(Event, 1).application_close_at = datetime.now(
                            UTC
                        ) - timedelta(seconds=1)
                        other.commit()

            def delete(self, key):
                assert not db.in_transaction()
                super().delete(key)

        def fail_audit(*_):
            raise RuntimeError("injected audit failure")

        store = Store()
        if failure == "database":
            sql_event.listen(BusinessAudit, "before_insert", fail_audit)
        try:
            with pytest.raises((HTTPException, RuntimeError)):
                asyncio.run(
                    workflow.submit(
                        db,
                        1,
                        m2.input_data(),
                        m2.RegistrationFake(),
                        m2.IdentityFake(),
                        picture=m2.application_picture(),
                        store=store,
                    )
                )
        finally:
            if failure == "database":
                sql_event.remove(BusinessAudit, "before_insert", fail_audit)
        assert db.scalar(select(func.count()).select_from(CreatorApplication)) == 0
        assert db.scalar(select(func.count()).select_from(CreatorProfile)) == 0
        assert db.scalar(select(func.count()).select_from(ProfileImage)) == 0
        assert store.objects == {}


def test_new_picture_edit_locks_on_review_and_admin_correction_is_explicit(browser):
    client, _, engine = browser
    assert post_application(client, m2.application_picture()).status_code == 303
    with Session(engine) as db:
        key = db.scalar(select(CreatorApplication.id))
    path = f"/creators/{key}/picture"
    token = m2.csrf(client, "/applications/form")
    assert (
        client.post(
            path,
            data={"csrf_token": token, "version": 1},
            files={"picture": ("new.png", m2.application_picture())},
        ).status_code
        == 200
    )
    m2.review_record(engine, key, 2, "ON_REVIEW")
    assert (
        client.post(
            path,
            data={"csrf_token": token, "version": 3},
            files={"picture": ("new.png", m2.application_picture())},
        ).status_code
        == 409
    )
    m2.login(client, 3)
    assert (
        client.post(
            path,
            data={"csrf_token": m2.csrf(client, "/account"), "version": 3},
            files={"picture": ("new.png", m2.application_picture())},
        ).status_code
        == 404
    )
    m2.login(client, 2)
    token = m2.csrf(client, f"/admin/applications/{key}/correct")
    assert (
        client.post(
            path,
            data={
                "csrf_token": token,
                "version": 3,
                "administrative": "yes",
                "reason": "Correction during review",
            },
            files={"picture": ("fixed.png", m2.application_picture())},
        ).status_code
        == 200
    )
    with Session(engine) as db:
        assert db.get(CreatorApplication, key).status == "ON_REVIEW"
        assert db.get(CreatorApplication, key).version == 4
    m2.review_record(engine, key, 4, "APPROVED")


def test_legacy_application_requires_picture_before_review(browser):
    client, _, engine = browser
    key = m2.submit_record(engine)
    with Session(engine) as db:
        db.get(CreatorProfile, key).image_id = None
        db.commit()
    with pytest.raises(HTTPException, match="picture is required"):
        m2.review_record(engine, key, 1, "ON_REVIEW")
    form = client.get("/applications/form")
    assert "Upload your picture before" in form.text
    assert (
        client.post(
            f"/creators/{key}/picture",
            data={"csrf_token": m2.csrf(client, "/applications/form"), "version": 1},
            files={"picture": ("picture.png", m2.application_picture())},
        ).status_code
        == 200
    )
    m2.review_record(engine, key, 2, "ON_REVIEW")


def test_submission_requires_csrf_and_storage_failure_is_retryable(browser):
    client, _, engine = browser
    assert (
        client.post(
            "/applications/form",
            files={"picture": ("picture.png", m2.application_picture())},
        ).status_code
        == 403
    )
    store = m2.SubmissionStore()

    def unavailable(*_):
        raise images.ImageStorageUnavailable

    store.put = unavailable
    app.dependency_overrides[images.get_image_store] = lambda: store
    response = post_application(client, m2.application_picture())
    assert response.status_code == 503
    assert "Image storage is unavailable" in response.text
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(CreatorApplication)) == 0


def test_missing_picture_blocks_approval_but_not_approval_revocation(
    application_engine,
):
    key = m2.submit_record(application_engine)
    m2.review_record(application_engine, key, 1, "ON_REVIEW")
    m2.review_record(application_engine, key, 2, "APPROVED")
    with Session(application_engine) as db:
        db.get(CreatorProfile, key).image_id = None
        db.commit()
    m2.review_record(application_engine, key, 3, "ON_REVIEW")
    with pytest.raises(HTTPException, match="picture is required"):
        m2.review_record(application_engine, key, 4, "APPROVED")
