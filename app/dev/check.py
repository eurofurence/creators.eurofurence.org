"""Report local acceptance readiness without disclosing configuration values."""

from pydantic import ValidationError
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from app.auth.configuration import oidc_status, value_status
from app.registration.client import RegistrationUnavailable
from app.registration.manual import load_manual_records

CALLBACK = "http://127.0.0.1:8000/auth/callback"


def diagnose(configuration, *, engine=None):
    rows = []
    ready = True

    def report(label, status, valid=None):
        nonlocal ready
        rows.append(f"{label}: {status}")
        ready &= status == "configured" if valid is None else valid

    local = configuration.environment in ("development", "test")
    report(
        "Environment",
        configuration.environment if local else "not development/test",
        local,
    )
    report("Session secret", value_status(configuration.session_secret, minimum=32))
    for field, status in oidc_status(configuration).items():
        if field == "oidc_redirect_uri":
            valid = (
                status == "configured" and configuration.oidc_redirect_uri == CALLBACK
            )
            report(
                "OIDC callback",
                CALLBACK if valid else "missing/invalid (expected local callback)",
                valid,
            )
        else:
            report(field.upper(), status)
    s3 = all(
        value_status(getattr(configuration, field)) == "configured"
        for field in (
            "s3_endpoint_url",
            "s3_bucket",
            "s3_access_key_id",
            "s3_secret_access_key",
            "s3_region",
        )
    )
    report("S3", "configured" if s3 else "missing/invalid")
    manual = configuration.registration_provider == "manual_test"
    report(
        "Registration provider", configuration.registration_provider, manual and local
    )
    try:
        if not configuration.registration_manual_file:
            raise RegistrationUnavailable
        load_manual_records(configuration.registration_manual_file)
        report("Manual Registration file", "configured")
    except RegistrationUnavailable:
        report("Manual Registration file", "missing/invalid")
    own_engine = engine is None
    try:
        if value_status(configuration.database_url) != "configured":
            raise ValueError
        if own_engine:
            url = configuration.database_url.get_secret_value()
            engine = create_engine(
                url,
                hide_parameters=True,
                connect_args={"connect_timeout": 3}
                if url.startswith("postgresql")
                else {},
            )
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            report("Database", "configured")
            event_id = configuration.active_event_id
            exists = (
                event_id is not None
                and connection.execute(
                    text("SELECT id FROM events WHERE id = :id"), {"id": event_id}
                ).scalar_one_or_none()
                is not None
            )
            report("Active event", str(event_id) if exists else "missing", bool(exists))
    except SQLAlchemyError, ValueError, ImportError:
        # Connection/SQL errors may carry credentials, URLs or row contents.
        report("Database / active event", "unavailable or migrations missing")
    finally:
        if own_engine and engine is not None:
            engine.dispose()
    rows.append("Result: READY" if ready else "Result: NOT READY")
    return rows, ready


def main():
    try:
        from app.config import settings
    except ValidationError:
        print(
            "Configuration: invalid; manual_test requires development/test and a file; check setting types"
        )
        print("Result: NOT READY")
        return 1
    rows, ready = diagnose(settings)
    print("\n".join(rows))
    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
