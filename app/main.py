import logging

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.sessions import SessionMiddleware

from app.applications.pages import DB
from app.applications.pages import router as pages_router
from app.applications.pages import static as application_static
from app.applications.routes import router as applications_router
from app.auth.routes import router as auth_router
from app.config import settings
from app.creators.pages import router as creators_router
from app.gallery.pages import router as gallery_router
from app.staff.pages import router as staff_router

if settings.session_secret is None or (
    len(settings.session_secret.get_secret_value()) < 32
    or settings.session_secret.get_secret_value().startswith("INSERT_")
):
    raise RuntimeError(
        "SESSION_SECRET must be a random secret of at least 32 characters"
    )


app = FastAPI(title=settings.app_name)


@app.exception_handler(SQLAlchemyError)
async def database_failure(request, error):
    # Database exception details can contain row values even with hidden binds.
    logging.getLogger(__name__).error(
        "Database operation failed (%s)", type(error).__name__
    )
    return JSONResponse(
        {"detail": "Database operation unavailable. Please retry."}, status_code=503
    )


app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret.get_secret_value(),
    session_cookie="creator_session",
    same_site="lax",
    https_only=settings.environment != "development",
    max_age=settings.session_max_age,
)

app.include_router(auth_router)
app.include_router(applications_router)
app.include_router(pages_router)
app.include_router(creators_router)
app.include_router(staff_router)
app.include_router(gallery_router)
app.mount("/application-assets", application_static, name="application-assets")


@app.middleware("http")
async def response_security(request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Cache-Control", "no-store")
    return response


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready")
def ready(db: DB):
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError:
        return JSONResponse(
            {"status": "unavailable"},
            status_code=503,
            headers={"Cache-Control": "no-store"},
        )
    return JSONResponse({"status": "ready"}, headers={"Cache-Control": "no-store"})
