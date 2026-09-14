"""
users.py — endpoints for the currently-authenticated user's own profile.

We intentionally only expose "/users/me" style endpoints (not "list all users"
or "get user X"), because in this app a user should only see/edit themselves.
Every endpoint here depends on CurrentUser, so it's protected: no valid token,
no access.
"""

from fastapi import APIRouter

from src.dependencies import CurrentUser, DbSession
from src.schemas.user import UserOut, UserUpdate
from src.services import user_service

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me", response_model=UserOut, summary="Get my profile")
async def read_me(user: CurrentUser) -> UserOut:
    """
    Return the authenticated user's profile.

    Notice there is NO database code here — get_current_user already loaded the
    User while validating the token. We just return it. This is dependency
    injection paying off: the endpoint is a one-liner because the cross-cutting
    work (auth + load) happened in the dependency.
    """
    return user


@router.patch("/me", response_model=UserOut, summary="Update my profile")
async def update_me(
    data: UserUpdate, user: CurrentUser, db: DbSession
) -> UserOut:
    """
    Partially update the authenticated user's own profile (name and/or password).
    The service applies only the fields that were actually sent (PATCH semantics).
    """
    updated = await user_service.update_user(db, user, data)
    return updated
