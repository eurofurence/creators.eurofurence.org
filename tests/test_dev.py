import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import test_auth as auth_tests
from dotenv import dotenv_values
from pydantic import SecretStr
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.config import Settings, settings
from app.database import Base
from app.dev.check import diagnose
from app.dev.event import (
    LOCAL_NAME,
    choose_existing_event,
    ensure_event,
    select_existing_event,
)
from app.dev.setup import SetupError, configure, inspect, read_local, write_values
from app.events.models import Event

client = auth_tests.client
db = auth_tests.db
provider = auth_tests.provider
key = auth_tests.key
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def local_repository(tmp_path):
    subprocess.run(
        ["git", "init", "--quiet", str(tmp_path)], check=True, capture_output=True
    )
    (tmp_path / ".gitignore").write_text(".env*\n" + "temp" + "/\n", encoding="utf-8")
    return tmp_path


def credentials():
    return {
        "OIDC_CLIENT_ID": "isolated-client",
        "OIDC_CLIENT_SECRET": "isolated-secret-'\\-#-value",
    }


def test_persistent_setup_preserves_credentials_session_and_manual_data(
    local_repository,
):
    root = local_repository
    assert inspect(root)["need_client_secret"]
    configure(root, credentials())
    first = read_local(root)
    assert first["OIDC_CLIENT_SECRET"] == credentials()["OIDC_CLIENT_SECRET"]
    assert len(first["SESSION_SECRET"]) >= 32
    assert first["REGISTRATION_PROVIDER"] == "manual_test"
    manual = Path(first["REGISTRATION_MANUAL_FILE"])
    assert json.loads(manual.read_text()) == []
    manual.write_text('[{"preserve": "existing private data"}]', encoding="utf-8")
    configure(root, {})
    assert read_local(root) == first
    assert json.loads(manual.read_text()) == [{"preserve": "existing private data"}]
    assert not inspect(root)["need_client_id"]
    assert not inspect(root)["need_client_secret"]
    assert (
        dotenv_values(root / ".env")["OIDC_CLIENT_SECRET"]
        == credentials()["OIDC_CLIENT_SECRET"]
    )


def test_existing_oidc_settings_are_never_replaced(local_repository):
    root = local_repository
    write_values(
        root,
        {
            **credentials(),
            "SESSION_SECRET": "stable-session-value-" * 3,
            "OIDC_REDIRECT_URI": "http://localhost:8000/auth/callback",
            "OIDC_ISSUER_URL": "https://other-development.example/",
        },
    )
    before = read_local(root)
    configure(root, {"OIDC_CLIENT_SECRET": "should-not-replace"})
    after = read_local(root)
    for field, value in before.items():
        assert after[field] == value


def test_conflicting_defaults_require_confirmation_and_placeholders_are_replaced(
    local_repository,
):
    root = local_repository
    write_values(
        root,
        {
            "ENVIRONMENT": "production",
            "SESSION_SECRET": "INSERT_SECRET",
            "OIDC_CLIENT_SECRET": "INSERT_SECRET",
        },
    )
    before = (root / ".env").read_bytes()
    with pytest.raises(SetupError, match="confirmation"):
        configure(root, credentials())
    assert (root / ".env").read_bytes() == before
    configure(root, {**credentials(), "confirm_local": True})
    assert read_local(root)["ENVIRONMENT"] == "development"
    assert not read_local(root)["SESSION_SECRET"].startswith("INSERT_")


@pytest.mark.parametrize("ignore", ["", ".env*\n"])
def test_setup_refuses_unignored_secret_or_manual_file(local_repository, ignore):
    (local_repository / ".gitignore").write_text(ignore)
    with pytest.raises(SetupError, match="ignored and untracked"):
        configure(local_repository, credentials())
    assert not (local_repository / ".env").exists()


def test_setup_refuses_tracked_env(local_repository):
    path = local_repository / ".env"
    path.write_text("UNRELATED=example\n")
    subprocess.run(
        ["git", "add", "-f", ".env"],
        cwd=local_repository,
        check=True,
        capture_output=True,
    )
    with pytest.raises(SetupError, match="ignored and untracked"):
        configure(local_repository, credentials())
    assert path.read_text() == "UNRELATED=example\n"


@pytest.fixture
def diagnostic_config(tmp_path):
    manual = tmp_path / "records.json"
    manual.write_text("[]")
    return settings.model_copy(
        update={
            "environment": "development",
            "registration_provider": "manual_test",
            "registration_manual_file": manual,
            "active_event_id": 45,
            "session_secret": SecretStr("private-session-value-that-must-not-appear"),
            "oidc_client_id": "private-client-id",
            "oidc_client_secret": SecretStr("private-client-secret"),
            "s3_endpoint_url": "http://127.0.0.1:9090",
            "s3_bucket": "creators",
            "s3_access_key_id": "private-s3-id",
            "s3_secret_access_key": SecretStr("private-s3-secret"),
            "database_url": SecretStr("sqlite://"),
        }
    )


@pytest.fixture
def diagnostic_engine():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE events (id INTEGER PRIMARY KEY)"))
        connection.execute(text("INSERT INTO events (id) VALUES (45)"))
    yield engine
    engine.dispose()


def test_diagnostics_ready_and_value_free(diagnostic_config, diagnostic_engine):
    rows, ready = diagnose(diagnostic_config, engine=diagnostic_engine)
    assert ready
    output = "\n".join(rows)
    assert "Result: READY" in output
    assert "Active event: 45" in output
    for field in (
        "session_secret",
        "oidc_client_secret",
        "s3_secret_access_key",
        "oidc_client_id",
        "s3_access_key_id",
    ):
        value = getattr(diagnostic_config, field)
        value = (
            value.get_secret_value() if hasattr(value, "get_secret_value") else value
        )
        assert value not in output


@pytest.mark.parametrize(
    "field,value,expected",
    [
        ("oidc_client_id", None, "OIDC_CLIENT_ID: missing"),
        ("oidc_client_secret", None, "OIDC_CLIENT_SECRET: missing"),
        ("oidc_client_id", "INSERT_CLIENT_ID", "OIDC_CLIENT_ID: placeholder/invalid"),
        (
            "oidc_client_secret",
            SecretStr("INSERT_SECRET"),
            "OIDC_CLIENT_SECRET: placeholder/invalid",
        ),
        ("session_secret", SecretStr("short"), "Session secret: placeholder/invalid"),
        (
            "oidc_redirect_uri",
            "http://localhost:8000/auth/callback",
            "OIDC callback: missing/invalid",
        ),
        ("s3_bucket", None, "S3: missing/invalid"),
        ("active_event_id", None, "Active event: missing"),
        ("active_event_id", 46, "Active event: missing"),
    ],
)
def test_diagnostics_report_failures(
    diagnostic_config, diagnostic_engine, field, value, expected
):
    setattr(diagnostic_config, field, value)
    rows, ready = diagnose(diagnostic_config, engine=diagnostic_engine)
    assert not ready
    assert expected in "\n".join(rows)


@pytest.mark.parametrize("content", [None, "invalid", '[{"bad":"data"}]'])
def test_diagnostics_invalid_manual_file(diagnostic_config, diagnostic_engine, content):
    if content is None:
        diagnostic_config.registration_manual_file.unlink()
    else:
        diagnostic_config.registration_manual_file.write_text(content)
    rows, ready = diagnose(diagnostic_config, engine=diagnostic_engine)
    assert not ready
    assert "Manual Registration file: missing/invalid" in rows


def test_diagnostics_database_unavailable(diagnostic_config, diagnostic_engine):
    diagnostic_config.database_url = None
    rows, ready = diagnose(diagnostic_config, engine=diagnostic_engine)
    assert not ready
    assert "Database / active event: unavailable or migrations missing" in rows


def clean_environment():
    fields = {field.upper() for field in Settings.model_fields}
    return {
        key: value for key, value in os.environ.items() if key.upper() not in fields
    }


@pytest.mark.parametrize("scenario", ["ready", "missing_secret", "production"])
def test_diagnostics_command_scenarios(local_repository, scenario):
    root = local_repository
    configure(root, credentials())
    path = root / "test.sqlite"
    engine = create_engine("sqlite:///" + path.as_posix())
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE events (id INTEGER PRIMARY KEY)"))
        connection.execute(text("INSERT INTO events VALUES (45)"))
    engine.dispose()
    write_values(
        root, {"DATABASE_URL": "sqlite:///" + path.as_posix(), "ACTIVE_EVENT_ID": "45"}
    )
    if scenario == "missing_secret":
        write_values(root, {"OIDC_CLIENT_SECRET": ""})
    if scenario == "production":
        write_values(root, {"ENVIRONMENT": "production"})
    result = subprocess.run(
        [sys.executable, "-m", "app.dev.check"],
        cwd=root,
        env={**clean_environment(), "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == (0 if scenario == "ready" else 1)
    assert "Traceback" not in result.stdout + result.stderr
    assert credentials()["OIDC_CLIENT_SECRET"] not in result.stdout + result.stderr
    assert read_local(root)["SESSION_SECRET"] not in result.stdout + result.stderr
    if scenario == "missing_secret":
        assert "OIDC_CLIENT_SECRET: missing" in result.stdout


def test_environment_still_overrides_dotenv(local_repository, monkeypatch):
    configure(local_repository, credentials())
    monkeypatch.setenv("OIDC_CLIENT_ID", "environment-client")
    loaded = Settings(_env_file=local_repository / ".env")
    assert loaded.oidc_client_id == "environment-client"
    assert read_local(local_repository)["OIDC_CLIENT_ID"] == "isolated-client"


@pytest.mark.parametrize("environment", ["development", "production"])
def test_identity_command_selects_only_requested_identity(
    local_repository, environment
):
    configure(local_repository, credentials())
    path = local_repository / "identities.sqlite"
    url = "sqlite:///" + path.as_posix()
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE external_identities (user_id INTEGER, issuer TEXT, subject TEXT)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO external_identities VALUES (45, 'https://identity.example/', 'requested-person'), (46, 'https://identity.example/', 'unrelated-person')"
            )
        )
    engine.dispose()
    write_values(
        local_repository,
        {
            "DATABASE_URL": url,
            "ENVIRONMENT": environment,
            "REGISTRATION_PROVIDER": "unavailable",
        },
    )
    result = subprocess.run(
        [sys.executable, "-m", "app.dev.identity", "--user-id", "45"],
        cwd=local_repository,
        env={**clean_environment(), "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert "unrelated-person" not in result.stdout + result.stderr
    if environment == "development":
        assert result.returncode == 0
        assert json.loads(result.stdout) == {
            "user_id": 45,
            "issuer": "https://identity.example/",
            "subject": "requested-person",
        }
    else:
        assert result.returncode == 1
        assert "development/test" in result.stdout
        assert "requested-person" not in result.stdout


@pytest.mark.parametrize(
    "value,status",
    [(None, "missing"), (SecretStr("INSERT_PRIVATE_VALUE"), "placeholder/invalid")],
)
def test_browser_config_error_is_generic_and_logging_safe(
    client, monkeypatch, caplog, value, status
):
    monkeypatch.setattr(settings, "oidc_client_secret", value)
    response = client.get("/auth/login")
    assert response.status_code == 503
    assert response.json() == {"detail": "OIDC client is not configured"}
    assert f"OIDC_CLIENT_SECRET is {status}" in caplog.text
    assert "INSERT_PRIVATE_VALUE" not in caplog.text + response.text
    assert settings.session_secret.get_secret_value() not in caplog.text + response.text


def test_provider_and_authentication_log_categories(client, provider, caplog):
    provider.failure = "/.well-known/openid-configuration"
    assert client.get("/auth/login").status_code == 503
    assert "Identity provider unavailable" in caplog.text
    assert "sensitive upstream detail" not in caplog.text
    provider.failure = None
    caplog.clear()
    assert (
        client.get("/auth/callback?state=private-state&code=private-code").status_code
        == 401
    )
    assert "OIDC authentication failed" in caplog.text
    for secret in ("private-state", "private-code", "test-client-secret"):
        assert secret not in caplog.text


def test_local_event_creation_refresh_and_nonlocal_protection():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    configuration = settings.model_copy(update={"active_event_id": None})
    with Session(engine) as db:
        event_id = ensure_event(db, configuration)
        db.commit()
        event = db.get(Event, event_id)
        assert event.name == LOCAL_NAME
        assert event.helper_limit is None
        assert (event.data_delete_at - event.ends_at).days == 30
        configuration.active_event_id = event_id
        original = event.application_open_at
        assert ensure_event(db, configuration) == event_id
        assert event.application_open_at == original
        assert ensure_event(db, configuration, refresh=True) == event_id
        event.name = "Real event"
        db.commit()
        with pytest.raises(SetupError, match="non-local"):
            ensure_event(db, configuration, refresh=True)
        configuration.environment = "production"
        with pytest.raises(SetupError, match="development/test"):
            ensure_event(db, configuration)
    engine.dispose()


def test_existing_event_selection_preserves_legacy_data(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    configuration = settings.model_copy(update={"active_event_id": None})
    with Session(engine) as db:
        event_id = ensure_event(db, configuration)
        event = db.get(Event, event_id)
        event.name = "Local browser acceptance"
        db.commit()
        before = {
            column.name: getattr(event, column.name)
            for column in Event.__table__.columns
        }
        with pytest.raises(SetupError, match="non-local Event"):
            ensure_event(db, configuration)
        monkeypatch.setattr("builtins.input", lambda _: str(event_id))
        assert choose_existing_event(db, configuration) == event_id
        assert select_existing_event(db, configuration, event_id) == event_id
        assert not db.dirty
        assert before == {
            column.name: getattr(event, column.name)
            for column in Event.__table__.columns
        }
        with pytest.raises(SetupError, match="does not exist"):
            select_existing_event(db, configuration, event_id + 1)
        monkeypatch.setattr("builtins.input", lambda _: "")
        with pytest.raises(SetupError, match="cancelled"):
            choose_existing_event(db, configuration)
        configuration.environment = "production"
        with pytest.raises(SetupError, match="development/test"):
            select_existing_event(db, configuration, event_id)
    engine.dispose()


def test_ignored_paths_and_docker_allowlist():
    for relative in (".env", str(Path("temp") / "manual-registration.json")):
        result = subprocess.run(
            ["git", "check-ignore", "--quiet", relative], cwd=ROOT, check=False
        )
        assert result.returncode == 0
    lines = (ROOT / ".dockerignore").read_text().splitlines()
    assert lines[0] == "*"
    assert {line for line in lines if line.startswith("!")} == {
        "!requirements.txt",
        "!app/",
        "!app/**",
        "!migrations/",
        "!migrations/**",
        "!alembic.ini",
    }


@pytest.mark.skipif(
    sys.platform != "win32", reason="Windows PowerShell developer tooling"
)
def test_powershell_setup_safe_preflight_in_isolated_directory(tmp_path):
    script = tmp_path / "scripts" / "dev-setup.ps1"
    script.parent.mkdir()
    shutil.copyfile(ROOT / "scripts" / "dev-setup.ps1", script)
    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "Create the virtualenv:" in result.stdout
    assert not (tmp_path / ".env").exists()
