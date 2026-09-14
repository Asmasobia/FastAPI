"""
user_service.py — user registration, lookup, login, and profile update.

Every function takes an AsyncSession `db` as its first argument. That session
comes from the get_db dependency, so the service participates in the request's
transaction. Services COMMIT when a write completes successfully.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.exceptions import AuthError, ConflictError, NotFoundError
from src.models.user import User
from src.schemas.user import UserCreate, UserUpdate
from src.security import hash_password, verify_password


async def get_user_by_email(db: AsyncSession, email: str) -> User | None:
    """Fetch a user by email, or None. Used by registration + login."""
    result = await db.execute(select(User).where(User.email == email))
    return result.scalar_one_or_none()


async def get_user_by_id(db: AsyncSession, user_id: str) -> User:
    """
    Fetch a user by id or raise NotFoundError. The "or raise" variant keeps
    routers clean: they don't each have to check for None.
    """
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise NotFoundError(f"User {user_id} not found")
    return user


async def register_user(db: AsyncSession, data: UserCreate) -> User:
    """
    Create a new user.

    ORDER OF OPERATIONS MATTERS:
      1. Reject duplicate emails up front with a clear 409 Conflict, instead of
         letting the DB's UNIQUE constraint blow up with an opaque error. (The
         DB constraint is still there as the ultimate guard against races.)
      2. HASH the password — we store the hash, never the plaintext.
      3. add + commit + refresh:
           - add() stages the new row in the session (no SQL yet).
           - commit() flushes the INSERT and commits the transaction.
           - refresh() reloads the row so server-generated values (created_at
             from server_default) are populated on the object we return.
    """
    if await get_user_by_email(db, data.email) is not None:
        raise ConflictError(
            f"A user with email {data.email} already exists"
        )

    user = User(
        email=data.email,
        full_name=data.full_name,
        hashed_password=hash_password(data.password),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def authenticate_user(
    db: AsyncSession, email: str, password: str
) -> User:
    """
    Verify login credentials and return the user, or raise AuthError.

    SECURITY DETAIL — we raise the SAME error whether the email doesn't exist or
    the password is wrong ("Incorrect email or password"). Telling the attacker
    "that email isn't registered" would leak which emails have accounts (user
    enumeration). One generic message closes that hole.
    """
    user = await get_user_by_email(db, email)
    if user is None or not verify_password(password, user.hashed_password):
        raise AuthError("Incorrect email or password")
    if not user.is_active:
        raise AuthError("User account is disabled")
    return user


async def update_user(
    db: AsyncSession, user: User, data: UserUpdate
) -> User:
    """
    Partially update the given user (their own profile).

    exclude_unset=True is the KEY to correct PATCH semantics: it returns only
    the fields the client actually SENT, so we don't overwrite untouched fields
    with their defaults (None). We then apply each provided field, hashing the
    password if it's among them.
    """
    updates = data.model_dump(exclude_unset=True)

    if "password" in updates:
        # Never store the raw password; replace it with its hash in-place.
        updates["hashed_password"] = hash_password(updates.pop("password"))

    for field, value in updates.items():
        setattr(user, field, value)

    await db.commit()
    await db.refresh(user)
    return user
