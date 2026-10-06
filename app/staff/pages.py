from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select

from app.applications.models import (
    BusinessAudit,
    LocalRoleAssignment,
    NotificationOutbox,
)
from app.applications.pages import DB, Registration, User, integer, render
from app.applications.security import protected_form, require_admin
from app.creators.models import BannedChannel
from app.creators.pages import Store
from app.events.models import Event
from app.moderation import service as moderation
from app.staff import exports, service

router = APIRouter()


def download(data, filename, media_type):
    return Response(
        data,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "private, no-store",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        },
    )


@router.get("/staff")
def staff_home(request: Request, db: DB, user: User):
    roles = list(
        db.scalars(
            select(LocalRoleAssignment).where(LocalRoleAssignment.user_id == user.id)
        )
    )
    administrative = any(r.role == "ADMIN" for r in roles)
    event_ids = [r.event_id for r in roles if r.role == "BADGE_STAFF"]
    if not administrative and not event_ids:
        raise HTTPException(403, "Staff access required")
    events = db.scalars(
        select(Event)
        .where(True if administrative else Event.id.in_(event_ids))
        .order_by(Event.year.desc())
    ).all()
    return render(request, "staff.html", events=events, administrative=administrative)


@router.get("/staff/events/{event_id}/badges")
def lookup_form(event_id: int, request: Request, db: DB, user: User):
    service.require_staff(db, user.id, event_id)
    return render(
        request,
        "pickup.html",
        event=service.event_record(db, event_id),
        rows=[],
        reg_id="",
        administrative=is_admin(db, user.id),
    )


def is_admin(db, actor_id):
    return (
        db.scalar(
            select(LocalRoleAssignment.id).where(
                LocalRoleAssignment.user_id == actor_id,
                LocalRoleAssignment.role == "ADMIN",
                LocalRoleAssignment.event_id.is_(None),
            )
        )
        is not None
    )


@router.post("/staff/events/{event_id}/badges")
async def lookup(event_id: int, request: Request, db: DB, user: User):
    service.require_staff(db, user.id, event_id)
    form = await protected_form(request)
    reg_id = form.get("reg_id", "")
    rows = service.lookup(db, user.id, event_id, reg_id)
    return render(
        request,
        "pickup.html",
        event=service.event_record(db, event_id),
        rows=rows,
        reg_id=reg_id,
        administrative=is_admin(db, user.id),
    )


@router.post("/staff/events/{event_id}/badges/{badge_id}")
async def pickup(event_id: int, badge_id: int, request: Request, db: DB, user: User):
    service.require_staff(db, user.id, event_id)
    form = await protected_form(request)
    action = form.get("action")
    if action not in ("pickup", "undo"):
        raise HTTPException(422, "Invalid pickup action")
    service.pickup(
        db,
        user.id,
        event_id,
        badge_id,
        undo=action == "undo",
        reason=form.get("reason", ""),
    )
    return RedirectResponse(f"/staff/events/{event_id}/badges", status_code=303)


@router.get("/admin/events/{event_id}/operations")
def operations(event_id: int, request: Request, db: DB, user: User):
    require_admin(db, user.id)
    return render(request, "operations.html", event=service.event_record(db, event_id))


@router.post("/admin/events/{event_id}/export")
async def export(
    event_id: int,
    request: Request,
    db: DB,
    user: User,
    registration: Registration,
    store: Store,
):
    require_admin(db, user.id)
    actor_id = user.id
    form = await protected_form(request)
    mode = form.get("mode")
    if mode not in ("print", "operations"):
        raise HTTPException(422, "Choose print or operations export")
    try:
        data = await exports.export(
            db, actor_id, event_id, registration, store, print_ready=mode == "print"
        )
    except HTTPException as error:
        if error.status_code != 409:
            raise
        db.rollback()
        require_admin(db, actor_id)
        return render(
            request,
            "operations.html",
            event=service.event_record(db, event_id),
            error=error.detail,
            status_code=409,
        )
    return download(
        data,
        f"creator-{event_id}-{mode}.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@router.get("/admin/pictures/{application_id}")
def picture(application_id: int, db: DB, user: User, store: Store):
    filename, data = exports.download_picture(db, user.id, application_id, store)
    return download(data, filename, "image/png")


@router.get("/admin/notifications")
def notifications(request: Request, db: DB, user: User):
    require_admin(db, user.id)
    rows = db.scalars(
        select(NotificationOutbox).order_by(NotificationOutbox.id.desc())
    ).all()
    return render(request, "notifications.html", rows=rows)


@router.get("/admin/bans")
def bans(request: Request, db: DB, user: User):
    require_admin(db, user.id)
    rows = db.scalars(
        select(BannedChannel).order_by(
            BannedChannel.platform, BannedChannel.normalized_account
        )
    ).all()
    history = db.scalars(
        select(BusinessAudit)
        .where(BusinessAudit.entity == "banned_channel")
        .order_by(BusinessAudit.id.desc())
    ).all()
    return render(request, "bans.html", rows=rows, history=history)


@router.post("/admin/bans")
async def save_ban(request: Request, db: DB, user: User):
    require_admin(db, user.id)
    form = await protected_form(request)
    ban_id = integer(form, "ban_id") if form.get("ban_id") else None
    if form.get("action") == "delete":
        if ban_id is None:
            raise HTTPException(422, "Ban ID required")
        moderation.delete_ban(db, user.id, ban_id, form.get("reason", ""))
    else:
        moderation.save(
            db,
            user.id,
            ban_id=ban_id,
            platform=form.get("platform", ""),
            account=form.get("account", ""),
            reason=form.get("reason", ""),
            active=form.get("active") == "yes",
        )
    return RedirectResponse("/admin/bans", status_code=303)
