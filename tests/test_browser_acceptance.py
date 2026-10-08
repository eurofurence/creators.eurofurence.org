"""Browser workflow regressions, with isolated SQLite and PostgreSQL data."""

import asyncio
from io import BytesIO

import pytest
import test_applications as m2
from fastapi import HTTPException
from openpyxl import load_workbook
from PIL import Image
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.applications.input import normalize_channel
from app.applications.models import (
    Badge,
    ConventionVideo,
    CreatorApplication,
    CreatorChannel,
    LocalRoleAssignment,
)
from app.creators import images
from app.creators.models import CreatorProfile, ProfileImage
from app.main import app
from app.moderation import service as moderation
from app.staff import exports

application_engine = m2.application_engine
postgres_engine = m2.postgres_engine
browser = m2.browser


@pytest.mark.parametrize(
    "value",
    [
        "@Creator",
        "Creator",
        "youtube.com/@Creator",
        "www.youtube.com/@Creator",
        "https://youtube.com/@Creator",
        "https://www.youtube.com/@Creator/?view=1#about",
    ],
)
def test_youtube_handle_forms_share_canonical_account(value):
    channel = normalize_channel("YouTube", value, True)
    assert channel.original_representation == value
    assert channel.normalized_account == "creator"
    assert channel.canonical_url == "https://www.youtube.com/@creator"
    assert channel.is_primary


def test_youtube_channel_id_and_international_handle():
    identifier = "UCAbcDef0123456789_-abcD"
    value = "https://www.youtube.com/channel/" + identifier
    channel = normalize_channel("YouTube", value, True)
    assert channel.canonical_url == value
    assert channel.normalized_account == "channel:" + identifier
    unicode_handle = normalize_channel(
        "YouTube", "https://youtube.com/@Cr%C3%89ateur", False
    )
    assert (
        unicode_handle.normalized_account
        == normalize_channel("YouTube", "@CrÉateur", False).normalized_account
    )
    assert unicode_handle.normalized_account == "créateur"
    assert unicode_handle.canonical_url == "https://www.youtube.com/@cr%C3%A9ateur"


@pytest.mark.parametrize(
    "value",
    [
        "https://youtube.com/watch?v=abc",
        "https://youtube.com/shorts/abc",
        "https://youtu.be/abc",
        "https://youtube.com.evil.example/@creator",
        "https://evil.example/@creator",
        "http://youtube.com/@creator",
        "https://youtube.com:444/@creator",
        "https://secret@youtube.com/@creator",
        "https://youtube.com/@creator%2Fvideos",
        "https://youtube.com/channel/not-an-id",
        "javascript:alert(1)",
    ],
)
def test_youtube_rejects_video_urls_and_unsafe_accounts(value):
    with pytest.raises(ValueError):
        normalize_channel("YouTube", value, True)


def saved_state(engine):
    with Session(engine) as db:
        record = db.scalar(select(CreatorApplication))
        return {
            "id": record.id,
            "version": record.version,
            "content": (record.livestream, record.shorts, record.vlogs),
            "image_id": db.get(CreatorProfile, record.id).image_id,
            "channels": [
                (
                    c.id,
                    c.platform,
                    c.original_representation,
                    c.is_primary,
                    c.publicly_hidden,
                )
                for c in db.scalars(select(CreatorChannel).order_by(CreatorChannel.id))
            ],
            "videos": list(
                db.scalars(
                    select(ConventionVideo.url).order_by(ConventionVideo.position)
                )
            ),
        }


def edit_form(client, state, **changes):
    return client.post(
        "/applications/form",
        data={
            "csrf_token": m2.csrf(client, "/applications/form"),
            "application_id": state["id"],
            "version": state["version"],
            **changes,
        },
        follow_redirects=False,
    )


def test_partial_application_edits_keep_related_data_and_picture(browser):
    client, _, engine = browser
    assert m2.submit_form(client).status_code == 303
    original = saved_state(engine)
    with Session(engine) as db:
        db.get(CreatorChannel, original["channels"][0][0]).publicly_hidden = True
        db.commit()
    original = saved_state(engine)
    assert edit_form(client, original, content_type="LIVESTREAM").status_code == 303
    changed = saved_state(engine)
    for field in ("image_id", "channels", "videos"):
        assert changed[field] == original[field]
    assert changed["content"] == (True, False, False)
    assert (
        edit_form(
            client,
            changed,
            platform=["Twitch", "Mastodon"],
            account=["@Creator", "https://social.example/@Other"],
            primary="1",
        ).status_code
        == 303
    )
    changed = saved_state(engine)
    assert changed["image_id"] == original["image_id"]
    assert changed["videos"] == original["videos"]
    assert [c[0] for c in changed["channels"]] == [c[0] for c in original["channels"]]
    assert changed["channels"][0][4] is True
    assert changed["channels"][1][3] is True
    assert edit_form(client, changed, videos="").status_code == 303
    assert saved_state(engine)["videos"] == []
    assert saved_state(engine)["image_id"] == original["image_id"]


def test_edit_validation_and_conflicts_keep_entered_and_omitted_sections(browser):
    client, _, engine = browser
    assert m2.submit_form(client).status_code == 303
    original = saved_state(engine)
    response = edit_form(
        client,
        original,
        platform="YouTube",
        account="https://youtube.com/watch?v=bad",
        primary="0",
    )
    assert response.status_code == 422
    assert "Publication channel 1" in response.text
    assert 'value="https://youtube.com/watch?v=bad"' in response.text
    assert original["videos"][0] in response.text
    assert f"/creators/{original['id']}/picture" in response.text
    assert saved_state(engine) == original
    assert edit_form(client, original, content_types_present="yes").status_code == 422
    assert saved_state(engine) == original
    response = edit_form(
        client, original, version=0, videos="https://video.example/entered"
    )
    assert response.status_code == 409
    assert "https://video.example/entered" in response.text
    assert 'name="version" value="0"' in response.text
    assert saved_state(engine) == original


def test_admin_correction_errors_keep_existing_image_and_fields(browser):
    client, _, engine = browser
    assert m2.submit_form(client).status_code == 303
    original = saved_state(engine)
    m2.login(client, 2)
    path = f"/admin/applications/{original['id']}/correct"
    response = client.post(
        path,
        data={
            "csrf_token": m2.csrf(client, path),
            "version": 1,
            "videos": "http://invalid.example",
            "correction_reason": "Fix links",
        },
    )
    assert response.status_code == 422
    assert "Convention video link 1" in response.text
    assert "Fix links" in response.text and "@Creator" in response.text
    assert f"/creators/{original['id']}/picture" in response.text
    assert saved_state(engine) == original
    assert (
        client.post(
            path,
            data={
                "csrf_token": m2.csrf(client, path),
                "version": 1,
                "correction_reason": "Keep everything",
            },
            follow_redirects=False,
        ).status_code
        == 303
    )
    changed = saved_state(engine)
    for field in ("image_id", "channels", "videos", "content"):
        assert changed[field] == original[field]


def test_one_image_from_submission_through_approval_publication_and_print(browser):
    client, registration, engine = browser
    store = app.dependency_overrides[images.get_image_store]()
    assert (
        m2.submit_form(
            client, platform="YouTube", account="www.youtube.com/@Creator", primary="0"
        ).status_code
        == 303
    )
    original = saved_state(engine)
    key = original["id"]
    path = f"/creators/{key}"
    picture_path = path + "/picture"
    image_data = images.normalize_png(m2.application_picture())
    for page in ("/applications", "/applications/form"):
        assert f'src="{picture_path}"' in client.get(page).text
    assert client.get(picture_path).content == image_data
    assert client.get("/api/v1/events/2028/creators").json() == []
    m2.login(client, 2)
    assert f'src="{picture_path}"' in client.get(f"/admin/applications/{key}").text
    assert client.get(picture_path).content == image_data
    m2.review_record(engine, key, 1, "ON_REVIEW")
    m2.review_record(engine, key, 2, "APPROVED")
    m2.login(client, 1)
    assert (
        client.post(
            path + "/profile",
            data={
                "csrf_token": m2.csrf(client, path),
                "version": 3,
                "channel_name": "Creator name",
            },
            follow_redirects=False,
        ).status_code
        == 303
    )
    after_name = saved_state(engine)
    for field in ("image_id", "channels", "videos", "content"):
        assert after_name[field] == original[field]
    assert f'src="{picture_path}"' in client.get(path).text
    public = client.get("/api/v1/events/2028/creators").json()[0]
    assert public["channels"] == [
        {
            "platform": "YouTube",
            "account": "creator",
            "url": "https://www.youtube.com/@creator",
            "primary": True,
        }
    ]
    assert client.get(public["image_url"]).content == image_data
    assert "YouTube: creator" in client.get("/gallery/2028").text
    with Session(engine) as db:
        moderation.save(
            db, 2, platform="YouTube", account="@CREATOR", reason="Match test"
        )
        assert len(moderation.warnings(db, key)) == 1
        content = asyncio.run(exports.export(db, 2, 1, registration, store))
        filename, downloaded = exports.download_picture(db, 2, key, store)
        assert downloaded == image_data
        book = load_workbook(BytesIO(content))
        assert tuple(book["Print"].values)[1] == (
            1,
            "Creator name",
            "YouTube",
            "https://www.youtube.com/@creator",
            filename,
        )
        book.close()
        old_image = db.get(ProfileImage, original["image_id"])
        old_key = old_image.object_key
        badge_id = db.scalar(select(Badge.id))
    replacement = BytesIO()
    Image.new("RGB", (600, 600), "green").save(replacement, format="PNG")
    assert (
        client.post(
            picture_path,
            data={
                "csrf_token": m2.csrf(client, path),
                "version": after_name["version"],
            },
            files={"picture": ("new.png", replacement.getvalue())},
            follow_redirects=False,
        ).status_code
        == 303
    )
    new_data = images.normalize_png(replacement.getvalue())
    assert client.get(picture_path).content == new_data
    assert client.get(public["image_url"]).content == new_data
    m2.login(client, 2)
    assert client.get(picture_path).content == new_data
    with Session(engine) as db:
        profile = db.get(CreatorProfile, key)
        assert profile.image_id != original["image_id"]
        assert db.get(ProfileImage, original["image_id"]) is None
        assert old_key not in store.objects
        assert db.get(Badge, badge_id).badge_number == 1
        assert asyncio.run(exports.export(db, 2, 1, registration, store))
        current_key = db.get(ProfileImage, profile.image_id).object_key
        store.objects.pop(current_key)
        with pytest.raises(HTTPException) as error:
            asyncio.run(exports.export(db, 2, 1, registration, store))
        assert "Profile image unavailable or invalid" in str(error.value.detail)


def test_legacy_approved_record_without_image_requires_real_upload(browser):
    client, registration, engine = browser
    store = app.dependency_overrides[images.get_image_store]()
    assert m2.submit_form(client).status_code == 303
    original = saved_state(engine)
    key = original["id"]
    m2.review_record(engine, key, 1, "ON_REVIEW")
    m2.review_record(engine, key, 2, "APPROVED")
    with Session(engine) as db:
        profile = db.get(CreatorProfile, key)
        image = db.get(ProfileImage, profile.image_id)
        store.objects.pop(image.object_key)
        profile.image_id = None
        profile.channel_name = "Legacy creator"
        db.flush()
        db.delete(image)
        db.commit()
        with pytest.raises(HTTPException) as error:
            asyncio.run(exports.export(db, 2, 1, registration, store))
        assert "Missing image_id" in str(error.value.detail)
    path = f"/creators/{key}"
    page = client.get(path)
    assert "No picture has been supplied" in page.text and "Upload picture" in page.text
    assert (
        client.post(
            path + "/picture",
            data={"csrf_token": m2.csrf(client, path), "version": 3},
            files={"picture": ("actual.png", m2.application_picture())},
            follow_redirects=False,
        ).status_code
        == 303
    )
    with Session(engine) as db:
        assert db.get(CreatorApplication, key).status == "APPROVED"
        assert db.scalar(select(Badge)).badge_number == 1
        assert db.get(CreatorProfile, key).image_id is not None
        assert asyncio.run(exports.export(db, 2, 1, registration, store))


def test_profile_validation_retains_values_and_related_data(browser):
    client, _, engine = browser
    assert m2.submit_form(client).status_code == 303
    original = saved_state(engine)
    key = original["id"]
    m2.review_record(engine, key, 1, "ON_REVIEW")
    m2.review_record(engine, key, 2, "APPROVED")
    path = f"/creators/{key}"
    response = client.post(
        path + "/profile",
        data={
            "csrf_token": m2.csrf(client, path),
            "version": 3,
            "channel_name": "Entered name",
            "platform": "YouTube",
            "account": "https://youtube.com/watch?v=bad",
            "primary": "0",
        },
    )
    assert response.status_code == 422
    assert 'value="Entered name"' in response.text
    assert 'value="https://youtube.com/watch?v=bad"' in response.text
    assert f'src="{path}/picture"' in response.text
    for field in ("image_id", "videos", "channels", "content"):
        assert saved_state(engine)[field] == original[field]


def test_header_home_logout_and_current_authorization(browser):
    client, _, engine = browser
    for user, admin, staff in ((1, False, False), (2, True, True), (3, False, True)):
        m2.login(client, user)
        page = client.get("/")
        assert page.status_code == 200
        assert "Logout" in page.text and 'action="/auth/logout"' in page.text
        assert 'href="/applications"' in page.text
        assert ('href="/admin/applications"' in page.text) == admin
        assert ('href="/staff"' in page.text) == staff
        assert "Session</a>" not in page.text and "Your session" not in page.text
        assert page.headers["cache-control"] == "no-store"
    m2.login(client, 2)
    with Session(engine) as db:
        db.execute(delete(LocalRoleAssignment).where(LocalRoleAssignment.user_id == 2))
        db.commit()
    assert 'href="/admin/applications"' not in client.get("/").text
    assert client.get("/admin/applications").status_code == 403
    assert client.get("/auth/logout").status_code == 405
    assert client.post("/auth/logout").status_code == 403
    assert (
        client.post("/auth/logout", data={"csrf_token": "invalid"}).status_code == 403
    )
    assert client.get("/auth/me").status_code == 200
    response = client.post(
        "/auth/logout",
        data={"csrf_token": m2.csrf(client, "/")},
        follow_redirects=False,
    )
    assert response.status_code == 303 and response.headers["location"] == "/"
    page = client.get("/")
    assert "Login" in page.text and "Logout" not in page.text
    assert 'href="/admin/applications"' not in page.text
    assert client.get("/auth/me").status_code == 401
    assert client.get("/account", follow_redirects=False).headers["location"] == "/"
    invitation = client.get("/helpers/redeem")
    assert "new tab" in invitation.text
    assert 'href="/auth/login" target="_blank"' in invitation.text


@pytest.mark.parametrize("failure", ["reason", "conflict", "registration"])
def test_review_errors_render_html_and_keep_notes_without_saving(browser, failure):
    client, registration, engine = browser
    assert m2.submit_form(client).status_code == 303
    key = saved_state(engine)["id"]
    if failure == "registration":
        m2.review_record(engine, key, 1, "ON_REVIEW")
        registration.failure = m2.RegistrationUnavailable()
    before = saved_state(engine)
    with Session(engine) as db:
        previous_notes = db.get(CreatorApplication, key).staff_notes
    m2.login(client, 2)
    path = f"/admin/applications/{key}"
    version = 0 if failure == "conflict" else before["version"]
    target = "APPROVED" if failure == "registration" else "NOT_ACCEPTED"
    response = client.post(
        path,
        data={
            "csrf_token": m2.csrf(client, path),
            "version": version,
            "status": target,
            "reason": "" if failure == "reason" else "Entered review reason",
            "staff_notes": "Keep these notes <script>unsafe()</script>",
            "exceptional": "yes",
        },
    )
    assert (
        response.status_code
        == {
            "reason": 422,
            "conflict": 409,
            "registration": 503,
        }[failure]
    )
    assert response.headers["content-type"].startswith("text/html")
    assert 'role="alert"' in response.text
    assert "Keep these notes &lt;script&gt;unsafe()&lt;/script&gt;" in response.text
    assert "<script>unsafe()" not in response.text
    assert f'name="version" value="{version}"' in response.text
    assert f'value="{target}" selected' in response.text
    assert 'name="exceptional" value="yes" checked' in response.text
    assert saved_state(engine) == before
    with Session(engine) as db:
        assert db.get(CreatorApplication, key).staff_notes == previous_notes
        assert db.scalar(select(Badge)) is None
    for actor in (1, 3):
        m2.login(client, actor)
        assert client.post(path, data={"status": target}).status_code == 403
    m2.login(client, 2)
    assert client.post(path, data={"status": target}).status_code == 403


def test_profile_completion_and_english_picture_controls(browser):
    client, _, engine = browser
    page = client.get("/applications/form")
    assert "Choose PNG picture" in page.text
    assert "No file selected" in page.text
    assert 'aria-label="Choose PNG picture"' in page.text
    assert "/application-assets/picture.js" in page.text
    assert m2.submit_form(client).status_code == 303
    key = saved_state(engine)["id"]
    assert "Choose PNG picture" in client.get("/applications/form").text
    m2.review_record(engine, key, 1, "ON_REVIEW")
    m2.review_record(engine, key, 2, "APPROVED")
    page = client.get("/applications")
    assert "Your public creator name is still missing" in page.text
    path = f"/creators/{key}"
    assert "Choose PNG picture" in client.get(path).text
    assert (
        client.post(
            path + "/profile",
            data={
                "csrf_token": m2.csrf(client, path),
                "version": 3,
                "channel_name": "Ready creator",
            },
            follow_redirects=False,
        ).status_code
        == 303
    )
    assert (
        "Your public creator name is still missing"
        not in client.get("/applications").text
    )
