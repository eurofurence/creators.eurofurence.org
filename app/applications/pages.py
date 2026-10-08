from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData, UploadFile

from app.applications import workflow
from app.applications.input import (
    CONTENT_TYPES,
    PLATFORM_LABELS,
    PLATFORMS,
    parse_application,
    video_values,
)
from app.applications.models import (
    Badge,
    ConventionVideo,
    CreatorApplication,
    CreatorChannel,
)
from app.applications.security import csrf_form, protected_form, require_admin
from app.auth.dependencies import get_current_user
from app.config import settings
from app.creators import images
from app.creators.models import CreatorProfile, ProfileImage
from app.database import get_db
from app.identity.models import LocalUser
from app.identity.profile import IdentityProfileClient, get_identity_profile_client
from app.registration.client import RegistrationClient, get_registration_client

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
static = StaticFiles(directory=Path(__file__).parent / "static")
DB = Annotated[Session, Depends(get_db)]
User = Annotated[LocalUser, Depends(get_current_user)]
Registration = Annotated[RegistrationClient, Depends(get_registration_client)]
Identity = Annotated[IdentityProfileClient, Depends(get_identity_profile_client)]
Store = Annotated[images.ImageStore, Depends(images.get_image_store)]


def form_values(context):
    record, profile = context.get("record"), context.get("profile")
    values = []
    if record:
        values += [("application_id", str(record.id)), ("version", str(record.version))]
        values += [
            ("content_type", kind)
            for kind in CONTENT_TYPES
            if getattr(record, kind.lower())
        ]
    values.append(("channel_name", profile.channel_name if profile else ""))
    for index, channel in enumerate(context.get("channels", [])):
        values += [
            ("platform", channel.platform),
            ("account", channel.original_representation),
        ]
        if channel.is_primary:
            values.append(("primary", str(index)))
    values += [("video_url", v.url) for v in context.get("videos", [])]
    submitted = context.get("submitted")
    if submitted is not None:
        overridden = set(submitted)
        if "content_types_present" in submitted:
            overridden.add("content_type")
        if "channels_present" in submitted:
            overridden.update(("platform", "account", "primary"))
        video_edit = any(
            key in submitted for key in ("videos", "video_url", "videos_present")
        )
        if video_edit:
            overridden.add("video_url")
        values = [(key, value) for key, value in values if key not in overridden]
        values += [
            (key, value)
            for key, value in submitted.multi_items()
            if isinstance(value, str) and key not in ("videos", "video_url")
        ]
        if video_edit:
            values += [("video_url", value) for value in video_values(submitted)]
    return FormData(values)


def render(request, template, *, status_code=200, **context):
    return templates.TemplateResponse(
        request=request,
        name=template,
        status_code=status_code,
        context={
            "csrf": csrf_form(request),
            "signed_in": getattr(request.state, "user", None) is not None,
            "is_admin": getattr(request.state, "is_admin", False),
            "is_staff": getattr(request.state, "is_staff", False),
            "platforms": PLATFORMS,
            "platform_labels": PLATFORM_LABELS,
            "content_types": CONTENT_TYPES,
            "content_labels": {
                "LIVESTREAM": "Livestreams",
                "SHORTS": "Short videos",
                "VLOGS": "Vlogs",
            },
            "values": form_values(context),
            "video_errors": {},
            "upload_limit_mib": settings.profile_image_max_bytes / (1024 * 1024),
            **context,
        },
        headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        },
    )


def details(db, record):
    profile = db.get(CreatorProfile, record.id) if record else None
    image = (
        db.get(ProfileImage, profile.image_id) if profile and profile.image_id else None
    )
    return {
        "record": record,
        "profile": profile,
        "has_picture": image is not None
        and image.state == "ACTIVE"
        and image.event_id == record.event_id,
        "channels": db.scalars(
            select(CreatorChannel)
            .where(CreatorChannel.application_id == record.id)
            .order_by(CreatorChannel.id)
        ).all()
        if record
        else [],
        "videos": db.scalars(
            select(ConventionVideo)
            .where(ConventionVideo.application_id == record.id)
            .order_by(ConventionVideo.position)
        ).all()
        if record
        else [],
        "badge": db.scalar(select(Badge).where(Badge.application_id == record.id))
        if record
        else None,
    }


def integer(data, field):
    try:
        return int(data[field])
    except ValueError, TypeError, KeyError:
        raise HTTPException(422, "Invalid form version or application ID") from None


@router.get("/account", include_in_schema=False)
def account():
    # Preserve old bookmarks without a dedicated logout page.
    return RedirectResponse("/", status_code=303)


@router.get("/applications")
def dashboard(request: Request, db: DB, user: User):
    event = workflow.active_event(db)
    record = workflow.application_for_user(db, user.id, event.id)
    return render(
        request,
        "dashboard.html",
        event=event,
        editable=workflow.applicant_can_edit(event, record, datetime.now(UTC))
        if record
        else workflow.window_open(event, datetime.now(UTC)),
        **details(db, record),
    )


@router.get("/applications/form")
def application_form(request: Request, db: DB, user: User):
    event = workflow.active_event(db)
    record = workflow.application_for_user(db, user.id, event.id)
    if not workflow.window_open(event, datetime.now(UTC)) or (
        record and not workflow.applicant_can_edit(event, record, datetime.now(UTC))
    ):
        return render(
            request,
            "dashboard.html",
            status_code=409,
            event=event,
            editable=False,
            **details(db, record),
        )
    return render(request, "form.html", administrative=False, **details(db, record))


@router.post("/applications/form")
async def save_application(
    request: Request,
    db: DB,
    user: User,
    registration: Registration,
    identity: Identity,
    store: Store,
):
    form = await protected_form(request, upload=True)
    try:
        user_id = user.id
        record = workflow.application_for_user(db, user_id, settings.active_event_id)
        try:
            data = parse_application(
                form,
                workflow.current_input(db, record)
                if form.get("application_id")
                else None,
            )
        except ValueError as error:
            record = workflow.application_for_user(
                db, user_id, settings.active_event_id
            )
            return render(
                request,
                "form.html",
                status_code=422,
                administrative=False,
                submitted=form,
                error=str(error),
                video_errors=getattr(error, "video_errors", {}),
                **details(db, record),
            )
        try:
            if form.get("application_id"):
                workflow.edit(
                    db,
                    user_id,
                    integer(form, "application_id"),
                    integer(form, "version"),
                    data,
                )
            else:
                file = form.get("picture")
                picture = (
                    await file.read(settings.profile_image_max_bytes + 1)
                    if isinstance(file, UploadFile)
                    else None
                )
                await workflow.submit(
                    db,
                    user_id,
                    data,
                    registration,
                    identity,
                    picture=picture,
                    store=store,
                )
        except HTTPException as error:
            if error.status_code not in (409, 413, 422, 503):
                raise
            db.rollback()
            record = workflow.application_for_user(
                db, user_id, settings.active_event_id
            )
            return render(
                request,
                "form.html",
                status_code=error.status_code,
                administrative=False,
                submitted=form,
                error=error.detail,
                **details(db, record),
            )
        return RedirectResponse("/applications", status_code=303)
    finally:
        await form.close()


@router.get("/admin/applications")
def admin_list(request: Request, db: DB, user: User):
    require_admin(db, user.id)
    event = workflow.active_event(db)
    records = db.scalars(
        select(CreatorApplication)
        .where(CreatorApplication.event_id == event.id)
        .order_by(CreatorApplication.created_at, CreatorApplication.id)
    ).all()
    return render(request, "admin_list.html", event=event, records=records)


@router.get("/admin/applications/{application_id}")
def admin_detail(application_id: int, request: Request, db: DB, user: User):
    require_admin(db, user.id)
    event = workflow.active_event(db)
    record = workflow.get_application(db, application_id, event.id)
    return render_review(request, db, record)


def render_review(request, db, record, **context):
    from app.moderation.service import warnings

    return render(
        request,
        "admin_detail.html",
        transitions=workflow.TRANSITIONS[record.status],
        banned=warnings(db, record.id),
        **details(db, record),
        **context,
    )


@router.post("/admin/applications/{application_id}")
async def admin_review(
    application_id: int,
    request: Request,
    db: DB,
    user: User,
    registration: Registration,
    identity: Identity,
):
    require_admin(db, user.id)
    actor_id = user.id
    form = await protected_form(request)
    try:
        await workflow.review(
            db,
            actor_id,
            application_id,
            integer(form, "version"),
            form.get("status", ""),
            form.get("reason", ""),
            form.get("staff_notes", ""),
            form.get("exceptional") == "yes",
            registration,
            identity,
        )
    except HTTPException as error:
        if error.status_code not in (409, 422, 503):
            raise
        db.rollback()
        require_admin(db, actor_id)
        record = workflow.get_application(
            db, application_id, workflow.active_event(db).id
        )
        return render_review(
            request,
            db,
            record,
            status_code=error.status_code,
            submitted=form,
            error=error.detail,
        )
    finally:
        await form.close()
    return RedirectResponse(f"/admin/applications/{application_id}", status_code=303)


@router.get("/admin/applications/{application_id}/correct")
def correction_form(application_id: int, request: Request, db: DB, user: User):
    require_admin(db, user.id)
    record = workflow.get_application(db, application_id, workflow.active_event(db).id)
    return render(request, "form.html", administrative=True, **details(db, record))


@router.post("/admin/applications/{application_id}/correct")
async def correct(application_id: int, request: Request, db: DB, user: User):
    require_admin(db, user.id)
    actor_id = user.id
    form = await protected_form(request)
    record = workflow.get_application(db, application_id, workflow.active_event(db).id)
    try:
        data = parse_application(form, workflow.current_input(db, record))
        workflow.edit(
            db,
            actor_id,
            application_id,
            integer(form, "version"),
            data,
            administrative=True,
            reason=form.get("correction_reason", ""),
        )
    except (ValueError, HTTPException) as error:
        if isinstance(error, HTTPException) and error.status_code not in (
            409,
            422,
            503,
        ):
            raise
        db.rollback()
        record = workflow.get_application(
            db, application_id, workflow.active_event(db).id
        )
        return render(
            request,
            "form.html",
            status_code=error.status_code if isinstance(error, HTTPException) else 422,
            administrative=True,
            submitted=form,
            error=error.detail if isinstance(error, HTTPException) else str(error),
            video_errors=getattr(error, "video_errors", {}),
            **details(db, record),
        )
    return RedirectResponse(f"/admin/applications/{application_id}", status_code=303)
