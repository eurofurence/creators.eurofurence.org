from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from starlette.datastructures import UploadFile

from app.applications.input import normalize_channel
from app.applications.models import Badge, CreatorApplication, CreatorChannel
from app.applications.pages import DB, Identity, Registration, User, integer, render
from app.applications.security import protected_form, require_admin
from app.applications.workflow import active_event, get_application, utc
from app.auth.dependencies import get_current_user
from app.creators import images, service
from app.creators.models import CreatorProfile, ProfileImage
from app.creators.policy import creator_active, helper_active
from app.helpers import service as helpers
from app.helpers.models import HelperInvitation, HelperRegistration

router = APIRouter()
Store = Annotated[images.ImageStore, Depends(images.get_image_store)]


def policy(form):
    return {
        "administrative": form.get("administrative") == "yes",
        "exceptional": form.get("exceptional") == "yes",
        "reason": form.get("reason", ""),
    }


def return_to_creator(application_id, administrative=False):
    return RedirectResponse(
        f"/creators/{application_id}"
        + ("?administrative=true" if administrative else ""),
        status_code=303,
    )


def creator_view(db, actor_id, application_id, administrative):
    event = active_event(db)
    application = get_application(db, application_id, event.id)
    if administrative:
        require_admin(db, actor_id)
    elif application.user_id != actor_id:
        raise HTTPException(404, "Creator not found")
    rows = db.scalars(
        select(HelperRegistration)
        .where(HelperRegistration.application_id == application.id)
        .order_by(HelperRegistration.id)
    ).all()
    # Explicit creator-safe projection: no email, Reg-ID or other identity details.
    helper_rows = [
        {
            "id": h.id,
            "nickname": h.nickname,
            "status": h.status,
            "version": h.version,
            "withdrawn": h.withdrawn_at is not None,
            "active": helper_active(h, application),
        }
        for h in rows
    ]
    invitations = db.scalars(
        select(HelperInvitation)
        .where(HelperInvitation.application_id == application.id)
        .order_by(HelperInvitation.id)
    ).all()
    now = datetime.now(UTC)
    return {
        "record": application,
        "event": event,
        "administrative": administrative,
        "profile": db.get(CreatorProfile, application.id),
        "channels": list(
            db.scalars(
                select(CreatorChannel)
                .where(CreatorChannel.application_id == application.id)
                .order_by(CreatorChannel.id)
            )
        ),
        "helpers": helper_rows,
        "invitations": [
            {
                "id": i.id,
                "expires_at": i.expires_at,
                "used": bool(i.consumed_at),
                "state": "Used"
                if i.consumed_at
                else "Revoked"
                if i.revoked_at
                else "Expired"
                if now >= utc(i.expires_at)
                else "Available",
            }
            for i in invitations
        ],
        "active": creator_active(application),
        "editable": creator_active(application)
        and event.badge_change_deadline_at is not None
        and now < utc(event.badge_change_deadline_at),
        "banned": service.banned_matches(db, application.id) if administrative else [],
    }


@router.get("/creators/{application_id}")
def creator_page(
    application_id: int,
    request: Request,
    db: DB,
    user: User,
    administrative: bool = False,
):
    return render(
        request,
        "creator.html",
        **creator_view(db, user.id, application_id, administrative),
    )


@router.post("/creators/{application_id}/profile")
async def save_profile(application_id: int, request: Request, db: DB, user: User):
    form = await protected_form(request)
    try:
        platforms, accounts = form.getlist("platform"), form.getlist("account")
        if len(platforms) != len(accounts):
            raise ValueError("Every channel requires a platform and account.")
        primary = integer(form, "primary")
        channels = tuple(
            normalize_channel(p, a, index == primary)
            for index, (p, a) in enumerate(zip(platforms, accounts))
        )
        service.save_profile(
            db,
            user.id,
            application_id,
            integer(form, "version"),
            form.get("channel_name", ""),
            channels,
            **policy(form),
        )
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    return return_to_creator(application_id, policy(form)["administrative"])


@router.post("/creators/{application_id}/picture")
async def picture(
    application_id: int, request: Request, db: DB, user: User, store: Store
):
    form = await protected_form(request, upload=True)
    try:
        file = form.get("picture")
        if not isinstance(file, UploadFile):
            raise HTTPException(422, "Select a PNG picture.")
        from app.config import settings

        data = await file.read(settings.profile_image_max_bytes + 1)
        images.replace_picture(
            db,
            user.id,
            application_id,
            integer(form, "version"),
            data,
            store,
            **policy(form),
        )
    finally:
        await form.close()
    return return_to_creator(application_id, policy(form)["administrative"])


@router.get("/creators/{application_id}/picture")
def view_picture(application_id: int, db: DB, user: User, store: Store):
    event = active_event(db)
    application = get_application(db, application_id, event.id)
    if application.user_id != user.id:
        require_admin(db, user.id)
    profile = db.get(CreatorProfile, application_id)
    image = (
        db.get(ProfileImage, profile.image_id) if profile and profile.image_id else None
    )
    if (
        image is None
        or image.state != "ACTIVE"
        or datetime.now(UTC) >= utc(event.data_delete_at)
    ):
        raise HTTPException(404, "Picture not found")
    key = image.object_key
    db.rollback()
    try:
        data = store.get(key)
    except images.ImageStorageUnavailable:
        raise HTTPException(
            503, "Image storage is unavailable. Please retry."
        ) from None
    return Response(
        data,
        media_type="image/png",
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        },
    )


@router.post("/creators/{application_id}/invitations")
async def invitation(
    application_id: int,
    request: Request,
    db: DB,
    user: User,
    registration: Registration,
    identity: Identity,
):
    form = await protected_form(request)
    actor_id = user.id
    _, secret = await helpers.invite(
        db,
        actor_id,
        application_id,
        registration,
        identity,
        replace_id=integer(form, "replace_id") if form.get("replace_id") else None,
        **policy(form),
    )
    # The URL fragment is never sent to this server by a normal browser.
    return render(
        request,
        "invitation_created.html",
        invitation_path="/helpers/redeem#" + secret,
        application_id=application_id,
    )


@router.post("/invitations/{invitation_id}/revoke")
async def revoke(invitation_id: int, request: Request, db: DB, user: User):
    await protected_form(request)
    helpers.revoke(db, user.id, invitation_id)
    return RedirectResponse("/applications", status_code=303)


@router.get("/helpers/redeem")
def redeem_page(request: Request, db: DB):
    try:
        get_current_user(request, db)
    except HTTPException as error:
        if error.status_code != 401:
            raise
        return render(request, "invitation_login.html")
    return render(request, "redeem.html")


@router.post("/helpers/redeem")
async def redeem(
    request: Request, db: DB, user: User, registration: Registration, identity: Identity
):
    form = await protected_form(request)
    await helpers.redeem(
        db, user.id, form.get("invitation", ""), registration, identity
    )
    return RedirectResponse("/helpers", status_code=303)


@router.get("/helpers")
def helper_dashboard(request: Request, db: DB, user: User):
    event = active_event(db)
    relationships = []
    for helper, application in db.execute(
        select(HelperRegistration, CreatorApplication)
        .join(
            CreatorApplication,
            HelperRegistration.application_id == CreatorApplication.id,
        )
        .where(
            HelperRegistration.user_id == user.id,
            HelperRegistration.event_id == event.id,
        )
    ):
        profile = db.get(CreatorProfile, application.id)
        badge = db.scalar(select(Badge).where(Badge.helper_id == helper.id))
        relationships.append(
            {
                "id": helper.id,
                "version": helper.version,
                "creator_name": profile.channel_name
                if profile and profile.channel_name
                else "Creator",
                "status": helper.status,
                "active": helper_active(helper, application),
                "withdrawn": helper.withdrawn_at is not None,
                "badge_number": badge.badge_number if badge else None,
            }
        )
    return render(
        request,
        "helpers.html",
        relationships=relationships,
        editable=event.badge_change_deadline_at is not None
        and datetime.now(UTC) < utc(event.badge_change_deadline_at),
    )


@router.post("/helpers/{helper_id}/decision")
async def helper_decision(
    helper_id: int,
    request: Request,
    db: DB,
    user: User,
    registration: Registration,
    identity: Identity,
):
    form = await protected_form(request)
    actor_id = user.id
    await helpers.decide(
        db,
        actor_id,
        helper_id,
        integer(form, "version"),
        form.get("status", ""),
        registration,
        identity,
        **policy(form),
    )
    return RedirectResponse("/applications", status_code=303)


@router.post("/participation/{kind}/{entity_id}")
async def participation(
    kind: str,
    entity_id: int,
    request: Request,
    db: DB,
    user: User,
    registration: Registration,
    identity: Identity,
):
    if kind not in ("creator", "helper"):
        raise HTTPException(404, "Participation not found")
    form = await protected_form(request)
    action = form.get("action")
    if action not in ("withdraw", "restore"):
        raise HTTPException(422, "Invalid action")
    await helpers.withdraw(
        db,
        user.id,
        entity_id,
        integer(form, "version"),
        registration,
        identity,
        helper=kind == "helper",
        restore=action == "restore",
        **policy(form),
    )
    return RedirectResponse(
        "/helpers" if kind == "helper" else "/applications", status_code=303
    )
