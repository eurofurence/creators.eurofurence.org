import hashlib
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select

from app.applications.pages import DB, User, integer, templates
from app.applications.security import protected_form, require_admin
from app.creators.images import ImageStorageUnavailable, normalize_png
from app.creators.pages import Store
from app.events.models import Event
from app.gallery.service import PublicCreator, public_records, set_visibility

router = APIRouter()


def cache_headers(request, content):
    # Revalidate at the origin so hiding/deletion wins over browser/CDN stale data.
    # Server-side consumers can use the documented five-minute polling interval.
    return {
        "ETag": '"' + hashlib.sha256(content).hexdigest() + '"',
        "Cache-Control": "private, no-cache, must-revalidate"
        if request.cookies
        else "public, no-cache, must-revalidate",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    }


def conditional(request, content, media_type):
    headers = cache_headers(request, content)
    tags = [
        tag.strip().removeprefix("W/")
        for tag in request.headers.get("if-none-match", "").split(",")
    ]
    if headers["ETag"] in tags or "*" in tags:
        return Response(status_code=304, headers=headers)
    return Response(content, media_type=media_type, headers=headers)


@router.get("/api/v1/events/{year}/creators", response_model=list[PublicCreator])
def creators(year: int, request: Request, db: DB):
    payload = [row.model_dump() for row, _ in public_records(db, year)]
    content = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    return conditional(request, content, "application/json")


@router.get("/api/v1/events/{year}/creators/{public_id}/image")
def picture(year: int, public_id: str, request: Request, db: DB, store: Store):
    rows = public_records(db, year, public_id)
    if not rows:
        raise HTTPException(
            404, "Picture not found", headers={"Cache-Control": "no-store"}
        )
    key = rows[0][1]
    db.rollback()
    try:
        data = normalize_png(store.get(key))
    except ImageStorageUnavailable, HTTPException:
        raise HTTPException(
            503,
            "Picture temporarily unavailable",
            headers={"Cache-Control": "no-store"},
        ) from None
    # Visibility or retention may have changed during storage I/O.
    current = public_records(db, year, public_id)
    if not current or current[0][1] != key:
        raise HTTPException(
            404, "Picture not found", headers={"Cache-Control": "no-store"}
        )
    return conditional(request, data, "image/png")


@router.get("/")
def home(request: Request, db: DB):
    from datetime import UTC, datetime

    years = list(
        db.scalars(
            select(Event.year)
            .where(
                Event.data_delete_at > datetime.now(UTC),
                Event.cleanup_started_at.is_(None),
            )
            .order_by(Event.year.desc())
        )
    )
    content = templates.get_template("gallery_index.html").render(years=years).encode()
    return conditional(request, content, "text/html")


@router.get("/gallery/{year}")
def gallery(year: int, request: Request, db: DB):
    content = (
        templates.get_template("gallery.html")
        .render(year=year, creators=[row for row, _ in public_records(db, year)])
        .encode()
    )
    return conditional(request, content, "text/html")


@router.post("/admin/creators/{application_id}/visibility")
async def visibility(application_id: int, request: Request, db: DB, user: User):
    require_admin(db, user.id)
    form = await protected_form(request)
    if form.get("hidden") not in ("yes", "no"):
        raise HTTPException(422, "Choose hide or restore")
    set_visibility(
        db,
        user.id,
        application_id,
        form["hidden"] == "yes",
        channel_id=integer(form, "channel_id") if form.get("channel_id") else None,
    )
    return RedirectResponse(
        f"/creators/{application_id}?administrative=true", status_code=303
    )
