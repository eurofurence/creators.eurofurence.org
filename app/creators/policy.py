from datetime import UTC, datetime

from fastapi import HTTPException

from app.applications.security import require_admin
from app.applications.workflow import utc


def creator_active(record):
    return record.status == "APPROVED" and record.withdrawn_at is None


def helper_active(helper, creator):
    return (
        creator_active(creator)
        and helper.withdrawn_at is None
        and helper.status == "CONFIRMED"
    )


def authorize(db, actor_id, owner_id, *, administrative=False, reason=""):
    if administrative:
        require_admin(db, actor_id, lock=True)
        if not reason.strip() or len(reason) > 4000:
            raise HTTPException(
                422,
                "An administrative correction requires a reason (maximum 4000 characters).",
            )
    elif actor_id != owner_id:
        raise HTTPException(404, "Record not found")


def change_allowed(event, *, administrative=False, exceptional=False, now=None):
    if event.badge_change_deadline_at is None:
        raise HTTPException(503, "The badge change deadline is not configured.")
    if (now or datetime.now(UTC)) >= utc(event.badge_change_deadline_at) and not (
        administrative and exceptional
    ):
        raise HTTPException(
            409,
            "The badge change deadline has passed. An explicit administrative correction is required.",
        )


def check_version(record, version):
    if record.version != version:
        raise HTTPException(409, "This record changed. Reload and retry.")


def require_active(record):
    if not creator_active(record):
        raise HTTPException(409, "An active approved creator is required.")
