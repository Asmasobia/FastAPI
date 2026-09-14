"""
middleware.py — a custom ASGI MIDDLEWARE that assigns a request id, times each
request, and logs it.

WHAT IS MIDDLEWARE?
  Code that wraps EVERY request/response. It runs before your endpoint (on the
  way in) and after it (on the way out). Think of it as an onion layer around
  the whole app — perfect for cross-cutting concerns that shouldn't be
  copy-pasted into every endpoint: logging, timing, auth headers, CORS,
  compression, request ids.

REQUEST FLOW WITH THIS MIDDLEWARE:
  incoming request
    -> middleware: generate/adopt a request id, start a timer
      -> (inner middleware / the route handler runs)
    <- middleware: measure elapsed time, attach headers, log a summary
  outgoing response

We use BaseHTTPMiddleware (Starlette's convenient base — FastAPI is built on
Starlette). You implement dispatch(request, call_next); `call_next` runs the
rest of the stack and gives you the response to post-process.
"""

import logging
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from src.logging_config import request_id_ctx

logger = logging.getLogger("app.request")

# The header name we read/emit the id under. X- prefix is the conventional
# (if informal) marker for a non-standard header.
REQUEST_ID_HEADER = "X-Request-ID"


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # ---- ON THE WAY IN ----
        # Reuse the client's request id if they sent one (lets a caller trace a
        # request across multiple services — DISTRIBUTED TRACING in miniature);
        # otherwise generate a fresh one.
        incoming = request.headers.get(REQUEST_ID_HEADER)
        request_id = incoming or str(uuid.uuid4())

        # Store it in the ContextVar so EVERY log line emitted during this
        # request (anywhere in the code, including deep in services) is stamped
        # with this id automatically — no need to thread it through function args.
        # set() returns a token we use to reset the value afterward, keeping the
        # context clean for whatever the event loop runs next.
        token = request_id_ctx.set(request_id)

        # Also stash it on request.state so exception handlers can read it to put
        # into the error envelope's trace_id field.
        request.state.request_id = request_id

        start = time.perf_counter()  # monotonic clock — correct for timing.
        try:
            # ---- RUN THE REST OF THE STACK (inner middleware + the endpoint) ----
            response: Response = await call_next(request)
        finally:
            # Reset the ContextVar even if the endpoint raised, so the id never
            # leaks into an unrelated request handled next on this task.
            request_id_ctx.reset(token)

        # ---- ON THE WAY OUT ----
        elapsed_ms = (time.perf_counter() - start) * 1000

        # Echo the id back so the client can log/correlate it too.
        response.headers[REQUEST_ID_HEADER] = request_id
        # Expose the server-measured duration — handy for quick debugging.
        response.headers["X-Process-Time-ms"] = f"{elapsed_ms:.2f}"

        # One structured summary line per request. Because RequestIdFilter is
        # installed, this line already carries request_id too.
        logger.info(
            "%s %s -> %s (%.2f ms)",
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
        )
        return response
