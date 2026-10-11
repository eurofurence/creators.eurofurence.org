from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.applications.workflow import active_event
from app.auth.dependencies import get_current_user
from app.database import get_db
from app.identity.models import LocalUser
from app.registration.eligibility import Eligibility
from app.registration.evidence import stored_snapshot

router = APIRouter(prefix="/applications", tags=["applications"])


class EligibilityResponse(BaseModel):
    status: Eligibility
    retryable: bool


@router.get("/eligibility", response_model=EligibilityResponse)
async def eligibility(
    response: Response,
    user: Annotated[LocalUser, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> EligibilityResponse:
    try:
        active_event(db)
        evidence = stored_snapshot(db, user.id, "creator")
    except SQLAlchemyError, HTTPException:
        evidence = None
    finally:
        # A status read never initiates another Registration verification.
        db.rollback()
    status = Eligibility.ELIGIBLE if evidence is not None else Eligibility.UNAVAILABLE
    response.headers["Cache-Control"] = "no-store"
    response.status_code = 503 if status is Eligibility.UNAVAILABLE else 200
    return EligibilityResponse(
        status=status, retryable=status is Eligibility.UNAVAILABLE
    )
