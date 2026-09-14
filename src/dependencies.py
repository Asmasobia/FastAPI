"""
dependencies.py — FastAPI DEPENDENCIES used across routers.

WHAT IS A "DEPENDENCY" IN FASTAPI (this is THE core FastAPI concept)?
  A dependency is just a function whose RESULT gets injected into your endpoint.
  You declare a parameter like `db: AsyncSession = Depends(get_db)` and FastAPI:
    1. Sees the Depends() marker.
    2. Calls get_db() for you (resolving ITS dependencies first, recursively).
    3. Passes the result in as `db`.
    4. Caches it for the duration of one request (so asking for the same
       dependency twice in one request runs it once).

  WHY THIS IS POWERFUL:
    - Reuse: write "get the current user" once, use it in 20 endpoints.
    - Testability: swap any dependency in tests via app.dependency_overrides
      (e.g. inject a fake user or a test DB) without touching endpoint code.
    - Separation: endpoints stay focused on their job; cross-cutting concerns
      (auth, DB sessions, config) are pulled in declaratively.

This module wires three things every protected endpoint needs:
  get_settings  -> configuration
  get_db        -> a database session (re-exported from db.py)
  get_current_user -> the authenticated User (decodes the JWT, loads the user)
"""

from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# Re-export get_db here so routers import all their deps from one place.
from src.cache import CacheClient
from src.config import Settings, get_settings
from src.db import get_db
from src.exceptions import AuthError
from src.models.user import User
from src.security import decode_access_token

# OAuth2PasswordBearer tells FastAPI: "expect a token in the
# `Authorization: Bearer <token>` header." tokenUrl points at our login endpoint
# — it's what makes the /docs "Authorize" button know where to POST credentials.
# This object, used as a dependency, EXTRACTS the raw token string from the
# header (and 401s automatically if the header is missing/malformed).
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


# ------------------------------------------------------------------------------
# TYPE ALIASES using Annotated — a modern FastAPI ergonomics trick.
#
# Instead of writing `db: AsyncSession = Depends(get_db)` in every single
# endpoint, we define the alias once here and then endpoints write
# `db: DbSession`. Cleaner signatures, one place to change. `Annotated[X, dep]`
# attaches the dependency to the type itself.
# ------------------------------------------------------------------------------
SettingsDep = Annotated[Settings, Depends(get_settings)]
DbSession = Annotated[AsyncSession, Depends(get_db)]
TokenDep = Annotated[str, Depends(oauth2_scheme)]


async def get_current_user(token: TokenDep, db: DbSession) -> User:
    """
    The AUTHENTICATION dependency. Any endpoint that declares
    `user: CurrentUser` becomes protected: unauthenticated requests get 401.

    STEP BY STEP:
      1. `token: TokenDep` -> oauth2_scheme has already pulled the raw JWT out of
         the Authorization header. (If the header was missing, FastAPI already
         returned 401 before we even get here.)
      2. decode_access_token verifies the signature + expiry and returns the
         user id (sub), or None if the token is invalid/expired.
      3. We look that user up in the DB. Even a validly-signed token is useless
         if the user was deleted or deactivated — so we check existence AND
         is_active. This is why auth is a DB-touching dependency, not pure token
         decoding.
      4. Return the User ORM object. FastAPI injects it into the endpoint, and
         because dependencies are cached per-request, every dependency in the
         same request that needs the current user shares this one lookup.

    We raise our own AuthError (not HTTPException) so the central handler renders
    the standard error envelope with a trace id.
    """
    user_id = decode_access_token(token)
    if user_id is None:
        raise AuthError("Invalid or expired authentication token")

    # select(User).where(...) is the SQLAlchemy 2.x query style. We await the
    # execution because it's an async session. scalar_one_or_none() returns the
    # single User or None (rather than a row tuple).
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if user is None:
        raise AuthError("User for this token no longer exists")
    if not user.is_active:
        raise AuthError("User account is disabled")

    return user


# Another alias so endpoints can just annotate `user: CurrentUser`.
CurrentUser = Annotated[User, Depends(get_current_user)]


def get_cache(request: Request) -> CacheClient:
    """
    Provide the shared CacheClient to endpoints.

    WHY IT LIVES ON app.state (and not a module global):
      The cache client owns a connection pool that must be created at startup
      and CLOSED at shutdown. The lifespan (main.py) builds it once and stores it
      at app.state.cache. Here we read it off the incoming request (every request
      knows its app). One client, shared, with a managed lifecycle — the same
      reasoning as the DB engine.
    """
    return request.app.state.cache


CacheDep = Annotated[CacheClient, Depends(get_cache)]
