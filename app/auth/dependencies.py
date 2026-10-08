from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.applications.models import LocalRoleAssignment
from app.database import get_db
from app.identity.models import LocalUser


def get_optional_user(
    request: Request, db: Annotated[Session, Depends(get_db)]
) -> LocalUser | None:
    user_id = request.session.get("user_id")
    if type(user_id) is int and user_id > 0:
        user = db.get(LocalUser, user_id)
        if user is not None and request.session.get("identity_key") == user.session_key:
            request.state.user = user
            roles = set(
                db.scalars(
                    select(LocalRoleAssignment.role).where(
                        LocalRoleAssignment.user_id == user.id
                    )
                )
            )
            request.state.is_admin = "ADMIN" in roles
            request.state.is_staff = bool(roles & {"ADMIN", "BADGE_STAFF"})
            return user
    if user_id is not None:
        request.session.clear()
    return None


def get_current_user(
    request: Request, db: Annotated[Session, Depends(get_db)]
) -> LocalUser:
    user = get_optional_user(request, db)
    if user is not None:
        return user
    request.session.clear()
    raise HTTPException(status_code=401, detail="Not authenticated")
