"""
common.py — schemas shared across endpoints: pagination and the error envelope.
"""

from typing import Generic, TypeVar

from pydantic import BaseModel, Field

# A TypeVar lets us build a GENERIC container: Page[TaskOut], Page[ProjectOut],
# etc., all from one definition. "T" is a placeholder for "whatever item type".
T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    """
    A generic paginated response envelope.

    WHY PAGINATE AT ALL:
      Never return "all rows". A table can grow to millions; sending them all
      would exhaust memory and time out. Pagination returns a bounded slice.

    WHY CURSOR/KEYSET STYLE (we expose next_cursor):
      Two common strategies:
        - OFFSET/LIMIT ("page 5, size 20" -> OFFSET 80): simple, but slow on
          deep pages (the DB still scans and discards the skipped rows) and can
          skip/duplicate rows if data changes between requests.
        - CURSOR/KEYSET ("give me items after this id"): the server returns an
          opaque `next_cursor`; the client sends it back to get the next slice.
          Fast at any depth and stable under inserts. This is what large APIs
          (Stripe, GitHub, Slack) use. We implement it in the tasks service.

    `items: list[T]` — the generic payload. When you write Page[TaskOut],
    FastAPI generates a concrete schema whose items are TaskOut, and documents
    it correctly in OpenAPI.
    """

    items: list[T]
    # The cursor to pass as ?cursor=... to fetch the next page. None => last page.
    next_cursor: str | None = Field(
        default=None,
        description="Opaque cursor for the next page; null when there are no more items.",
    )
    # How many items this page contains (convenience for clients/UIs).
    count: int = Field(description="Number of items in this page.")


class ErrorDetail(BaseModel):
    """The inner object of our consistent error envelope."""

    # A stable, machine-readable code clients can branch on (e.g. "not_found").
    # Unlike the human message, this shouldn't change wording over time.
    code: str
    # A human-readable explanation.
    message: str
    # The request's trace id, so a user reporting an error can quote it and we
    # can find the exact request in the logs. Wired up by the request-id
    # middleware + exception handlers.
    trace_id: str | None = None


class ErrorResponse(BaseModel):
    """
    The OUTER error envelope: {"error": {...}}.

    WHY A CONSISTENT ENVELOPE:
      Clients should be able to handle every error the same way. Rather than
      FastAPI's default {"detail": "..."} for some errors and different shapes
      for others, we normalize ALL errors to {"error": {code, message,
      trace_id}} via custom exception handlers. Predictable = easy to consume.
    """

    error: ErrorDetail
