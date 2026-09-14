"""
user.py (schemas) — request/response shapes for users, plus auth token schemas.

Notice the naming convention used throughout this project:
    XxxCreate  -> what the CLIENT sends to create an Xxx (input).
    XxxUpdate  -> what the client sends to modify an Xxx (input, all optional).
    XxxOut     -> what the SERVER returns (output; secrets stripped).
This separation is deliberate: inputs and outputs are different shapes.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class UserCreate(BaseModel):
    """Payload for POST /auth/register."""

    # EmailStr validates that the string is a syntactically valid email. If not,
    # Pydantic rejects the request with a 422 before our code runs. (This is why
    # we installed email-validator.)
    email: EmailStr
    # Field(...) with constraints: min/max length enforced automatically.
    full_name: str = Field(min_length=1, max_length=120)
    # We accept the PLAINTEXT password here (over HTTPS in prod), then hash it in
    # the service before storing. min_length=8 is a basic strength floor.
    # NOTE: this password is never echoed back — no XxxOut schema includes it.
    password: str = Field(min_length=8, max_length=128)


class UserUpdate(BaseModel):
    """
    Payload for PATCH /users/me. Every field is OPTIONAL (defaults to None) so a
    client can send just the fields they want to change — that's what makes it a
    PATCH (partial update) rather than a PUT (full replace).
    """

    full_name: str | None = Field(default=None, min_length=1, max_length=120)
    password: str | None = Field(default=None, min_length=8, max_length=128)


class UserOut(BaseModel):
    """
    What we return for a user. CRUCIALLY: no password / hashed_password field,
    so those can never be serialized into a response.
    """

    # ConfigDict(from_attributes=True) is the Pydantic v2 replacement for v1's
    # `orm_mode`. It lets Pydantic build this schema by reading ATTRIBUTES off an
    # arbitrary object (like a SQLAlchemy User) instead of only from a dict. So
    # in endpoints we can `return user_orm_object` and FastAPI converts it using
    # this schema — reading user.id, user.email, etc. Without this, you'd have to
    # hand-build a dict every time.
    model_config = ConfigDict(from_attributes=True)

    id: str
    email: EmailStr
    full_name: str
    is_active: bool
    created_at: datetime


# ---- Auth token schemas ----
class Token(BaseModel):
    """
    The response of a successful login. This EXACT shape (access_token +
    token_type) is the OAuth2 standard the /docs "Authorize" button expects,
    which is why the login endpoint uses it.
    """

    access_token: str
    token_type: str = "bearer"


class TokenPayload(BaseModel):
    """
    The decoded contents ("claims") of a JWT, validated after we decode it.

    Standard JWT claim names:
      sub  = "subject" — WHO the token is about (here, the user id).
      exp  = expiry timestamp — after this, the token is invalid.
      iat  = issued-at timestamp — when it was created.
    Modeling the payload as a schema means even the token's internals are typed
    and validated, not a loose dict.
    """

    sub: str
    exp: int | None = None
    iat: int | None = None
