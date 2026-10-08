"""Stored Reg-ID lookup and repeated links, on isolated SQLite/PostgreSQL."""

from datetime import UTC, datetime, timedelta

import pytest
import test_applications as m2
import test_browser_acceptance as acceptance
import test_m3 as m3
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.applications.models import Badge, CreatorApplication
from app.config import settings
from app.creators.models import CreatorProfile
from app.events.models import Event
from app.helpers.models import HelperRegistration
from app.staff import service

application_engine = m2.application_engine
postgres_engine = m2.postgres_engine
browser = m2.browser


def test_repeated_links_add_edit_remove_and_preserve_other_fields(browser):
    client, _, engine = browser
    assert m2.submit_form(client).status_code == 303
    original = acceptance.saved_state(engine)
    links = [f"https://video.example/{i}" for i in range(3)]
    for submitted in (
        links,
        [links[0], links[2]],
        [links[0], "https://video.example/edited"],
        [],
    ):
        before = acceptance.saved_state(engine)
        response = acceptance.edit_form(
            client, before, videos_present="yes", video_url=submitted
        )
        assert response.status_code == 303
        current = acceptance.saved_state(engine)
        assert current["videos"] == submitted
        for key in ("channels", "image_id", "content"):
            assert current[key] == original[key]
        page = client.get("/applications/form").text
        assert '<textarea name="videos"' not in page
        assert 'id="add-video">Add another link' in page
        for link in submitted:
            assert f'value="{link}"' in page
    # An omitted link section must not clear the stored URLs.
    before = acceptance.saved_state(engine)
    assert acceptance.edit_form(client, before, video_url=links).status_code == 303
    before = acceptance.saved_state(engine)
    assert (
        acceptance.edit_form(client, before, content_type="SHORTS").status_code == 303
    )
    current = acceptance.saved_state(engine)
    assert current["videos"] == links
    assert current["channels"] == original["channels"]
    assert current["image_id"] == original["image_id"]
    assert (
        acceptance.edit_form(
            client, current, platform="YouTube", account="@creator", primary="0"
        ).status_code
        == 303
    )
    current = acceptance.saved_state(engine)
    assert current["videos"] == links and current["image_id"] == original["image_id"]


def test_repeated_links_errors_identify_rows_and_keep_every_entered_value(browser):
    client, _, engine = browser
    assert m2.submit_form(client).status_code == 303
    before = acceptance.saved_state(engine)
    links = [
        "https://video.example/first",
        "http://invalid.example/second",
        "https://video.example/third",
        "not-a-url",
    ]
    response = acceptance.edit_form(
        client, before, videos_present="yes", video_url=links
    )
    assert response.status_code == 422
    assert "Convention video link 2:" in response.text
    assert "Convention video link 4:" in response.text
    assert 'id="video-error-1"' in response.text
    assert 'id="video-error-3"' in response.text
    assert 'aria-invalid="true"' in response.text
    for link in links:
        assert f'value="{link}"' in response.text
    assert "@Creator" in response.text
    assert f"/creators/{before['id']}/picture" in response.text
    assert acceptance.saved_state(engine) == before
    ten = [f"https://video.example/{i}" for i in range(10)]
    assert (
        acceptance.edit_form(
            client, before, video_url=ten + ["https://extra.example/"]
        ).status_code
        == 422
    )
    assert acceptance.saved_state(engine) == before
    assert acceptance.edit_form(client, before, video_url=ten).status_code == 303
    assert acceptance.saved_state(engine)["videos"] == ten


def test_stored_reg_id_differs_from_badge_number_and_needs_no_provider(browser):
    client, registration, engine = browser
    with Session(engine, expire_on_commit=False) as db:
        app_id = m3.approve(db)
        record = db.get(CreatorApplication, app_id)
        # Submission/approval persisted the Registration response, not the badge number.
        assert record.reg_id == "REG-123" and record.eligibility_checked_at
        assert db.scalar(select(Badge.badge_number)) == 1
    registration.failure = RuntimeError("Staff lookup must not call Registration")
    calls = registration.calls
    assert "Attendee Reg-ID:" in client.get("/applications").text
    assert "REG-123" in client.get("/applications").text
    path = "/staff/events/1/badges"
    for actor in (2, 3):  # Global ADMIN and event-scoped BADGE_STAFF.
        m2.login(client, actor)
        token = m2.csrf(client, path)
        missing = client.post(path, data={"csrf_token": token, "reg_id": "1"})
        assert missing.status_code == 200 and "No badge records found" in missing.text
        assert "different identifier" in missing.text
        found = client.post(path, data={"csrf_token": token, "reg_id": " REG-123 "})
        assert found.status_code == 200
        assert "Badge 1" in found.text and "Creator" in found.text
        assert "Active" in found.text and "Not collected" in found.text
        assert (
            client.post(
                path, data={"csrf_token": "wrong", "reg_id": "REG-123"}
            ).status_code
            == 403
        )
        assert (
            client.post(path, data={"csrf_token": token, "reg_id": " "}).status_code
            == 422
        )
    m2.login(client, 1)
    assert client.get(path).status_code == 403
    assert client.post(path, data={"reg_id": "REG-123"}).status_code == 403
    m2.login(client, 3)
    assert "REG-123" not in client.get("/applications").text
    assert "REG-123" not in client.get("/api/v1/events/2028/creators").text
    assert registration.calls == calls


def test_one_attendee_multiple_creator_helper_badges_and_event_isolation(
    application_engine, monkeypatch
):
    with Session(application_engine, expire_on_commit=False) as db:
        own = m3.approve(db, 1)
        others = [m3.approve(db, user) for user in (2, 3)]
        helpers = []
        for other, actor in zip(others, (2, 3)):
            helper = m3.register(db, other, user=1, actor=actor)
            m3.decide(db, helper, actor=actor)
            assert db.get(HelperRegistration, helper).reg_id == "REG-123"
            helpers.append(helper)
            db.get(CreatorApplication, other).reg_id = f"OTHER-{actor}"
            db.get(CreatorProfile, other).channel_name = f"Linked creator {actor}"
            db.commit()
        badge = db.scalar(select(Badge).where(Badge.helper_id == helpers[0]))
        service.pickup(db, 3, 1, badge.id)
        m3.withdraw(db, helpers[1], actor=1, helper=True)
        rows = service.lookup(db, 3, 1, "REG-123")
        assert [row["kind"] for row in rows] == ["Creator", "Helper", "Helper"]
        assert [row["application_id"] for row in rows] == [own, *others]
        assert [row["active"] for row in rows] == [True, True, False]
        assert rows[1]["picked_up_at"] and not rows[2]["picked_up_at"]
        assert rows[1]["creator"] == "Linked creator 2"
        assert service.lookup(db, 2, 1, "REG-123") == rows
        assert service.lookup(db, 3, 1, "nonexistent") == []
        now = datetime.now(UTC)
        db.add(
            Event(
                id=2,
                year=2029,
                name="Separate event",
                starts_at=now,
                ends_at=now + timedelta(days=3),
                application_open_at=now - timedelta(days=1),
                application_close_at=now + timedelta(days=2),
                badge_change_deadline_at=now + timedelta(days=2),
                badge_print_at=now + timedelta(days=3),
            )
        )
        db.commit()
        monkeypatch.setattr(settings, "active_event_id", 2)
        m3.approve(db, 1)
        isolated = service.lookup(db, 2, 2, "REG-123")
        assert len(isolated) == 1 and isolated[0]["number"] == 1
        assert isolated[0]["application_id"] not in [own, *others]
        assert len(service.lookup(db, 2, 1, "REG-123")) == 3
        with pytest.raises(HTTPException) as error:
            service.lookup(db, 3, 2, "REG-123")
        assert error.value.status_code == 403
