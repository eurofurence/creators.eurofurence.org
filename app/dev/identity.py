"""Read one OIDC-verified local identity; never grants roles."""

import argparse
import json

from sqlalchemy import text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--user-id",
        type=int,
        required=True,
        help="ID shown by /auth/me after real OIDC login",
    )
    args = parser.parse_args()
    try:
        from app.config import settings

        if settings.environment not in ("development", "test"):
            print("Identity diagnostics require development/test")
            return 1
        from app.database import SessionLocal

        with SessionLocal() as db:
            rows = (
                db.execute(
                    text(
                        "SELECT user_id, issuer, subject FROM external_identities WHERE user_id = :id"
                    ),
                    {"id": args.user_id},
                )
                .mappings()
                .all()
            )
        if len(rows) != 1:
            print(
                "Expected exactly one verified identity for that local user; complete real OIDC login first"
            )
            return 1
        print(json.dumps(dict(rows[0])))
        return 0
    except Exception:  # noqa: BLE001 - CLI boundary must not dump settings/SQL values.
        print(
            "Identity diagnostics unavailable; check local configuration and database"
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
