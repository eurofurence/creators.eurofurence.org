"""Operator-controlled local role management."""

import argparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.applications.models import BusinessAudit, LocalRoleAssignment
from app.database import SessionLocal
from app.events.models import Event
from app.helpers import models as helper_models  # noqa: F401
from app.identity.models import ExternalIdentity


def set_role(
    db: Session, issuer: str, subject: str, grant: bool, reason: str, *, event_id=None
) -> None:
    if not reason.strip():
        raise ValueError("An operator reason is required")
    identity = db.scalar(
        select(ExternalIdentity)
        .where(ExternalIdentity.issuer == issuer, ExternalIdentity.subject == subject)
        .with_for_update()
    )
    if identity is None:
        raise ValueError("Identity not found; the user must log in first")
    role_name = "ADMIN" if event_id is None else "BADGE_STAFF"
    if event_id is not None and db.get(Event, event_id) is None:
        raise ValueError("Event not found")
    role = db.scalar(
        select(LocalRoleAssignment)
        .where(
            LocalRoleAssignment.user_id == identity.user_id,
            LocalRoleAssignment.role == role_name,
            LocalRoleAssignment.event_id == event_id,
        )
        .with_for_update()
    )
    if grant and role is None:
        db.add(
            LocalRoleAssignment(
                user_id=identity.user_id, role=role_name, event_id=event_id
            )
        )
    elif not grant and role is not None:
        db.delete(role)
    else:
        return
    db.add(
        BusinessAudit(
            event_id=event_id,
            actor_id=None,
            entity="local_role",
            entity_id=identity.user_id,
            action=f"operator_{role_name.lower()}_" + ("grant" if grant else "revoke"),
            reason=reason.strip(),
            changes={"role": role_name},
        )
    )


def set_admin(db: Session, issuer: str, subject: str, grant: bool, reason: str) -> None:
    set_role(db, issuer, subject, grant, reason)


def main():
    parser = argparse.ArgumentParser(
        description="Explicit operator ADMIN grant/revocation for a verified local identity"
    )
    parser.add_argument(
        "action",
        choices=(
            "grant-admin",
            "revoke-admin",
            "grant-badge-staff",
            "revoke-badge-staff",
        ),
    )
    parser.add_argument("--event-id", type=int)
    parser.add_argument("--issuer", required=True)
    parser.add_argument("--subject", required=True)
    parser.add_argument("--reason", required=True)
    args = parser.parse_args()
    if ("badge-staff" in args.action) != (args.event_id is not None):
        parser.error("--event-id is required only for BADGE_STAFF grants/revocations")
    with SessionLocal.begin() as db:
        set_role(
            db,
            args.issuer,
            args.subject,
            args.action.startswith("grant-"),
            args.reason,
            event_id=args.event_id,
        )


if __name__ == "__main__":
    main()
