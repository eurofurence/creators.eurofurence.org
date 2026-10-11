"""Issue #78: required video evidence through application and profile workflows."""

import asyncio
from dataclasses import replace
from html import escape

import pytest
import test_applications as m2
import test_browser_acceptance as acceptance
import test_m3 as m3
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.applications import workflow
from app.applications.models import BusinessAudit, ConventionVideo, CreatorApplication
from app.creators import images, service
from app.creators.models import ProfileImage
from app.main import app

application_engine = m2.application_engine
postgres_engine = m2.postgres_engine
browser = m2.browser

REQUIRED = "Provide at least one existing convention video URL."


@pytest.mark.parametrize(
    "fields",
    [
        {},  # No video controls, including a request made without the browser UI.
        {"videos": ""},
        {"videos": " \t\r\n "},
        {"videos_present": "yes"},
        {"video_url": ["", " \t "]},
        {"videos_present": "yes", "videos": "https://video.example/old-field"},
    ],
)
def test_submission_requires_video_before_external_calls_or_writes(browser, fields):
    client, registration, engine = browser
    page = client.get("/applications/form")
    assert "Existing convention videos (at least one required)" in page.text
    assert "at least one valid link must remain when saving" in page.text
    response = client.post(
        "/applications/form",
        data={
            "csrf_token": m2.csrf(client, "/applications/form"),
            "content_type": "VLOGS",
            "platform": "Twitch",
            "account": "@Creator",
            "primary": "0",
            **fields,
        },
        files={"picture": ("picture.png", m2.application_picture(), "image/png")},
    )
    assert response.status_code == 422
    assert REQUIRED in response.text and 'role="alert"' in response.text
    assert 'value="@Creator"' in response.text
    assert 'value="VLOGS" checked' in response.text
    assert registration.calls == 0
    assert not app.dependency_overrides[images.get_image_store]().objects
    with Session(engine) as db:
        for model in (CreatorApplication, ConventionVideo, ProfileImage, BusinessAudit):
            assert db.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.parametrize("count", [1, 3, 10, 11])
@pytest.mark.parametrize("editor", ["legacy", "repeated"])
def test_submission_preserves_multiple_urls_and_ten_link_limit(browser, count, editor):
    client, _, engine = browser
    links = [f"https://evidence.example/watch?v={i}&event=ef" for i in range(count)]
    fields = (
        {"videos": "\n".join(["", *links, " "])}
        if editor == "legacy"
        else {"videos_present": "yes", "video_url": ["", *links, " "]}
    )
    response = m2.submit_form(client, **fields)
    if count > 10:
        assert response.status_code == 422
        assert "at most 10 convention video links" in response.text
        for link in links:
            assert f'value="{escape(link, quote=True)}"' in response.text
        with Session(engine) as db:
            assert db.scalar(select(CreatorApplication)) is None
    else:
        assert response.status_code == 303
        state = acceptance.saved_state(engine)
        assert state["videos"] == links
        # Editing another section, or a complete no-op edit, preserves every link.
        assert (
            acceptance.edit_form(client, state, content_type="SHORTS").status_code
            == 303
        )
        state = acceptance.saved_state(engine)
        assert acceptance.edit_form(client, state).status_code == 303
        assert acceptance.saved_state(engine)["videos"] == links


@pytest.mark.parametrize(
    "fields",
    [
        {"videos": " \r\n"},
        {"videos_present": "yes"},
        {"videos_present": "yes", "video_url": ["", " \t "]},
    ],
)
@pytest.mark.parametrize("administrative", [False, True])
def test_clearing_last_link_rejects_edit_and_preserves_saved_data(
    browser, fields, administrative
):
    client, _, engine = browser
    assert m2.submit_form(client).status_code == 303
    acceptance.seed_legacy_picture(engine)
    before = acceptance.saved_state(engine)
    with Session(engine) as db:
        audit_count = db.scalar(select(func.count()).select_from(BusinessAudit))
    if administrative:
        m2.login(client, 2)
        path = f"/admin/applications/{before['id']}/correct"
        response = client.post(
            path,
            data={
                "csrf_token": m2.csrf(client, path),
                "version": before["version"],
                "correction_reason": "Correct video evidence",
                **fields,
            },
        )
        assert "Correct video evidence" in response.text
    else:
        response = acceptance.edit_form(client, before, **fields)
    assert response.status_code == 422 and REQUIRED in response.text
    assert 'value="@Creator"' in response.text
    assert f"/creators/{before['id']}/picture" in response.text
    # Retain the explicitly cleared form input so the creator can correct it;
    # the failed save must not erase the stored link or other application data.
    assert before["videos"][0] not in response.text
    assert acceptance.saved_state(engine) == before
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(BusinessAudit)) == audit_count


@pytest.mark.parametrize(
    "videos",
    [
        (),
        ("",),
        (" \t ",),
        ("http://video.example/",),
        ("javascript:alert(1)",),
        ("https://video.example/valid", "https://user:password@video.example/"),
    ],
)
def test_direct_workflows_cannot_bypass_video_validation(application_engine, videos):
    record_id = m2.submit_record(application_engine)
    before = acceptance.saved_state(application_engine)
    data = replace(m2.input_data(), videos=videos)
    registration = m2.RegistrationFake()
    with Session(application_engine) as db:
        with pytest.raises(ValueError):
            asyncio.run(workflow.submit(db, 3, data, registration, m2.IdentityFake()))
        with pytest.raises(ValueError):
            workflow.edit(db, 1, record_id, before["version"], data)
    assert registration.calls == 0
    assert acceptance.saved_state(application_engine) == before


def test_legacy_empty_application_requires_video_on_edit_but_keeps_profile_working(
    browser,
):
    client, _, engine = browser
    assert m2.submit_form(client).status_code == 303
    with Session(engine) as db:
        # Simulate data saved when zero videos were permitted; do not backfill it.
        db.execute(delete(ConventionVideo))
        db.commit()
    before = acceptance.saved_state(engine)
    assert "No convention videos supplied." in client.get("/applications").text
    response = acceptance.edit_form(client, before, content_type="SHORTS")
    assert response.status_code == 422 and REQUIRED in response.text
    assert acceptance.saved_state(engine) == before
    assert (
        acceptance.edit_form(
            client, before, video_url="https://video.example/new"
        ).status_code
        == 303
    )
    assert acceptance.saved_state(engine)["videos"] == ["https://video.example/new"]

    # Profile saves do not edit video evidence; retain their channel validation.
    with Session(engine, expire_on_commit=False) as db:
        key = m3.approve(db, 3)
        db.execute(delete(ConventionVideo).where(ConventionVideo.application_id == key))
        db.commit()
        version = db.get(CreatorApplication, key).version
        channels = m2.input_data().channels
        for invalid in ((), (replace(channels[0], is_primary=False),), channels * 2):
            with pytest.raises(ValueError):
                service.save_profile(db, 3, key, version, "Creator", invalid)
        service.save_profile(db, 3, key, version, "Creator", channels)
        assert not workflow.current_input(db, db.get(CreatorApplication, key)).videos
