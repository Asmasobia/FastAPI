"""
error_handlers.py — turn exceptions into our CONSISTENT error envelope.

GOAL: every error the API returns looks the same:
    {"error": {"code": "...", "message": "...", "trace_id": "..."}}
so clients can handle failures uniformly and every error is traceable.

We register handlers in main.py. When a matching exception bubbles up, FastAPI
calls the handler instead of returning a raw stack trace. We cover:
  - our own AppError family (domain errors from services) -> mapped status codes.
  - FastAPI's RequestValidationError (bad input, normally a 422 with a
    different shape) -> re-wrapped into our envelope.
  - a catch-all Exception -> 500 without leaking internals.
"""

import logging

from fastapi import Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.exceptions import (
    AppError,
    AuthError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
)

logger = logging.getLogger("app.errors")

# Map each domain error type to the HTTP status code it should produce. This is
# the single place that decides "NotFound -> 404", keeping status logic out of
# both services (which shouldn't know HTTP) and routers.
_STATUS_BY_ERROR: dict[type[AppError], int] = {
    NotFoundError: status.HTTP_404_NOT_FOUND,
    PermissionDeniedError: status.HTTP_403_FORBIDDEN,
    ConflictError: status.HTTP_409_CONFLICT,
    AuthError: status.HTTP_401_UNAUTHORIZED,
}


def _trace_id(request: Request) -> str | None:
    """Pull the request id the middleware stashed on request.state (if any)."""
    return getattr(request.state, "request_id", None)


def _envelope(code: str, message: str, trace_id: str | None) -> dict:
    """Build the standard error body once, so every handler is consistent."""
    return {"error": {"code": code, "message": message, "trace_id": trace_id}}


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    """
    Handle any of our domain errors. We look up the right status from the map;
    unknown AppError subclasses default to 400 Bad Request.
    """
    http_status = _STATUS_BY_ERROR.get(type(exc), status.HTTP_400_BAD_REQUEST)
    return JSONResponse(
        status_code=http_status,
        content=_envelope(exc.code, exc.message, _trace_id(request)),
        # For 401s, the WWW-Authenticate header is part of the OAuth2 bearer
        # spec — it tells clients how to authenticate. Add it only for 401.
        headers=(
            {"WWW-Authenticate": "Bearer"}
            if http_status == status.HTTP_401_UNAUTHORIZED
            else None
        ),
    )


async def validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """
    Re-wrap FastAPI's input-validation errors (raised when a request body/query
    fails a Pydantic constraint) into OUR envelope, so even 422s match the
    house style. We fold the detailed field errors into the message so clients
    still see what went wrong.
    """
    # exc.errors() is a list of per-field problems; summarize them briefly.
    problems = "; ".join(
        f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}"
        for e in exc.errors()
    )
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=_envelope(
            "validation_error",
            problems or "Request validation failed",
            _trace_id(request),
        ),
    )


async def http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    """
    Re-wrap Starlette/FastAPI's built-in HTTPException into our envelope.

    WHY WE NEED THIS: some errors are raised by the framework itself, not by our
    services — e.g. OAuth2PasswordBearer raises HTTPException(401,
    "Not authenticated") when the Authorization header is missing, and unmatched
    routes raise 404. Without this handler those would return FastAPI's default
    {"detail": "..."} shape, breaking our "every error looks the same" contract.
    We derive a machine code from the status so clients still get {code, message,
    trace_id}.
    """
    # Map a few common statuses to stable codes; fall back to "http_error".
    code_by_status = {
        401: "unauthorized",
        403: "permission_denied",
        404: "not_found",
        405: "method_not_allowed",
        429: "rate_limited",
    }
    code = code_by_status.get(exc.status_code, "http_error")
    return JSONResponse(
        status_code=exc.status_code,
        content=_envelope(code, str(exc.detail), _trace_id(request)),
        headers=getattr(exc, "headers", None),
    )


async def unhandled_error_handler(
    request: Request, exc: Exception
) -> JSONResponse:
    """
    Catch-all for anything we didn't anticipate -> 500.

    SECURITY: we LOG the real exception (with stack trace, via exc_info=True) for
    ourselves, but return a GENERIC message to the client. Never leak internal
    details (file paths, SQL, stack traces) to callers — that's an information
    disclosure risk. The trace_id lets us find the full logged error.
    """
    logger.error("Unhandled exception", exc_info=exc)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=_envelope(
            "internal_error",
            "An unexpected error occurred.",
            _trace_id(request),
        ),
    )
