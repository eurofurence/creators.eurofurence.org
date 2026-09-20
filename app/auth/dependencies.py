from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.identity.models import LocalUser


def get_current_user(
    request: Request, db: Annotated[Session, Depends(get_db)]
) -> LocalUser:
    user_id = request.session.get("user_id")
    if type(user_id) is int and user_id > 0:
        user = db.get(LocalUser, user_id)
        if user is not None:
            return user
    request.session.clear()
    raise HTTPException(status_code=401, detail="Not authenticated")
