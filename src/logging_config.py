"""
logging_config.py — STRUCTURED, TRACE-AWARE logging.

WHY NOT JUST print()?
  print() gives you unstructured, timestamp-less lines with no request context.
  Real services need logs you can FILTER and CORRELATE: "show me everything that
  happened during request abc-123". We achieve that with:
    - a ContextVar holding the current request's id, and
    - a logging Filter that stamps that id onto every log record automatically.

WHAT'S A ContextVar (and why not a global variable)?
  contextvars.ContextVar stores a value that is ISOLATED PER ASYNC TASK. In an
  async server many requests run concurrently on one thread; a plain global
  would be shared and clobbered between them. A ContextVar gives each request
  its own value, so request A's id never bleeds into request B's logs. This is
  the correct primitive for "current request" state in async Python — and a
  great thing to be able to explain in an interview.
"""

import logging
import sys
from contextvars import ContextVar

# The per-request id. Default "-" means "no request in context" (e.g. logs
# emitted at startup before any request). The middleware sets this at the start
# of each request; the log filter reads it.
request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")


class RequestIdFilter(logging.Filter):
    """
    A logging Filter that injects the current request id onto every record, so
    our format string can print it. Filters run for each log record; returning
    True keeps the record (we never drop records, we just enrich them).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # Attach the current request id (from the ContextVar) to the record so
        # the formatter can reference %(request_id)s below.
        record.request_id = request_id_ctx.get()
        return True


def configure_logging() -> None:
    """
    Set up the root logger once at startup (called from main.py's lifespan).

    Format includes the request_id, so a single grep for an id reconstructs the
    whole request. In real production you'd often emit JSON logs (one JSON
    object per line) so a log aggregator can index the fields; here we use a
    readable key=value-ish line and keep the trace id front and center.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(RequestIdFilter())
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)-8s | request_id=%(request_id)s | "
            "%(name)s | %(message)s"
        )
    )

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # Clear any pre-existing handlers (uvicorn may add its own) so we don't get
    # duplicate log lines.
    root.handlers.clear()
    root.addHandler(handler)
