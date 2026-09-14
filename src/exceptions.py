"""
exceptions.py — the app's OWN exception types.

WHY CUSTOM EXCEPTIONS INSTEAD OF RAISING HTTPException EVERYWHERE:
  The SERVICE layer (business logic) should not know about HTTP. If a service
  raises fastapi.HTTPException, it's coupled to the web framework and can't be
  reused (e.g. from a CLI or a background job). Instead, services raise these
  PLAIN domain errors ("this project doesn't exist", "you don't own this"), and
  a single set of exception handlers (registered in main.py) translates them
  into HTTP responses with our standard error envelope.

  So the flow is:
    service raises NotFoundError  ->  handler turns it into 404 + {"error": {...}}

  This keeps layers decoupled AND gives every error a consistent shape + code.

Each error carries a stable `code` (machine-readable, for clients) and a
`message` (human-readable). The matching HTTP status lives in the handler.
"""


class AppError(Exception):
    """
    Base class for all our domain errors. `code` and `message` are the two
    pieces our error envelope needs. Subclasses set a sensible default code.
    """

    # Subclasses override this default machine code.
    code: str = "error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code


class NotFoundError(AppError):
    """Raised when a requested resource doesn't exist. -> HTTP 404."""

    code = "not_found"


class PermissionDeniedError(AppError):
    """
    Raised when the user is authenticated but not allowed to touch this
    resource (e.g. editing someone else's project). -> HTTP 403.
    """

    code = "permission_denied"


class ConflictError(AppError):
    """
    Raised on a state conflict, e.g. registering an email that already exists.
    -> HTTP 409.
    """

    code = "conflict"


class AuthError(AppError):
    """
    Raised for authentication failures (bad credentials, invalid/expired token).
    -> HTTP 401.
    """

    code = "unauthorized"
