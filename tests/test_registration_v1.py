import asyncio
import gzip
import json
import logging
from dataclasses import asdict, replace

import httpx2
import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.config import Settings, settings
from app.identity.models import ExternalIdentity
from app.identity.profile import UnavailableIdentityProfileClient
from app.main import app
from app.registration import v1
from app.registration.client import (
    RegistrationLookup,
    RegistrationStatus,
    RegistrationUnavailable,
    UnavailableRegistrationClient,
    get_registration_client,
)
from app.registration.configuration import RegistrationV1Event
from app.registration.eligibility import Eligibility, check_eligibility
from app.registration.evidence import verify_participant
from tests.test_eligibility import eligibility_setup  # noqa: F401

LOOKUP = RegistrationLookup("https://identity.example/", "Subject123", 7, 2028)
SECRET = "test-only-service-credential"
BASE = "https://registration.example/event-test/attsrv/api/rest/v1"


def event_config(**changes):
    return RegistrationV1Event(
        **{
            "event_id": LOOKUP.event_id,
            "event_year": LOOKUP.event_year,
            "issuer": LOOKUP.issuer,
            "base_url": BASE,
            "identity_mapping": "confirmed_oidc_subject",
            "auth_method": "api_key",
            "credential": SECRET,
            **changes,
        }
    )


class Service:
    """HTTP-level v1 contract double; no EF credentials or external networking."""

    def __init__(self, *, status="paid", event=None, environment="test"):
        self.status = status
        self.requests = []
        self.overrides = {}
        self.transport = httpx2.MockTransport(self.handle)
        self.client = v1.RegistrationV1Client(
            [event or event_config()], environment=environment, transport=self.transport
        )

    async def handle(self, request):
        logging.getLogger("httpcore2.http11").debug("private-response-header")
        index = len(self.requests) % 4
        self.requests.append(request)
        payload = [
            {"ids": [41]},
            {"id": 41, "nickname": "Furry Attendee", "email": "private@example.test"},
            {"status": self.status},
            {"ids": [41]},
        ][index]
        response = self.overrides.get(index, httpx2.Response(200, json=payload))
        if isinstance(response, Exception):
            raise response
        return response

    def lookup(self, lookup=LOOKUP):
        return asyncio.run(self.client.lookup(lookup))


@pytest.mark.parametrize("auth", ["api_key", "bearer"])
@pytest.mark.parametrize(
    "status,expected",
    [
        ("paid", RegistrationStatus.PAID),
        ("checked in", RegistrationStatus.CHECKED_IN),
        ("new", RegistrationStatus.INELIGIBLE),
        ("approved", RegistrationStatus.INELIGIBLE),
        ("partially paid", RegistrationStatus.INELIGIBLE),
        ("cancelled", RegistrationStatus.INELIGIBLE),
        ("deleted", RegistrationStatus.INELIGIBLE),
        ("waiting", RegistrationStatus.INELIGIBLE),
        ("future-status", RegistrationStatus.UNKNOWN),
        ("PAID", RegistrationStatus.UNKNOWN),
        ("paid ", RegistrationStatus.UNKNOWN),
        ("checked_in", RegistrationStatus.UNKNOWN),
    ],
)
def test_wire_statuses_credentials_and_minimal_snapshot(auth, status, expected, caplog):
    caplog.set_level(logging.DEBUG)
    service = Service(status=status, event=event_config(auth_method=auth))
    result = service.lookup()
    assert result.lookup == LOOKUP
    assert result.status is expected
    assert result.reg_id == "41" and result.nickname == "Furry Attendee"
    assert "email" not in asdict(result)
    suffixes = (
        "attendees/identity/Subject123",
        "attendees/41",
        "attendees/41/status",
        "attendees/identity/Subject123",
    )
    assert [str(r.url) for r in service.requests] == [f"{BASE}/{p}" for p in suffixes]
    for request in service.requests:
        assert request.method == "GET" and not request.content
        assert request.headers["accept"] == "application/json"
        assert request.headers["accept-encoding"] == "identity"
        assert "cookie" not in request.headers
        assert not request.url.query
        assert all(
            value == v1.REQUEST_TIMEOUT
            for value in request.extensions["timeout"].values()
        )
        if auth == "api_key":
            assert request.headers["x-api-key"] == SECRET
            assert "authorization" not in request.headers
        else:
            assert request.headers["authorization"] == f"Bearer {SECRET}"
            assert "x-api-key" not in request.headers
    assert LOOKUP.subject not in caplog.text and SECRET not in caplog.text
    assert "private@example.test" not in caplog.text
    assert "private-response-header" not in caplog.text
    expected_eligibility = (
        Eligibility.ELIGIBLE
        if expected in (RegistrationStatus.PAID, RegistrationStatus.CHECKED_IN)
        else Eligibility.INELIGIBLE
        if expected is RegistrationStatus.INELIGIBLE
        else Eligibility.UNAVAILABLE
    )
    assert (
        asyncio.run(check_eligibility(service.client, LOOKUP)) is expected_eligibility
    )


@pytest.mark.parametrize(
    "change",
    [
        {"event_id": 8},
        {"event_year": 2029},
        {"issuer": "https://different-issuer.example/"},
        {"issuer": LOOKUP.issuer.rstrip("/")},
        {"subject": ""},
        {"subject": "../41"},
        {"subject": "user?id=41"},
        {"subject": "with-hyphen"},
        {"subject": "üser"},
        {"subject": "%2F"},
    ],
)
def test_unbound_event_or_identity_never_makes_request(change):
    service = Service()
    with pytest.raises(RegistrationUnavailable):
        service.lookup(replace(LOOKUP, **change))
    assert not service.requests


@pytest.mark.parametrize("routes", [[], [event_config(), event_config()]])
def test_missing_or_ambiguous_routes_fail_closed(routes):
    service = Service()
    service.client = v1.RegistrationV1Client(
        routes, environment="test", transport=service.transport
    )
    with pytest.raises(RegistrationUnavailable):
        service.lookup()
    assert not service.requests


@pytest.mark.parametrize(
    "environment", ["development", "test", "staging", "production", "unknown"]
)
def test_mapping_confirmation_is_required_in_every_environment(environment):
    service = Service(
        event=event_config(identity_mapping="unconfirmed", production_verified=True),
        environment=environment,
    )
    with pytest.raises(RegistrationUnavailable):
        service.lookup()
    assert not service.requests


@pytest.mark.parametrize("environment", ["production", "unknown"])
def test_production_requires_explicit_verification(environment):
    service = Service(environment=environment)
    with pytest.raises(RegistrationUnavailable):
        service.lookup()
    assert not service.requests
    verified = Service(
        event=event_config(production_verified=True), environment=environment
    )
    assert verified.lookup().status is RegistrationStatus.PAID


def test_event_routing_and_credentials_remain_separate():
    service = Service()
    second = event_config(
        event_id=8,
        event_year=2029,
        base_url="https://other-event.example/api/rest/v1/",
        credential="other-event-secret",
    )
    service.client = v1.RegistrationV1Client(
        [event_config(), second], environment="test", transport=service.transport
    )
    assert service.lookup().lookup == LOOKUP
    other = replace(LOOKUP, event_id=8, event_year=2029)
    assert service.lookup(other).lookup == other
    assert all(
        r.url.host == "registration.example" and r.headers["x-api-key"] == SECRET
        for r in service.requests[:4]
    )
    assert all(
        r.url.host == "other-event.example"
        and r.headers["x-api-key"] == "other-event-secret"
        for r in service.requests[4:]
    )


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"data": {"ids": [41]}},
        {"ids": []},
        {"ids": [41, 42]},
        {"ids": [41, 41]},
        {"ids": [True]},
        {"ids": ["41"]},
        {"ids": [41.0]},
        {"ids": [0]},
        {"ids": [-1]},
        {"ids": [2**63]},
        {"ids": None},
    ],
)
@pytest.mark.parametrize("index", [0, 3])
def test_missing_ambiguous_or_malformed_identity_evidence(payload, index):
    service = Service()
    service.overrides[index] = httpx2.Response(200, json=payload)
    assert (
        asyncio.run(check_eligibility(service.client, LOOKUP))
        is Eligibility.UNAVAILABLE
    )
    assert len(service.requests) == index + 1


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"id": 42, "nickname": "Other attendee"},
        {"id": "41", "nickname": "Person"},
        {"id": True, "nickname": "Person"},
        {"id": 41},
        {"id": 41, "nickname": None},
        {"id": 41, "nickname": ""},
        {"id": 41, "nickname": "   "},
        {"id": 41, "nickname": "n" * 81},
    ],
)
def test_wrong_or_malformed_attendee(payload):
    service = Service()
    service.overrides[1] = httpx2.Response(200, json=payload)
    with pytest.raises(RegistrationUnavailable):
        service.lookup()
    assert len(service.requests) == 2


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"status": None},
        {"status": True},
        {"status": 1},
        {"data": {"status": "paid"}},
    ],
)
def test_malformed_status_is_unavailable(payload):
    service = Service()
    service.overrides[2] = httpx2.Response(200, json=payload)
    with pytest.raises(RegistrationUnavailable):
        service.lookup()


def test_owner_changes_during_lookup_and_status_is_not_cached():
    service = Service()
    assert service.lookup().status is RegistrationStatus.PAID
    service.status = "cancelled"
    assert service.lookup().status is RegistrationStatus.INELIGIBLE
    service.overrides[3] = httpx2.Response(200, json={"ids": [42]})
    assert (
        asyncio.run(check_eligibility(service.client, LOOKUP))
        is Eligibility.UNAVAILABLE
    )


@pytest.mark.parametrize(
    "status", [301, 302, 307, 308, 400, 401, 403, 404, 429, 500, 503]
)
@pytest.mark.parametrize("index", [0, 1, 2, 3])
def test_http_failures_and_redirects_are_unavailable_without_retries(
    index, status, caplog
):
    caplog.set_level(logging.DEBUG)
    service = Service()
    service.overrides[index] = httpx2.Response(
        status,
        json={"message": "private-provider-payload"},
        headers={"Location": "https://untrusted.example/", "Retry-After": "120"},
    )
    with pytest.raises(RegistrationUnavailable) as error:
        service.lookup()
    assert str(error.value) == "" and error.value.__cause__ is None
    assert len(service.requests) == index + 1
    assert "private-provider-payload" not in caplog.text
    assert LOOKUP.subject not in caplog.text and SECRET not in caplog.text


@pytest.mark.parametrize(
    "failure",
    [
        httpx2.ConnectError,
        httpx2.ReadTimeout,
        httpx2.RemoteProtocolError,
        httpx2.InvalidURL,
    ],
)
@pytest.mark.parametrize("index", [0, 1, 2, 3])
def test_transport_errors_are_sanitized(failure, index):
    service = Service()
    service.overrides[index] = failure("private-service-detail")
    with pytest.raises(RegistrationUnavailable) as error:
        service.lookup()
    assert str(error.value) == "" and error.value.__suppress_context__


@pytest.mark.parametrize(
    "body,content_type",
    [
        (b"not json", "application/json"),
        (b'{"ids":[41],"ids":[42]}', "application/json"),
        (b'{"ids":[41]}', "text/html"),
        (b'{"ids":[41]}', ""),
        (b"[" * 2000, "application/json"),
    ],
)
def test_invalid_response_content(body, content_type):
    service = Service()
    service.overrides[0] = httpx2.Response(
        200, content=body, headers={"Content-Type": content_type}
    )
    with pytest.raises(RegistrationUnavailable):
        service.lookup()


class Stream(httpx2.AsyncByteStream):
    def __init__(self, chunks, *, delay=0):
        self.chunks, self.delay, self.closed = chunks, delay, False

    async def __aiter__(self):
        for chunk in self.chunks:
            await asyncio.sleep(self.delay)
            yield chunk

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("problem", ["oversize", "compressed", "slow"])
def test_bounded_responses_and_total_deadline_close_streams(problem, monkeypatch):
    service = Service()
    headers = {"Content-Type": "application/json"}
    if problem == "slow":
        monkeypatch.setattr(v1, "LOOKUP_TIMEOUT", 0.02)
        stream = Stream([b'{"ids":[41]}'], delay=1)
    else:
        body = b" " * (v1.MAX_RESPONSE_BYTES + 1)
        if problem == "compressed":
            headers["Content-Encoding"] = "gzip"
            body = gzip.compress(body)
        stream = Stream([body])
    service.overrides[0] = httpx2.Response(200, headers=headers, stream=stream)
    with pytest.raises(RegistrationUnavailable):
        service.lookup()
    assert stream.closed


def test_logging_filter_is_scoped_to_adapter_requests(caplog):
    caplog.set_level(logging.INFO, logger="httpx2")
    service = Service()
    service.lookup()
    assert LOOKUP.subject not in caplog.text
    logging.getLogger("httpx2").info("unrelated-http-log")
    assert "unrelated-http-log" in caplog.text


@pytest.mark.parametrize(
    "change",
    [
        {"base_url": "http://registration.example/api/rest/v1"},
        {"base_url": "https://user:private-password@registration.example/"},
        {"base_url": "https://@registration.example/"},
        {"base_url": "https://registration.example/?secret=private-value"},
        {"base_url": "https://registration.example/#fragment"},
        {"base_url": "https://registration.example:invalid/"},
        {"base_url": "https://registration.example:99999/"},
        {"base_url": "https://registration.example/\n"},
        {"base_url": "https://registration.example\\other/"},
        {"base_url": "/api/rest/v1"},
        {"issuer": "http://identity.example/"},
        {"event_id": True},
        {"event_id": 0},
        {"event_year": "2028"},
        {"auth_method": "basic"},
        {"auth_method": "client_credentials"},
        {"credential": ""},
        {"credential": "INSERT_SECRET"},
        {"credential": "private-value\nheader"},
        {"identity_mapping": "email"},
        {"production_verified": "false"},
    ],
)
def test_invalid_route_configuration_hides_inputs(change):
    with pytest.raises(ValidationError) as error:
        event_config(**change)
    assert SECRET not in str(error.value)
    assert "private-" not in str(error.value)


def test_settings_load_json_routes_and_default_activation_is_closed(monkeypatch):
    route = json.loads(event_config().model_dump_json())
    route["credential"] = SECRET
    route.pop("identity_mapping")
    monkeypatch.setenv("REGISTRATION_V1_EVENTS", json.dumps([route]))
    configured = Settings(_env_file=None)
    assert configured.registration_provider == "unavailable"
    assert configured.registration_v1_events[0].identity_mapping == "unconfirmed"
    assert not configured.registration_v1_events[0].production_verified
    assert SECRET not in repr(configured)
    monkeypatch.setattr(
        settings, "registration_v1_events", configured.registration_v1_events
    )
    assert isinstance(get_registration_client(), UnavailableRegistrationClient)
    monkeypatch.setattr(settings, "registration_provider", "v1")
    adapter = get_registration_client()
    assert isinstance(adapter, v1.RegistrationV1Client)
    assert asyncio.run(check_eligibility(adapter, LOOKUP)) is Eligibility.UNAVAILABLE
    monkeypatch.setenv("REGISTRATION_V1_EVENTS", json.dumps([route, route]))
    with pytest.raises(ValidationError, match="unique event IDs") as error:
        Settings(_env_file=None)
    assert SECRET not in str(error.value)


def test_existing_eligibility_endpoint_uses_v1_without_exposing_attendee_data(
    eligibility_setup,  # noqa: F811
    monkeypatch,
    caplog,
):
    client, _, db = eligibility_setup
    db.scalar(select(ExternalIdentity)).subject = LOOKUP.subject
    db.commit()
    service = Service()
    constructor = v1.RegistrationV1Client
    monkeypatch.setattr(
        v1,
        "RegistrationV1Client",
        lambda events, environment: constructor(
            events, environment=environment, transport=service.transport
        ),
    )
    monkeypatch.setattr(settings, "registration_v1_events", [event_config()])
    monkeypatch.setattr(settings, "registration_provider", "v1")
    app.dependency_overrides.pop(get_registration_client)
    assert client.get("/applications/eligibility").status_code == 503
    assert service.requests == []
    asyncio.run(
        verify_participant(
            db,
            1,
            "creator",
            get_registration_client(),
            UnavailableIdentityProfileClient(),
        )
    )
    response = client.get("/applications/eligibility?event_id=999&user_id=999")
    assert response.json() == {"status": "ELIGIBLE", "retryable": False}
    assert response.headers["cache-control"] == "no-store"
    service.status = "future-status"
    count = len(service.requests)
    assert client.get("/applications/eligibility").status_code == 200
    assert len(service.requests) == count
    assert not db.new and not db.dirty and not db.deleted
    assert "private@example.test" not in response.text + caplog.text
