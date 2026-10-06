"""Private dotenv persistence for the Windows local setup command."""

import argparse
import io
import json
import os
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

from dotenv import dotenv_values
from dotenv.parser import parse_stream

from app.auth.configuration import oidc_status, value_status

ROOT = Path(__file__).resolve().parents[2]


class SetupError(Exception):
    """Messages are fixed operator instructions, never configuration values."""


def require_ignored(root, path):
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise SetupError("Refusing a linked or non-local configuration path")
    relative = path.relative_to(root).as_posix()
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "--", relative],
        cwd=root,
        capture_output=True,
        check=False,
    )
    ignored = subprocess.run(
        ["git", "check-ignore", "--quiet", "--", relative],
        cwd=root,
        capture_output=True,
        check=False,
    )
    if tracked.returncode != 1 or ignored.returncode != 0:
        raise SetupError(
            "Local configuration must be ignored and untracked; fix Git ignore rules first"
        )


def read_local(root):
    path = root / ".env"
    require_ignored(root, path)
    if not path.exists():
        return {}
    content = path.read_text(encoding="utf-8-sig")
    if any(binding.error for binding in parse_stream(io.StringIO(content))):
        raise SetupError("Existing .env syntax is invalid; repair it before setup")
    return dict(dotenv_values(stream=io.StringIO(content), interpolate=False))


def defaults(root):
    return {
        "ENVIRONMENT": "development",
        "DATABASE_URL": "postgresql+psycopg://creators:creators@127.0.0.1:5432/creators",
        "S3_ENDPOINT_URL": "http://127.0.0.1:9090",
        "S3_BUCKET": "creators",
        "S3_ACCESS_KEY_ID": "test",
        "S3_SECRET_ACCESS_KEY": "test",
        "S3_REGION": "us-east-1",
        "OIDC_ISSUER_URL": "https://identity.eurofurence.org/",
        "OIDC_SERVER_METADATA_URL": "https://identity.eurofurence.org/.well-known/openid-configuration",
        "OIDC_REDIRECT_URI": "http://127.0.0.1:8000/auth/callback",
        "REGISTRATION_PROVIDER": "manual_test",
        "REGISTRATION_MANUAL_FILE": str(root / "temp" / "manual-registration.json"),
        "NOTIFICATION_PROVIDER_FACTORY": "",
    }


def proposed_changes(root, existing):
    configuration = SimpleNamespace(
        environment="development",
        **{
            key.lower(): existing.get(key)
            for key in (
                "OIDC_CLIENT_ID",
                "OIDC_CLIENT_SECRET",
                "OIDC_ISSUER_URL",
                "OIDC_SERVER_METADATA_URL",
                "OIDC_REDIRECT_URI",
            )
        },
    )
    oidc = oidc_status(configuration)
    return {
        key: value
        for key, value in defaults(root).items()
        if existing.get(key) != value
        and not (key.startswith("OIDC_") and oidc[key.lower()] == "configured")
    }


def inspect(root):
    existing = read_local(root)
    changes = proposed_changes(root, existing)
    return {
        "need_client_id": value_status(existing.get("OIDC_CLIENT_ID")) != "configured",
        "need_client_secret": value_status(existing.get("OIDC_CLIENT_SECRET"))
        != "configured",
        "confirm_settings": [key for key in changes if existing.get(key)],
    }


def write_values(root, changes):
    path = root / ".env"
    require_ignored(root, path)
    original = path.read_text(encoding="utf-8-sig") if path.exists() else ""
    remaining = dict(changes)
    output = []
    for binding in parse_stream(io.StringIO(original)):
        if binding.error:
            raise SetupError("Existing .env syntax is invalid; no changes made")
        if binding.key not in changes:
            output.append(binding.original.string)
        elif binding.key in remaining:
            output.append(encode(binding.key, remaining.pop(binding.key)))
    if output and not output[-1].endswith("\n"):
        output.append("\n")
    output.extend(encode(key, value) for key, value in remaining.items())
    descriptor, filename = tempfile.mkstemp(prefix=".env.", dir=root)
    candidate = Path(filename)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            require_ignored(root, candidate)
            stream.write("".join(output))
        os.replace(candidate, path)
    finally:
        candidate.unlink(missing_ok=True)


def encode(key, value):
    if any(character in value for character in ("\r", "\n", "\x00", "${")):
        raise SetupError("New settings must be single-line literal dotenv values")
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"{key}='{escaped}'\n"


def configure(root, supplied):
    existing = read_local(root)
    plan = inspect(root)
    if plan["confirm_settings"] and supplied.get("confirm_local") is not True:
        raise SetupError("Local settings replacement requires explicit confirmation")
    changes = proposed_changes(root, existing)
    for key in ("OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET"):
        if value_status(existing.get(key)) != "configured":
            value = supplied.get(key)
            if value_status(value) != "configured":
                raise SetupError(
                    "A non-placeholder development OIDC client ID and secret are required"
                )
            changes[key] = value
    if value_status(existing.get("SESSION_SECRET"), minimum=32) != "configured":
        changes["SESSION_SECRET"] = secrets.token_urlsafe(48)
    manual_path = root / "temp" / "manual-registration.json"
    require_ignored(root, manual_path)
    manual_path.parent.mkdir(exist_ok=True)
    if not manual_path.exists():
        manual_path.write_text("[]\n", encoding="utf-8")
    write_values(root, changes)


def require_local_effective_settings():
    from app.config import settings

    # Refuse to migrate another database because a transient shell overrides .env.
    if (
        settings.environment != "development"
        or not settings.database_url
        or settings.database_url.get_secret_value() != defaults(ROOT)["DATABASE_URL"]
        or settings.notification_provider_factory
    ):
        raise SetupError(
            "Setup requires the local development DB and no notification provider; remove conflicting environment overrides"
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("inspect", "configure", "verify-local"))
    args = parser.parse_args()
    try:
        if args.action == "inspect":
            print(json.dumps(inspect(ROOT)))
        elif args.action == "configure":
            configure(ROOT, json.load(sys.stdin))
            print("Local configuration saved.")
        else:
            require_local_effective_settings()
        return 0
    except SetupError as error:
        print(str(error), file=sys.stderr)
    except Exception:  # noqa: BLE001 - Never print input/credential exception payloads.
        print(
            "Local configuration could not be processed; check configuration syntax and permissions.",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
