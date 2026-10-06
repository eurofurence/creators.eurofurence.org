import asyncio
import base64
import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.database import Base, get_db
from app.events.models import Event
from app.identity.models import ExternalIdentity, LocalUser
from app.main import app
from app.registration.client import (
    RegistrationLookup,
    RegistrationResult,
    RegistrationStatus,
    RegistrationUnavailable,
    get_registration_client,
)
from app.registration.eligibility import (
    Eligibility,
    EligibilityRequired,
    check_eligibility,
    require_eligible_registration,
)


class FakeRegistrationClient:
    """Deterministic internal adapter fake, not an EF response schema."""

    def __init__(self, status=RegistrationStatus.PAID):
        self.status = status
        self.calls = []
        self.failure = None
        self.result_lookup = None

    async def lookup(self, lookup):
        self.calls.append(lookup)
        if self.failure:
            raise self.failure
        return RegistrationResult(self.result_lookup or lookup, self.status)


LOOKUP = RegistrationLookup("https://identity.example/", "person-123", 7, 2028)


@pytest.mark.parametrize(
    "status,expected",
    [
        (RegistrationStatus.PAID, Eligibility.ELIGIBLE),
        (RegistrationStatus.CHECKED_IN, Eligibility.ELIGIBLE),
        (RegistrationStatus.INELIGIBLE, Eligibility.INELIGIBLE),
        (RegistrationStatus.UNKNOWN, Eligibility.UNAVAILABLE),
        (None, Eligibility.UNAVAILABLE),
        ("paid", Eligibility.UNAVAILABLE),
    ],
)
def test_status_policy_and_guard(status, expected):
    client = FakeRegistrationClient(status)
    assert asyncio.run(check_eligibility(client, LOOKUP)) is expected
    if expected is Eligibility.ELIGIBLE:
        asyncio.run(require_eligible_registration(client, LOOKUP))
    else:
        with pytest.raises(EligibilityRequired) as error:
            asyncio.run(require_eligible_registration(client, LOOKUP))
        assert error.value.eligibility is expected


@pytest.mark.parametrize(
    "change",
    [
        {"issuer": "https://other.example/"},
        {"subject": "other"},
        {"event_id": 8},
        {"event_year": 2029},
    ],
)
def test_wrong_person_or_event_never_grants_eligibility(change):
    client = FakeRegistrationClient()
    client.result_lookup = replace(LOOKUP, **change)
    assert asyncio.run(check_eligibility(client, LOOKUP)) is Eligibility.UNAVAILABLE


def test_each_guard_call_rechecks_instead_of_trusting_earlier_eligibility():
    client = FakeRegistrationClient()
    asyncio.run(require_eligible_registration(client, LOOKUP))
    client.status = RegistrationStatus.INELIGIBLE
    with pytest.raises(EligibilityRequired):
        asyncio.run(require_eligible_registration(client, LOOKUP))
    client.failure = RegistrationUnavailable("private provider payload")
    with pytest.raises(EligibilityRequired) as error:
        asyncio.run(require_eligible_registration(client, LOOKUP))
    assert error.value.eligibility is Eligibility.UNAVAILABLE
    assert len(client.calls) == 3


@pytest.fixture
def eligibility_setup(monkeypatch):
    monkeypatch.setattr(settings, "active_event_id", 7)
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        date = datetime(2028, 9, 1, tzinfo=UTC)
        db.add(
            Event(
                id=7,
                year=2028,
                name="Test event",
                starts_at=date,
                ends_at=date,
                badge_print_at=date,
                application_open_at=date,
                application_close_at=date,
                data_delete_at=date,
            )
        )
        db.add(LocalUser(id=1))
        db.flush()
        db.add(
            ExternalIdentity(user_id=1, issuer=LOOKUP.issuer, subject=LOOKUP.subject)
        )
        db.commit()
        adapter = FakeRegistrationClient()
        app.dependency_overrides[get_db] = lambda: db
        app.dependency_overrides[get_registration_client] = lambda: adapter
        try:
            with TestClient(app, base_url="http://127.0.0.1:8000") as client:
                payload = base64.b64encode(
                    json.dumps(
                        {"user_id": 1, "identity_key": db.get(LocalUser, 1).session_key}
                    ).encode()
                )
                cookie = (
                    TimestampSigner(settings.session_secret.get_secret_value())
                    .sign(payload)
                    .decode()
                )
                client.cookies.set("creator_session", cookie)
                yield client, adapter, db
        finally:
            app.dependency_overrides.clear()
    engine.dispose()


@pytest.mark.parametrize(
    "status,expected,code",
    [
        (RegistrationStatus.PAID, "ELIGIBLE", 200),
        (RegistrationStatus.CHECKED_IN, "ELIGIBLE", 200),
        (RegistrationStatus.INELIGIBLE, "INELIGIBLE", 200),
        (RegistrationStatus.UNKNOWN, "UNAVAILABLE", 503),
    ],
)
def test_authenticated_endpoint_is_private_and_bound_to_configured_event(
    eligibility_setup, status, expected, code
):
    client, adapter, db = eligibility_setup
    adapter.status = status
    response = client.get(
        "/applications/eligibility?user_id=999&event_id=999&status=paid"
    )
    assert response.status_code == code
    assert response.json() == {"status": expected, "retryable": code == 503}
    assert response.headers["cache-control"] == "no-store"
    assert adapter.calls == [LOOKUP]
    # Only local identity was stored: name, email and check-in are not prerequisites.
    assert db.get(LocalUser, 1) is not None


def test_anonymous_cannot_lookup_registration(eligibility_setup):
    client, adapter, _ = eligibility_setup
    client.cookies.clear()
    assert client.get("/applications/eligibility").status_code == 401
    assert adapter.calls == []


@pytest.mark.parametrize("active_event_id", [None, 999])
def test_missing_active_event_is_unavailable_not_calendar_fallback(
    eligibility_setup, monkeypatch, active_event_id
):
    client, adapter, _ = eligibility_setup
    monkeypatch.setattr(settings, "active_event_id", active_event_id)
    response = client.get("/applications/eligibility")
    assert response.status_code == 503
    assert response.json() == {"status": "UNAVAILABLE", "retryable": True}
    assert adapter.calls == []


@pytest.mark.parametrize("identity_problem", ["missing", "ambiguous", "wrong_issuer"])
def test_missing_or_ambiguous_identity_fails_closed(
    eligibility_setup, identity_problem
):
    client, adapter, db = eligibility_setup
    identity = db.scalar(select(ExternalIdentity))
    if identity_problem == "missing":
        db.delete(identity)
    elif identity_problem == "wrong_issuer":
        identity.issuer = "https://other.example/"
    else:
        db.add(ExternalIdentity(user_id=1, issuer=LOOKUP.issuer, subject="second"))
    db.commit()
    assert client.get("/applications/eligibility").status_code == 503
    assert adapter.calls == []


@pytest.mark.parametrize("failure", [RegistrationUnavailable, TimeoutError])
def test_outage_is_retryable_and_does_not_leak_provider_details(
    eligibility_setup, caplog, failure
):
    client, adapter, db = eligibility_setup
    adapter.failure = failure("private provider payload")
    response = client.get("/applications/eligibility")
    assert response.status_code == 503
    assert response.json() == {"status": "UNAVAILABLE", "retryable": True}
    assert "private provider payload" not in response.text + caplog.text
    assert db.get(LocalUser, 1) is not None
    assert not db.new and not db.deleted and not db.dirty


def test_unconfigured_adapter_is_unavailable_even_with_valid_identity(
    eligibility_setup,
):
    client, adapter, _ = eligibility_setup
    app.dependency_overrides.pop(get_registration_client)
    response = client.get("/applications/eligibility")
    assert response.status_code == 503
    assert response.json() == {"status": "UNAVAILABLE", "retryable": True}
    assert adapter.calls == []
