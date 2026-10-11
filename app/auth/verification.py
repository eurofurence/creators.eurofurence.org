"""Explicit participant verification using a fresh OIDC grant when configured."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import SecretStr
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.responses import RedirectResponse

from app.applications.pages import render
from app.applications.security import protected_form
from app.applications.service import registration_lookup_for_user
from app.applications.workflow import active_event
from app.auth.dependencies import get_current_user
from app.config import settings
from app.database import get_db
from app.identity.models import LocalUser
from app.identity.profile import IdentityProfileClient, get_identity_profile_client
from app.registration.client import (
    RegistrationClient,
    RegistrationUnavailable,
    get_registration_client,
)
from app.registration.evidence import stored_snapshot, verify_participant
from app.registration.v1 import RegistrationV1Client

router = APIRouter(prefix="/auth/registration", tags=["auth"])
DB = Annotated[Session, Depends(get_db)]
User = Annotated[LocalUser, Depends(get_current_user)]
Identity = Annotated[IdentityProfileClient, Depends(get_identity_profile_client)]
Registration = Annotated[RegistrationClient, Depends(get_registration_client)]


def purpose_value(value):
    if value not in ("creator", "helper"):
        raise HTTPException(422, "Choose creator or helper verification.")
    return value


def result_redirect(request, purpose, message=None):
    if message:
        request.session["registration_result"] = {
            "purpose": purpose,
            "message": message,
        }
    return RedirectResponse(f"/auth/registration?purpose={purpose}", status_code=303)


@router.get("")
def verification_page(request: Request, db: DB, user: User, purpose: str = "creator"):
    purpose = purpose_value(purpose)
    event = active_event(db)
    result = request.session.pop("registration_result", {})
    return render(
        request,
        "registration_verification.html",
        purpose=purpose,
        event=event,
        verified=stored_snapshot(db, user.id, purpose) is not None,
        error=result.get("message") if result.get("purpose") == purpose else None,
    )


@router.post("")
async def start_verification(
    request: Request, db: DB, user: User, registration: Registration, identity: Identity
):
    form = await protected_form(request)
    purpose = purpose_value(form.get("purpose"))
    event = active_event(db)
    user_id, identity_key = user.id, user.session_key
    event_context = event.id, event.year
    routes = [
        route for route in settings.registration_v1_events if route.event_id == event.id
    ]
    needs_user_token = settings.registration_provider == "v1" and any(
        route.auth_method == "user_bearer" for route in routes
    )
    if needs_user_token and stored_snapshot(db, user_id, purpose) is None:
        lookup = registration_lookup_for_user(db, user_id)
        try:
            if lookup is None:
                raise RegistrationUnavailable
            RegistrationV1Client(routes, environment=settings.environment)._event(
                lookup
            )
        except RegistrationUnavailable:
            return result_redirect(
                request,
                purpose,
                "Registration verification is not configured for this event. Please contact an administrator.",
            )
        from app.auth.routes import login

        form_csrf = request.session.get("csrf")
        db.rollback()
        response = await login(request)
        # Only the intent is signed into the cookie; no token, profile or invitation.
        request.session["registration_verification"] = {
            "user_id": user_id,
            "identity_key": identity_key,
            "purpose": purpose,
            "event_id": event_context[0],
            "event_year": event_context[1],
            "form_csrf": form_csrf,
        }
        return response
    try:
        await verify_participant(
            db, user_id, purpose, registration, identity, expected_event=event_context
        )
    except HTTPException as error:
        db.rollback()
        return result_redirect(request, purpose, error.detail)
    return result_redirect(request, purpose)


async def complete_verification(request, db, user_id, intent, token, identity):
    """Runs only after Authlib validates the same user's OIDC callback."""
    purpose = purpose_value(intent["purpose"])
    try:
        event = active_event(db)
        if (event.id, event.year) != (intent["event_id"], intent["event_year"]):
            raise HTTPException(409, "The event changed. Retry verification.")
        lookup = registration_lookup_for_user(db, user_id)
        claims = token["userinfo"]
        if lookup is None or (lookup.issuer, lookup.subject) != (
            claims["iss"],
            claims["sub"],
        ):
            raise HTTPException(409, "The identity changed. Retry verification.")
        access_token = token.get("access_token")
        if (
            not isinstance(access_token, str)
            or not access_token
            or str(token.get("token_type", "")).lower() != "bearer"
        ):
            raise HTTPException(
                503, "A fresh user access token is unavailable. Retry verification."
            )
        if settings.registration_provider != "v1":
            raise HTTPException(
                503, "Registration configuration changed. Retry verification."
            )
        client = RegistrationV1Client(
            settings.registration_v1_events,
            environment=settings.environment,
            user_token=SecretStr(access_token),
            token_lookup=lookup,
        )
        if lookup is None or client._event(lookup).auth_method != "user_bearer":
            raise HTTPException(
                503, "User-authorized Registration is unavailable. Retry verification."
            )
        await verify_participant(
            db,
            user_id,
            purpose,
            client,
            identity,
            expected_event=(event.id, event.year),
        )
    except HTTPException as error:
        db.rollback()
        return result_redirect(request, purpose, error.detail)
    except RegistrationUnavailable, SQLAlchemyError:
        db.rollback()
        return result_redirect(
            request, purpose, "Registration verification is unavailable. Please retry."
        )
    return result_redirect(request, purpose)
