"""Operator-controlled local role management."""

import argparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.applications.models import BusinessAudit, LocalRoleAssignment
from app.database import SessionLocal
from app.events.models import Event  # noqa: F401
from app.helpers import models as helper_models  # noqa: F401
from app.identity.models import ExternalIdentity


def set_admin(db: Session, issuer: str, subject: str, grant: bool, reason: str) -> None:
    if not reason.strip():
        raise ValueError("An operator reason is required")
    identity = db.scalar(
        select(ExternalIdentity)
        .where(ExternalIdentity.issuer == issuer, ExternalIdentity.subject == subject)
        .with_for_update()
    )
    if identity is None:
        raise ValueError("Identity not found; the user must log in first")
    role = db.scalar(
        select(LocalRoleAssignment)
        .where(
            LocalRoleAssignment.user_id == identity.user_id,
            LocalRoleAssignment.role == "ADMIN",
        )
        .with_for_update()
    )
    if grant and role is None:
        db.add(LocalRoleAssignment(user_id=identity.user_id, role="ADMIN"))
    elif not grant and role is not None:
        db.delete(role)
    else:
        return
    db.add(
        BusinessAudit(
            event_id=None,
            actor_id=None,
            entity="local_role",
            entity_id=identity.user_id,
            action="operator_admin_grant" if grant else "operator_admin_revoke",
            reason=reason.strip(),
            changes={"role": "ADMIN"},
        )
    )


def main():
    parser = argparse.ArgumentParser(
        description="Explicit operator ADMIN grant/revocation for a verified local identity"
    )
    parser.add_argument("action", choices=("grant-admin", "revoke-admin"))
    parser.add_argument("--issuer", required=True)
    parser.add_argument("--subject", required=True)
    parser.add_argument("--reason", required=True)
    args = parser.parse_args()
    with SessionLocal.begin() as db:
        set_admin(
            db, args.issuer, args.subject, args.action == "grant-admin", args.reason
        )


if __name__ == "__main__":
    main()
