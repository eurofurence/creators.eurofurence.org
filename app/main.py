from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from app.auth.routes import router as auth_router
from app.config import settings


if settings.session_secret is None:
    raise RuntimeError("SESSION_SECRET must be configured")


app = FastAPI(title=settings.app_name)

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret.get_secret_value(),
    session_cookie="creator_session",
    same_site="lax",
    https_only=settings.environment != "development",
)

app.include_router(auth_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
