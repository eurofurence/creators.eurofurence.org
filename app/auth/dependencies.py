from fastapi import HTTPException, Request


def get_current_user_sub(request: Request) -> str:
    user_sub = request.session.get("user_sub")

    if not user_sub:
        raise HTTPException(
            status_code=401,
            detail="Not authenticated",
        )

    return user_sub
