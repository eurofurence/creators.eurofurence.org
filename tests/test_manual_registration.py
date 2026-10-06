import asyncio
import json
import os
import subprocess
import sys
from dataclasses import asdict, replace
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings, settings
from app.main import app
from app.registration.client import (
    RegistrationLookup,
    RegistrationStatus,
    RegistrationUnavailable,
    UnavailableRegistrationClient,
    get_registration_client,
)
from app.registration.eligibility import Eligibility, check_eligibility
from app.registration.manual import ManualTestRegistrationClient
from tests.test_eligibility import eligibility_setup  # noqa: F401

LOOKUP = RegistrationLookup("https://identity.example/", "person-123", 7, 2028)


def write_records(path, status="PAID", **overrides):
    record = {
        **asdict(LOOKUP),
        "status": status,
        "reg_id": "LOCAL-123",
        "nickname": "Local attendee",
        **overrides,
    }
    path.write_text(json.dumps([record]), encoding="utf-8")


@pytest.mark.parametrize("environment", ["development", "test"])
@pytest.mark.parametrize(
    "status,expected",
    [
        ("PAID", Eligibility.ELIGIBLE),
        ("CHECKED_IN", Eligibility.ELIGIBLE),
        ("INELIGIBLE", Eligibility.INELIGIBLE),
        ("UNKNOWN", Eligibility.UNAVAILABLE),
        ("UNAVAILABLE", Eligibility.UNAVAILABLE),
    ],
)
def test_manual_statuses_and_snapshot(tmp_path, environment, status, expected):
    path = tmp_path / "registrations.json"
    write_records(path, status)
    client = ManualTestRegistrationClient(path, environment=environment)
    assert asyncio.run(check_eligibility(client, LOOKUP)) is expected
    if status != "UNAVAILABLE":
        result = asyncio.run(client.lookup(LOOKUP))
        assert result.lookup == LOOKUP
        assert result.reg_id == "LOCAL-123"
        assert result.nickname == "Local attendee"
        assert result.status is RegistrationStatus[status]


@pytest.mark.parametrize(
    "change",
    [
        {"issuer": "https://other.example/"},
        {"subject": "another-person"},
        {"event_id": 8},
        {"event_year": 2029},
    ],
)
def test_requires_exact_identity_and_event_binding(tmp_path, change):
    path = tmp_path / "registrations.json"
    write_records(path)
    client = ManualTestRegistrationClient(path, environment="development")
    assert (
        asyncio.run(check_eligibility(client, replace(LOOKUP, **change)))
        is Eligibility.UNAVAILABLE
    )


@pytest.mark.parametrize(
    "problem", ["missing", "malformed", "duplicate", "status", "extra", "oversize"]
)
def test_invalid_configuration_fails_closed_without_payloads(tmp_path, caplog, problem):
    path = tmp_path / "registrations.json"
    write_records(path)
    if problem == "missing":
        path.unlink()
    elif problem == "malformed":
        path.write_text("private-invalid-record", encoding="utf-8")
    elif problem == "duplicate":
        rows = json.loads(path.read_text())
        path.write_text(json.dumps(rows + rows), encoding="utf-8")
    elif problem == "status":
        write_records(path, "eligible")
    elif problem == "extra":
        write_records(path, unexpected="private-invalid-record")
    else:
        path.write_bytes(b" " * (1024 * 1024 + 1))
    client = ManualTestRegistrationClient(path, environment="development")
    with pytest.raises(RegistrationUnavailable) as error:
        asyncio.run(client.lookup(LOOKUP))
    assert str(error.value) == ""
    assert "private-invalid-record" not in caplog.text
    assert LOOKUP.subject not in caplog.text


def test_file_reloaded_and_default_never_activates_manual_provider(
    tmp_path, monkeypatch
):
    path = tmp_path / "registrations.json"
    write_records(path)
    monkeypatch.setattr(settings, "registration_manual_file", path)
    assert isinstance(get_registration_client(), UnavailableRegistrationClient)
    monkeypatch.setattr(settings, "registration_provider", "manual_test")
    client = get_registration_client()
    assert asyncio.run(check_eligibility(client, LOOKUP)) is Eligibility.ELIGIBLE
    write_records(path, "INELIGIBLE")
    assert asyncio.run(check_eligibility(client, LOOKUP)) is Eligibility.INELIGIBLE
    path.unlink()
    assert asyncio.run(check_eligibility(client, LOOKUP)) is Eligibility.UNAVAILABLE


@pytest.mark.parametrize("environment", ["staging", "production"])
@pytest.mark.parametrize("provider", ["unavailable", "manual_test"])
def test_real_application_startup_guard(tmp_path, environment, provider):
    result = subprocess.run(
        [sys.executable, "-c", "import app.main"],
        cwd=tmp_path,
        env={
            **os.environ,
            "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
            "ENVIRONMENT": environment,
            "REGISTRATION_PROVIDER": provider,
            "REGISTRATION_MANUAL_FILE": str(tmp_path / "registrations.json"),
        },
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    if provider == "manual_test":
        assert result.returncode != 0
        assert "manual_test Registration requires development or test" in result.stderr
    else:
        assert result.returncode == 0, result.stderr


def test_manual_requires_file_and_known_provider(tmp_path):
    with pytest.raises(ValidationError, match="requires REGISTRATION_MANUAL_FILE"):
        Settings(
            _env_file=None,
            environment="development",
            registration_provider="manual_test",
            registration_manual_file=None,
        )
    with pytest.raises(ValidationError):
        Settings(_env_file=None, registration_provider="auto")
    with pytest.raises(RuntimeError, match="development or test"):
        ManualTestRegistrationClient(
            tmp_path / "records.json", environment="production"
        )


def test_browser_uses_selected_provider_and_retryable_failure(
    eligibility_setup,  # noqa: F811
    tmp_path,
    monkeypatch,
):
    client, _, _ = eligibility_setup
    path = tmp_path / "registrations.json"
    write_records(path)
    monkeypatch.setattr(settings, "registration_provider", "manual_test")
    monkeypatch.setattr(settings, "registration_manual_file", path)
    app.dependency_overrides.pop(get_registration_client)
    response = client.get("/applications/eligibility")
    assert response.json() == {"status": "ELIGIBLE", "retryable": False}
    write_records(path, "UNKNOWN")
    response = client.get("/applications/eligibility")
    assert response.status_code == 503
    assert response.json() == {"status": "UNAVAILABLE", "retryable": True}
    assert response.headers["cache-control"] == "no-store"
    client.cookies.clear()
    assert client.get("/applications/eligibility").status_code == 401
