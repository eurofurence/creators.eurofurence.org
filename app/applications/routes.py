from typing import Annotated

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.applications.service import registration_lookup_for_user
from app.auth.dependencies import get_current_user
from app.database import get_db
from app.identity.models import LocalUser
from app.registration.client import RegistrationClient, get_registration_client
from app.registration.eligibility import Eligibility, check_eligibility

router = APIRouter(prefix="/applications", tags=["applications"])


class EligibilityResponse(BaseModel):
    status: Eligibility
    retryable: bool


@router.get("/eligibility", response_model=EligibilityResponse)
async def eligibility(
    response: Response,
    user: Annotated[LocalUser, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    registration: Annotated[RegistrationClient, Depends(get_registration_client)],
) -> EligibilityResponse:
    try:
        lookup = registration_lookup_for_user(db, user.id)
    except SQLAlchemyError:
        lookup = None
    finally:
        # This read-only endpoint releases its DB transaction before external I/O.
        db.rollback()
    status = (
        await check_eligibility(registration, lookup)
        if lookup is not None
        else Eligibility.UNAVAILABLE
    )
    response.headers["Cache-Control"] = "no-store"
    response.status_code = 503 if status is Eligibility.UNAVAILABLE else 200
    return EligibilityResponse(
        status=status, retryable=status is Eligibility.UNAVAILABLE
    )
