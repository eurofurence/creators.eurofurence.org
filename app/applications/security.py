from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session
from wtforms import Form
from wtforms.csrf.session import SessionCSRF

from app.applications.models import LocalRoleAssignment
from app.config import settings


def require_admin(db: Session, user_id: int, *, lock: bool = False) -> None:
    query = select(LocalRoleAssignment).where(
        LocalRoleAssignment.user_id == user_id,
        LocalRoleAssignment.role == "ADMIN",
        LocalRoleAssignment.event_id.is_(None),
    )
    if lock:
        query = query.with_for_update(read=True)
    if db.scalar(query) is None:
        raise HTTPException(403, "Administrator access required")


class CSRFForm(Form):
    class Meta:
        csrf = True
        csrf_class = SessionCSRF


def csrf_form(request: Request, data=None) -> CSRFForm:
    return CSRFForm(
        data,
        meta={
            "csrf_context": request.session,
            "csrf_secret": settings.session_secret.get_secret_value().encode(),
        },
    )


async def protected_form(request: Request, *, upload=False):
    # Bound the body before form parsing, including chunked requests.
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > (settings.profile_image_max_bytes + 65536 if upload else 65536):
            raise HTTPException(413, "Form is too large")
    request._body = bytes(body)
    data = await request.form(
        max_files=1 if upload else 0, max_fields=300, max_part_size=65536
    )
    if not csrf_form(request, data).validate():
        await data.close()
        raise HTTPException(
            403, "Invalid or expired form. Reload the page and try again."
        )
    return data
