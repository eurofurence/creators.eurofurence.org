from datetime import UTC, datetime, timedelta
from functools import partial
from urllib.parse import parse_qs, urlparse

import httpx2
import pytest
import test_applications as m2
import test_auth as auth
import test_registration_v1 as contract
from sqlalchemy import select

from app.auth import verification
from app.config import settings
from app.events.models import Event
from app.identity.models import ExternalIdentity
from app.registration.client import RegistrationLookup
from app.registration.models import RegistrationVerification
from app.registration.v1 import RegistrationV1Client

key = auth.key
provider = auth.provider
db = auth.db
client = auth.client


@pytest.fixture
def registration_login(client, provider, db, monkeypatch):
    now = datetime.now(UTC)
    db.add(
        Event(
            id=7,
            year=2028,
            name="Test event",
            starts_at=now,
            ends_at=now + timedelta(days=1),
            application_open_at=now - timedelta(days=1),
            application_close_at=now + timedelta(days=1),
            badge_print_at=now + timedelta(days=1),
            data_delete_at=now + timedelta(days=31),
        )
    )
    db.commit()
    monkeypatch.setattr(settings, "active_event_id", 7)
    route = contract.event_config(
        auth_method="user_bearer",
        credential=None,
        identity_mapping="confirmed_user_token",
    )
    monkeypatch.setattr(settings, "registration_provider", "v1")
    monkeypatch.setattr(settings, "registration_v1_events", [route])
    service = contract.Service(event=route)
    monkeypatch.setattr(
        verification,
        "RegistrationV1Client",
        partial(RegistrationV1Client, transport=service.transport),
    )
    auth.begin(client, provider)
    assert auth.finish(client, provider).status_code == 303
    assert service.requests == []  # Ordinary login has no eligibility side effect.
    return client, provider, db, service


def begin_verification(client, provider, purpose="creator"):
    page = f"/auth/registration?purpose={purpose}"
    response = client.post(
        "/auth/registration",
        data={
            "csrf_token": m2.csrf(client, page),
            "purpose": purpose,
            "user_id": "999",
            "event_id": "999",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302
    provider.query = parse_qs(urlparse(response.headers["location"]).query)
    assert provider.query["scope"] == ["openid"]  # No invented Registration scope.


@pytest.mark.parametrize("purpose", ["creator", "helper"])
def test_fresh_token_verification_saves_only_bound_evidence(
    registration_login, purpose, caplog
):
    client, provider, db, service = registration_login
    begin_verification(client, provider, purpose)
    response = auth.finish(client, provider)
    assert response.status_code == 303
    assert response.headers["location"] == f"/auth/registration?purpose={purpose}"
    row = db.scalar(select(RegistrationVerification))
    assert (
        row.user_id,
        row.purpose,
        row.event_id,
        row.event_year,
        row.issuer,
        row.subject,
        row.reg_id,
        row.status,
    ) == (1, purpose, 7, 2028, settings.oidc_issuer_url, "person-123", "41", "PAID")
    assert row.verified_at is not None
    assert [request.url.path for request in service.requests] == [
        "/event-test/attsrv/api/rest/v1/attendees",
        "/event-test/attsrv/api/rest/v1/attendees/41",
        "/event-test/attsrv/api/rest/v1/attendees/41/status",
        "/event-test/attsrv/api/rest/v1/attendees",
    ]
    assert all(
        request.headers["authorization"] == "Bearer private-access-token"
        for request in service.requests
    )
    assert (
        "private-access-token"
        not in str(auth.session_data(client))
        + str(row.__dict__)
        + caplog.text
        + response.text
    )
    assert set(auth.session_data(client)) == {"user_id", "identity_key", "csrf"}
    assert "Registration verified" in client.get(response.headers["location"]).text
    # Success is reused, even when the remote service would now fail.
    service.overrides[0] = httpx2.Response(503)
    response = client.post(
        "/auth/registration",
        data={
            "csrf_token": m2.csrf(client, response.headers["location"]),
            "purpose": purpose,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303 and len(service.requests) == 4
    db.expire_all()
    assert db.scalar(select(RegistrationVerification)).verified_at == row.verified_at


@pytest.mark.parametrize(
    "failure",
    [
        "unavailable",
        "ineligible",
        "ambiguous",
        "missing",
        "unknown",
        "details_forbidden",
    ],
)
def test_callback_failures_preserve_login_and_allow_retry(registration_login, failure):
    client, provider, db, service = registration_login
    if failure == "unavailable":
        service.overrides[0] = httpx2.Response(503)
    elif failure == "ineligible":
        service.status = "cancelled"
    elif failure == "unknown":
        service.status = "new-future-status"
    elif failure == "details_forbidden":
        service.overrides[1] = httpx2.Response(403)
    else:
        service.overrides[0] = httpx2.Response(
            200, json={"ids": [41, 42] if failure == "ambiguous" else []}
        )
    begin_verification(client, provider)
    response = auth.finish(client, provider)
    assert response.status_code == 303
    assert client.get("/auth/me").status_code == 200
    assert db.scalar(select(RegistrationVerification)) is None
    page = client.get(response.headers["location"])
    assert 'role="alert"' in page.text and "Verify Registration" in page.text
    assert "private-access-token" not in str(auth.session_data(client))
    service.status = "checked in"
    service.requests.clear()
    service.overrides.clear()
    begin_verification(client, provider)
    assert auth.finish(client, provider).status_code == 303
    assert db.scalar(select(RegistrationVerification.status)) == "CHECKED_IN"


@pytest.mark.parametrize(
    "change",
    ["account", "event", "identity", "token", "token_type", "configuration", "grant"],
)
def test_callback_context_changes_never_save_evidence(
    registration_login, change, monkeypatch
):
    client, provider, db, service = registration_login
    begin_verification(client, provider)
    if change == "account":
        provider.claims["sub"] = "other-person"
    elif change == "event":
        db.get(Event, 7).year += 1
        db.commit()
    elif change == "identity":
        db.add(
            ExternalIdentity(
                user_id=1, issuer=settings.oidc_issuer_url, subject="second"
            )
        )
        db.commit()
    elif change == "token":
        provider.token_updates["access_token"] = ""
    elif change == "token_type":
        provider.token_updates["token_type"] = "NotBearer"
    elif change == "configuration":
        monkeypatch.setattr(settings, "registration_provider", "unavailable")
    else:
        provider.claims["nonce"] = "wrong"
    response = auth.finish(client, provider)
    assert response.status_code == (401 if change in ("account", "grant") else 303)
    assert db.scalar(select(RegistrationVerification)) is None
    assert service.requests == []
    if change in ("account", "grant"):
        assert client.cookies.get("creator_session") is None
    else:
        assert "private-access-token" not in str(auth.session_data(client))


def test_unconfirmed_user_token_mapping_does_not_start_authentication(
    registration_login, monkeypatch
):
    client, _, db, service = registration_login
    monkeypatch.setattr(
        settings,
        "registration_v1_events",
        [
            contract.event_config(
                auth_method="user_bearer",
                credential=None,
                identity_mapping="unconfirmed",
            )
        ],
    )
    response = client.post(
        "/auth/registration",
        data={
            "purpose": "creator",
            "csrf_token": m2.csrf(client, "/auth/registration"),
        },
    )
    assert "not configured for this event" in response.text
    assert (
        service.requests == [] and db.scalar(select(RegistrationVerification)) is None
    )


def test_user_token_adapter_cannot_verify_another_identity():
    route = contract.event_config(
        auth_method="user_bearer",
        credential=None,
        identity_mapping="confirmed_user_token",
    )
    service = contract.Service(event=route)
    from pydantic import SecretStr

    from app.registration.client import RegistrationUnavailable

    service.client.user_token = SecretStr("test-user-token")
    service.client.token_lookup = RegistrationLookup(
        contract.LOOKUP.issuer, "other", 7, 2028
    )
    with pytest.raises(RegistrationUnavailable):
        service.lookup()
    assert service.requests == []


def test_open_application_form_can_submit_after_same_account_verification(
    registration_login,
):
    client, provider, _, service = registration_login
    original_csrf = m2.csrf(client, "/applications/form")
    begin_verification(client, provider)
    assert auth.finish(client, provider).status_code == 303
    response = client.post(
        "/applications/form",
        data={
            "csrf_token": original_csrf,
            "content_type": "VLOGS",
            "platform": "Twitch",
            "account": "creator",
            "primary": "0",
            "video_url": "https://video.example/convention",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    assert len(service.requests) == 4


@pytest.mark.parametrize("token", [None, "", "bad\nheader", "valid-test-token"])
def test_user_token_mode_stays_disabled_for_unverified_production(token):
    from pydantic import SecretStr

    from app.registration.client import RegistrationUnavailable

    route = contract.event_config(
        auth_method="user_bearer",
        credential=None,
        identity_mapping="confirmed_user_token",
    )
    service = contract.Service(event=route, environment="production")
    service.client.user_token = SecretStr(token) if token is not None else None
    service.client.token_lookup = contract.LOOKUP
    with pytest.raises(RegistrationUnavailable):
        service.lookup()
    assert service.requests == []
