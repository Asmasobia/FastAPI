"""
db.py — THE ASYNC DATABASE LAYER (engine + session factory + FastAPI dependency).

MENTAL MODEL — three objects, three jobs:
  1. ENGINE   : the connection pool / "phone line" to the database. Created ONCE
                for the whole app. Expensive to build, cheap to reuse.
  2. SESSION  : your unit of work for ONE request — a workspace that tracks the
                objects you load/change and flushes them as SQL. Created PER
                REQUEST, then closed.
  3. get_db() : a FastAPI dependency that hands each request a fresh session and
                guarantees it's cleaned up afterward (even if the request errors).

WHY ASYNC?
  A web server spends most of its time WAITING on the database and network. With
  sync code, one request waiting on the DB blocks the whole worker. With async,
  while request A awaits its query, the event loop serves request B. Same
  hardware, far more concurrency. FastAPI is async-first, so we use SQLAlchemy's
  async engine to keep the whole request path non-blocking.

THE ONE RULE THAT TRIPS EVERYONE UP:
  Inside an `async def` you must `await` DB calls and never call BLOCKING
  (synchronous) I/O — a blocking call freezes the entire event loop and kills
  the concurrency benefit. That's why we use async drivers (aiosqlite/asyncpg)
  and the async session.
"""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.config import get_settings

# Grab the (cached) settings once at import time to configure the engine.
settings = get_settings()


# ------------------------------------------------------------------------------
# 1) THE ENGINE
# ------------------------------------------------------------------------------
# SQLite needs a special flag; Postgres/others don't. We branch on that.
#
# check_same_thread=False:
#   SQLite by default forbids using a connection across threads. SQLAlchemy's
#   async layer may touch the connection from a helper thread, so we must
#   disable that check for SQLite specifically. (Harmless here; unsafe only if
#   you shared one raw connection across threads yourself — the session doesn't.)
_connect_args = {"check_same_thread": False} if settings.is_sqlite else {}

engine = create_async_engine(
    settings.database_url,
    # echo=True prints every SQL statement — invaluable while learning.
    echo=settings.db_echo,
    connect_args=_connect_args,
    # pool_pre_ping: before handing out a pooled connection, send a tiny
    # "SELECT 1" to check it's still alive. Prevents the classic "server closed
    # the connection unexpectedly" error when a DB connection went stale (e.g.
    # after an idle period). Cheap insurance; standard in production configs.
    pool_pre_ping=True,
)


# ------------------------------------------------------------------------------
# 2) THE SESSION FACTORY
# ------------------------------------------------------------------------------
# async_sessionmaker is a FACTORY: calling it (or entering it as a context
# manager) produces a fresh AsyncSession bound to our engine.
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    # expire_on_commit=False — THE most important async-session setting.
    #
    #   By default, after session.commit() SQLAlchemy EXPIRES every object it
    #   loaded, meaning the next time you read obj.some_attribute it silently
    #   re-fetches from the DB (a "lazy load"). In ASYNC code a lazy load is a
    #   hidden blocking I/O that raises errors like "greenlet_spawn has not been
    #   called" — because attribute access isn't awaitable.
    #
    #   Concretely: we create a task, commit, then return it in the HTTP
    #   response. Serializing it reads task.id, task.title, etc. With the
    #   default we'd trigger a lazy reload mid-response and crash. Setting
    #   expire_on_commit=False keeps the already-loaded values usable after
    #   commit. Remember this — it's a guaranteed interview question.
    expire_on_commit=False,
)


# ------------------------------------------------------------------------------
# 3) THE FASTAPI DEPENDENCY
# ------------------------------------------------------------------------------
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    Yield a database session to a request, then guarantee cleanup.

    HOW FASTAPI USES THIS (the "yield dependency" pattern):
      An endpoint declares `session: AsyncSession = Depends(get_db)`. FastAPI:
        1. Calls get_db(), runs code up to the `yield`, and injects the yielded
           session into the endpoint.
        2. Runs the endpoint.
        3. Comes BACK here and runs everything AFTER the yield — the teardown —
           no matter what (success OR exception). This is why we use a
           generator with try/finally: it's a reliable "setup / use / teardown"
           bracket around the request.

    WHY `async with AsyncSessionLocal()`:
      Entering the session as a context manager guarantees the connection is
      returned to the pool and the session is closed on exit. We DON'T blanket
      commit here; commits belong in the service layer where the business
      operation knows it succeeded. We DO roll back on error so a half-finished
      transaction never lingers.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            # If the endpoint raised, undo any pending changes so we never leave
            # a dangling open transaction on the connection.
            await session.rollback()
            raise  # re-raise so FastAPI's error handling still runs.
        # No explicit close needed: `async with` closes the session for us.
