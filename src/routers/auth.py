"""
auth.py — registration and login endpoints.

These are the only endpoints that are PUBLIC (no token required) — everything
else depends on get_current_user. Login is what MINTS the token the rest of the
API consumes.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, status
from fastapi.security import OAuth2PasswordRequestForm

from src.dependencies import DbSession
from src.schemas.user import Token, UserCreate, UserOut
from src.security import create_access_token
from src.services import user_service

# prefix -> every path here is under /auth. tags -> groups these in /docs.
router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/register",
    response_model=UserOut,  # FastAPI validates+shapes the return through this.
    status_code=status.HTTP_201_CREATED,  # 201 = "created", the right code here.
    summary="Register a new user",
)
async def register(data: UserCreate, db: DbSession) -> UserOut:
    """
    Create an account.

    HOW THE PIECES FIT:
      - `data: UserCreate` — FastAPI reads the JSON body and validates it against
        UserCreate (email format, password length). Invalid -> 422 automatically,
        before this function runs.
      - `db: DbSession` — the injected session (Depends(get_db) under the alias).
      - We delegate to the service, which hashes the password and handles the
        duplicate-email conflict.
      - `response_model=UserOut` guarantees the returned User is serialized WITHOUT
        the password hash — even though we return the full ORM object, only
        UserOut's fields go out. That's the security guarantee of response models.
    """
    user = await user_service.register_user(db, data)
    return user  # ORM object; from_attributes on UserOut converts it.


@router.post("/login", response_model=Token, summary="Log in and get a JWT")
async def login(
    db: DbSession,
    # OAuth2PasswordRequestForm is a FastAPI-provided dependency that reads a
    # FORM body with fields `username` and `password` (the OAuth2 standard). We
    # treat `username` as the email. Wrapping it in Depends() (via Annotated)
    # tells FastAPI to build it from the form body. Using this exact type is what
    # makes the /docs "Authorize" button work out of the box.
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
) -> Token:
    """
    Verify credentials and return a signed JWT.

    WHY FORM DATA, NOT JSON:
      The OAuth2 "password flow" spec sends credentials as
      application/x-www-form-urlencoded with fields username/password. Following
      the standard means standard clients + Swagger UI's Authorize dialog just
      work. (This is why python-multipart is a dependency — it parses forms.)

    FLOW:
      authenticate_user checks email+password (or raises AuthError -> 401), then
      we mint a token whose `sub` is the user's id. The client stores this token
      and sends it as `Authorization: Bearer <token>` on subsequent requests.
    """
    user = await user_service.authenticate_user(
        db, email=form_data.username, password=form_data.password
    )
    token = create_access_token(subject=user.id)
    return Token(access_token=token)
