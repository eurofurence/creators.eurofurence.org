from authlib.integrations.base_client import OAuthError
from fastapi import APIRouter, HTTPException, Request
from starlette.responses import RedirectResponse

from app.config import settings

from app.auth.client import eurofurence
from app.auth.dependencies import get_current_user_sub


router = APIRouter(
    prefix="/auth",
    tags=["auth"],
)


@router.get("/login", name="auth_login")
async def login(request: Request) -> RedirectResponse:
    if not all(
        (
            settings.oidc_client_id,
            settings.oidc_issuer_url,
            settings.oidc_token_endpoint_auth_method,
            settings.oidc_redirect_uri,
        )
    ):
        raise HTTPException(
            status_code=503,
            detail="OIDC client is not configured",
        )

    return await eurofurence.authorize_redirect(
        request,
        settings.oidc_redirect_uri,
    )


@router.get("/callback", name="auth_callback")
async def callback(request: Request) -> dict[str, str]:
    try:
        token = await eurofurence.authorize_access_token(request)
    except OAuthError as exc:
        if request.query_params.get("error"):
            # Authlib returns provider errors before clearing transaction state.
            await eurofurence.framework.clear_state_data(
                request.session, request.query_params.get("state")
            )
        raise HTTPException(
            status_code=401,
            detail="OIDC authentication failed",
        ) from exc

    userinfo = token.get("userinfo")

    if userinfo is None:
        raise HTTPException(
            status_code=401,
            detail="OIDC response did not contain user information",
        )

    subject = userinfo.get("sub")

    if not isinstance(subject, str) or not subject:
        raise HTTPException(
            status_code=401,
            detail="OIDC response did not contain a subject identifier",
        )

    request.session.clear()
    request.session["user_sub"] = subject

    return {
        "status": "authenticated",
        "sub": subject,
    }


@router.get("/me")
async def me(request: Request) -> dict[str, str]:
    user_sub = get_current_user_sub(request)

    return {
        "sub": user_sub,
    }


@router.post("/logout")
async def logout(request: Request) -> dict[str, str]:
    request.session.clear()

    return {
        "status": "logged_out",
    }
