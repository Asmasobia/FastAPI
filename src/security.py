"""
security.py — PASSWORD HASHING and JWT (JSON Web Token) creation/verification.

This module is pure, framework-agnostic logic: no FastAPI, no DB. It just knows
how to (a) hash/verify passwords and (b) mint/validate tokens. Keeping it pure
makes it trivial to unit-test and reuse.

TWO SECURITY PRIMITIVES LIVE HERE:

1) PASSWORD HASHING (bcrypt)
   We never store plaintext passwords. We store a bcrypt HASH. Bcrypt is:
     - one-way (you can't reverse the hash back to the password),
     - salted (each hash embeds a random salt, so two identical passwords get
       different hashes — defeats precomputed "rainbow table" attacks),
     - deliberately SLOW (a tunable "work factor"), which makes brute-forcing
       stolen hashes expensive.

2) JWT (stateless auth tokens)
   After login we hand the client a signed token. On each later request the
   client sends it back; we verify the signature and read the user id from it.
   "Stateless" = the server stores nothing; the token itself carries the proof.
   See the README for the JWT-vs-session-cookie tradeoff (a classic interview Q).
"""

from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt
from passlib.context import CryptContext

from src.config import get_settings

settings = get_settings()

# CryptContext is passlib's "which algorithm(s) do we use" manager.
#   schemes=["bcrypt"] -> hash with bcrypt.
#   deprecated="auto"  -> if we later add a newer scheme, passlib will flag old
#                         hashes as needing a rehash. Future-proofing in one line.
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


# ------------------------------------------------------------------------------
# PASSWORD HELPERS
# ------------------------------------------------------------------------------
def hash_password(plain_password: str) -> str:
    """Turn a plaintext password into a bcrypt hash (safe to store)."""
    return pwd_context.hash(plain_password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """
    Check a login attempt: does `plain_password` match the stored `hashed_password`?

    passlib re-hashes the attempt using the salt embedded in the stored hash and
    compares. It uses a constant-time comparison internally to avoid timing
    attacks. Returns True/False; never raises on a wrong password.
    """
    return pwd_context.verify(plain_password, hashed_password)


# ------------------------------------------------------------------------------
# JWT HELPERS
# ------------------------------------------------------------------------------
def create_access_token(subject: str) -> str:
    """
    Create a signed JWT whose "subject" (sub) is the user's id.

    A JWT is three base64 parts joined by dots: header.payload.signature.
      - The PAYLOAD (claims) is readable by anyone — it is NOT encrypted, only
        SIGNED. So never put secrets in it. Here we store only the user id and
        timestamps.
      - The SIGNATURE is an HMAC of header+payload using our secret. Anyone can
        READ the payload, but nobody can FORGE a valid one without the secret.
        If a byte of the payload is changed, the signature no longer matches and
        verification fails.

    Claims we set:
      sub = user id (who this token authenticates).
      iat = issued-at (now).
      exp = expiry (now + configured minutes). python-jose checks exp on decode
            and rejects expired tokens automatically.
    """
    now = datetime.now(timezone.utc)
    expire = now + timedelta(minutes=settings.access_token_expire_minutes)
    claims = {
        "sub": subject,
        "iat": int(now.timestamp()),
        "exp": int(expire.timestamp()),
    }
    # Sign with our secret + chosen algorithm (HS256). This produces the token.
    return jwt.encode(
        claims, settings.jwt_secret_key, algorithm=settings.jwt_algorithm
    )


def decode_access_token(token: str) -> str | None:
    """
    Verify a token and return its subject (user id), or None if it's invalid.

    jwt.decode does the heavy lifting: it recomputes the signature with our
    secret and rejects the token if the signature is wrong OR if it's expired
    (raising JWTError). We catch that and return None so callers can respond with
    a clean 401 instead of a stack trace.

    We deliberately return None rather than raising here, so the AUTH DEPENDENCY
    (dependencies.py) decides the HTTP response. This module stays HTTP-agnostic.
    """
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
        )
    except JWTError:
        # Covers bad signature, malformed token, and expired token.
        return None
    # "sub" is where we stored the user id. Missing sub => not a token we minted.
    return payload.get("sub")
