import logging
import time
from collections.abc import Mapping
from typing import Annotated

from authlib.integrations.base_client import OAuthError
from fastapi import APIRouter, Depends, HTTPException, Request
from httpx2 import HTTPError
from joserfc.errors import JoseError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.responses import RedirectResponse

from app.auth.client import (
    eurofurence,
    require_configuration,
    require_provider_metadata,
)
from app.auth.dependencies import get_current_user
from app.config import settings
from app.database import get_db
from app.identity.models import LocalUser
from app.identity.service import resolve_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


def authentication_error(request: Request, status: int, detail: str) -> HTTPException:
    request.session.clear()
    # Never log exception text, callback parameters, tokens, or provider descriptions.
    logger.warning("OIDC authentication failed (%s)", status)
    return HTTPException(status_code=status, detail=detail)


@router.get("/login", name="auth_login")
async def login(request: Request) -> RedirectResponse:
    request.session.clear()
    try:
        require_configuration()
    except ValueError:
        raise authentication_error(
            request, 503, "OIDC client is not configured"
        ) from None
    try:
        await require_provider_metadata()
        return await eurofurence.authorize_redirect(request, settings.oidc_redirect_uri)
    except (OAuthError, JoseError, HTTPError, ValueError, KeyError, TypeError):
        raise authentication_error(
            request, 503, "Identity provider unavailable"
        ) from None


@router.get("/callback", name="auth_callback")
async def callback(
    request: Request, db: Annotated[Session, Depends(get_db)]
) -> dict[str, str | int]:
    try:
        require_configuration()
    except ValueError:
        raise authentication_error(
            request, 503, "OIDC client is not configured"
        ) from None

    try:
        # Authlib performs state matching and nonce/PKCE validation. Require a
        # complete, unexpired local transaction so ID-token parsing cannot be skipped.
        state = request.query_params.get("state")
        transaction = request.session.get(f"_state_eurofurence_{state}", {})
        data = transaction.get("data", {})
        if (
            not state
            or transaction.get("exp", 0) <= time.time()
            or not data.get("nonce")
            or not data.get("code_verifier")
            or (
                not request.query_params.get("code")
                and not request.query_params.get("error")
            )
        ):
            raise OAuthError(error="invalid_transaction")
        await require_provider_metadata()
        token = await eurofurence.authorize_access_token(
            request,
            claims_options={
                "iss": {"essential": True, "value": settings.oidc_issuer_url},
                "sub": {"essential": True},
                "nonce": {"essential": True, "value": data["nonce"]},
            },
        )
        claims = token.get("userinfo")
        if not token.get("id_token") or not isinstance(claims, Mapping):
            raise OAuthError(error="invalid_id_token")
        issuer, subject = claims.get("iss"), claims.get("sub")
        if (
            issuer != settings.oidc_issuer_url
            or not isinstance(subject, str)
            or not subject
        ):
            raise OAuthError(error="invalid_identity")
    except (OAuthError, JoseError):
        raise authentication_error(request, 401, "OIDC authentication failed") from None
    except (HTTPError, ValueError, KeyError, TypeError, AttributeError):
        raise authentication_error(
            request, 503, "Identity provider unavailable"
        ) from None

    request.session.clear()
    try:
        user = resolve_user(db, issuer, subject)
    except SQLAlchemyError:
        db.rollback()
        raise authentication_error(
            request, 503, "Authentication storage unavailable"
        ) from None
    request.session["user_id"] = user.id
    return {"status": "authenticated", "user_id": user.id}


@router.get("/me")
def me(user: Annotated[LocalUser, Depends(get_current_user)]) -> dict[str, int]:
    return {"user_id": user.id}


@router.post("/logout")
async def logout(request: Request) -> dict[str, str]:
    request.session.clear()
    return {"status": "logged_out"}
