"""
main.py — THE APPLICATION ENTRYPOINT. This file assembles everything:
  - creates the FastAPI app,
  - manages startup/shutdown (the "lifespan"),
  - installs middleware, exception handlers, and rate limiting,
  - includes all routers, including the operational one (/health, /ready).

Run it with:  uvicorn src.main:app --reload
("src.main:app" = "import the `app` object from the src.main module".)

READ TOP-TO-BOTTOM: the order in which we add middleware and handlers matters,
and the comments explain why.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.cache import CacheClient
from src.config import get_settings
from src.db import engine
from src.error_handlers import (
    app_error_handler,
    http_exception_handler,
    unhandled_error_handler,
    validation_error_handler,
)
from src.exceptions import AppError
from src.logging_config import configure_logging
from src.middleware import RequestContextMiddleware
from src.models.base import Base
from src.rate_limit import limiter
from src.routers import auth, comments, projects, system, tasks, users

logger = logging.getLogger("app.main")
settings = get_settings()


# ------------------------------------------------------------------------------
# LIFESPAN — startup & shutdown, the modern replacement for @app.on_event.
# ------------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Code BEFORE the `yield` runs once at STARTUP; code AFTER runs once at
    SHUTDOWN. FastAPI enters this context when the server boots and exits it when
    the server stops (e.g. on SIGTERM from Docker/k8s). This is where we acquire
    and release long-lived resources — the DB engine and the cache client.

    WHY LIFESPAN INSTEAD OF @app.on_event("startup"/"shutdown"):
      on_event is deprecated. lifespan is a single async context manager, so
      setup and matching teardown live together and share scope — harder to
      forget to clean something up.

    GRACEFUL SHUTDOWN:
      The teardown block closes the Redis client and disposes the DB engine's
      connection pool. On SIGTERM, uvicorn stops accepting new requests, lets
      in-flight ones finish, then runs this teardown — so we release
      connections cleanly instead of leaking them. That's what "graceful
      shutdown" means in practice.
    """
    # ---- STARTUP ----
    configure_logging()
    logger.info("Starting %s v%s", settings.app_name, settings.app_version)

    # Build the cache client once and stash it on app.state so the get_cache
    # dependency can hand it to endpoints.
    app.state.cache = CacheClient(
        redis_url=settings.redis_url, enabled=settings.cache_enabled
    )

    # DEV CONVENIENCE: auto-create tables on SQLite so the app runs immediately
    # with no migration step. In production (Postgres) you DON'T do this — you
    # run Alembic migrations instead, which version your schema changes safely.
    # We guard on is_sqlite so this shortcut never runs against a real DB.
    if settings.is_sqlite:
        async with engine.begin() as conn:
            # run_sync bridges to SQLAlchemy's sync metadata API from async code.
            await conn.run_sync(Base.metadata.create_all)
        logger.info("SQLite tables ensured (dev convenience).")

    yield  # ---- the app serves requests here ----

    # ---- SHUTDOWN ----
    logger.info("Shutting down; releasing resources.")
    await app.state.cache.close()  # close Redis pool.
    await engine.dispose()  # close DB connection pool.


# ------------------------------------------------------------------------------
# CREATE THE APP
# ------------------------------------------------------------------------------
app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    lifespan=lifespan,
    description=(
        "A task-tracker API built as a FastAPI deep-dive: async SQLAlchemy, "
        "JWT auth, cursor pagination, caching, and structured logging."
    ),
)

# ---- RATE LIMITING wiring ----
# slowapi needs its limiter attached to app.state and its middleware + error
# handler registered. SlowAPIMiddleware enforces the default_limits on all routes.
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

# ---- OUR REQUEST-CONTEXT MIDDLEWARE ----
# IMPORTANT ORDERING NOTE: Starlette runs middleware in REVERSE order of
# addition for the "incoming" phase — the LAST added wraps OUTERMOST. We add our
# context middleware after SlowAPI so that our request-id is set up around the
# whole request, including any rate-limit rejection, and every log line gets an id.
app.add_middleware(RequestContextMiddleware)

# ---- EXCEPTION HANDLERS ----
# Map exception types to our handlers so every error becomes the standard
# envelope. Registration order isn't significant here (FastAPI matches by type).
#   - AppError (and all subclasses) -> our domain handler (404/403/409/401/400).
#   - RequestValidationError -> re-wrapped 422 in our envelope.
#   - Exception (catch-all) -> generic 500 without leaking internals.
app.add_exception_handler(AppError, app_error_handler)
app.add_exception_handler(RequestValidationError, validation_error_handler)
# Framework-raised HTTPExceptions (missing auth header, unknown route, 405, ...)
# get re-wrapped into our envelope too, so EVERY error the API emits is uniform.
app.add_exception_handler(StarletteHTTPException, http_exception_handler)
app.add_exception_handler(Exception, unhandled_error_handler)

# ---- ROUTERS ----
# include_router mounts each mini-app. Their prefixes/tags were set in the router
# files, so this stays clean. Order affects only the /docs listing, not behavior.
app.include_router(auth.router)
app.include_router(users.router)
app.include_router(projects.router)
app.include_router(tasks.router)
app.include_router(comments.router)

# ---- OPERATIONAL ENDPOINTS: /health (liveness) and /ready (readiness) ----
#
# WHY THESE MOVED OUT OF THIS FILE INTO src/routers/system.py:
#   They used to be a single /health defined inline here, which was fine while
#   there was one of them and it checked nothing. Once readiness became a real
#   endpoint with dependency checks, timeouts, and a documented fatal/non-fatal
#   policy per dependency, it stopped being a footnote to app assembly and became
#   its own concern — with reasoning that deserves to be readable in one place.
#
#   The general rule this follows: main.py should ASSEMBLE the application, not
#   IMPLEMENT parts of it. The moment something in an entrypoint file grows a
#   decision worth explaining, it belongs in its own module. That also makes it
#   discoverable — an operator looking for "how does this app report health" finds
#   system.py, rather than scrolling to the bottom of main.py.
app.include_router(system.router)
