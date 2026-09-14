"""
conftest.py — SHARED PYTEST FIXTURES. This is the backbone of the test suite.

pytest automatically discovers a file named conftest.py and makes every
"fixture" defined here available to all tests WITHOUT importing it. A FIXTURE is
a reusable piece of setup (and teardown) that a test receives just by naming it
as a parameter. Think dependency injection, but for tests.

THE THREE HARD PROBLEMS THIS FILE SOLVES (and the interview gold in each):

  1. A SEPARATE TEST DATABASE.
     Tests must NEVER touch your real/dev database. We spin up a brand-new
     in-memory SQLite DB per test, create the schema, and throw it away after.
     Fast and perfectly isolated — one test can't corrupt another's data.

  2. DRIVING THE APP WITHOUT A NETWORK.
     We use httpx.AsyncClient with ASGITransport(app=app). That sends requests
     straight into the FastAPI app object IN-PROCESS — no real socket, no
     uvicorn, no port. Fast and deterministic.

  3. SWAPPING REAL DEPENDENCIES FOR TEST ONES (dependency_overrides).
     The app's get_db yields the real session. In tests we OVERRIDE it to yield
     a session bound to the test DB. FastAPI's app.dependency_overrides is a
     dict {real_dependency: fake_dependency}; this is THE reason we put get_db,
     get_settings, etc. behind Depends() — so they're swappable here. We also
     override the cache with a no-op so tests need no Redis.
"""

from collections.abc import AsyncGenerator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.db import get_db
from src.dependencies import get_cache
from src.main import app
from src.models.base import Base

# An IN-MEMORY SQLite database. "sqlite+aiosqlite:///:memory:" lives entirely in
# RAM and vanishes when the connection closes — ideal for tests.
#
# StaticPool + a single shared connection: normally each connection to
# ":memory:" gets its OWN separate database. That would break us, because the
# fixture creates tables on one connection and the request would use another
# (empty) one. StaticPool forces every checkout to reuse the SAME underlying
# connection, so the schema and data are visible across the whole test.
from sqlalchemy.pool import StaticPool

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture
async def db_engine():
    """
    Create a fresh in-memory engine, build the schema, yield it, then dispose.

    Scope is the default (function): a NEW database per test function. That's
    the strongest isolation — no test can see another's rows. (If the suite
    grows slow you might widen the scope, but isolation-by-default is the right
    starting point.)
    """
    engine = create_async_engine(
        TEST_DATABASE_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,  # share one connection (see note above).
    )
    # Create all tables from the ORM metadata. In tests we use create_all rather
    # than running Alembic migrations: it's faster and tests the CURRENT models
    # directly. (Some teams run migrations in one CI job to test the migrations
    # themselves — a good thing to mention in an interview.)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield engine

    # Teardown: drop everything and dispose the pool so nothing leaks.
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine) -> AsyncGenerator[AsyncSession, None]:
    """
    Yield an AsyncSession bound to the test engine. Mirrors production settings
    (expire_on_commit=False) so behavior matches. Tests that need direct DB
    access (e.g. to seed data) can use this.
    """
    TestSessionLocal = async_sessionmaker(
        bind=db_engine, class_=AsyncSession, expire_on_commit=False
    )
    async with TestSessionLocal() as session:
        yield session


class _NoopCache:
    """
    A stand-in cache that always misses and stores nothing. Lets tests run with
    ZERO Redis dependency while exercising the exact same code paths (the cache
    calls simply become no-ops / misses). Matches CacheClient's async interface.

    ⚠️ THE COST OF A HAND-WRITTEN TEST DOUBLE, which this class demonstrates:
       it must be kept in step with the real class by hand. Adding `enabled` and
       `ping()` to CacheClient (for /ready) meant adding them HERE too, or the
       readiness endpoint would raise AttributeError in tests while working
       perfectly in production — a test failure that looks like a bug in the code
       under test.

       The alternative is unittest.mock's autospec, which derives the double from
       the real class and fails loudly when they diverge. The tradeoff is
       readability: this class is obvious at a glance, a mock is not. For an
       interface this small and stable, explicit wins — but know that you have
       taken on the maintenance, and that this is exactly where duplicated
       interfaces drift.
    """

    # Reports "caching is switched off" — so /ready records cache: "disabled",
    # which is a healthy state, not a failure. See src/routers/system.py.
    enabled = False

    async def ping(self) -> bool:
        return False

    async def get(self, key: str):
        return None

    async def set(self, key: str, value: str, ttl_seconds: int):
        return None

    async def delete(self, key: str):
        return None

    async def close(self):
        return None


@pytest_asyncio.fixture
async def client(db_engine) -> AsyncGenerator[AsyncClient, None]:
    """
    THE MAIN FIXTURE most tests use: an AsyncClient wired to the app, with the
    DB and cache dependencies overridden to point at test doubles.

    Step by step:
      1. Build a session factory on the TEST engine.
      2. Define override_get_db that yields sessions from it — same yield-based
         shape as the real get_db, so FastAPI treats it identically.
      3. Register overrides in app.dependency_overrides. Now anywhere the app
         asks for get_db it gets the TEST db; anywhere it asks for get_cache it
         gets the no-op cache.
      4. Enter the app's lifespan so startup/shutdown logic runs (this also sets
         app.state.cache; our override means endpoints won't use it, but keeping
         the lifespan real keeps the test faithful).
      5. Yield an AsyncClient using ASGITransport(app=app) — in-process, no network.
      6. Clean up overrides afterward so tests don't leak into each other.
    """
    TestSessionLocal = async_sessionmaker(
        bind=db_engine, class_=AsyncSession, expire_on_commit=False
    )

    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        async with TestSessionLocal() as session:
            yield session

    def override_get_cache():
        return _NoopCache()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_cache] = override_get_cache

    transport = ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as ac:
            yield ac

    # IMPORTANT cleanup: clear overrides so the next test starts clean.
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def auth_client(client: AsyncClient) -> AsyncClient:
    """
    A client that is ALREADY logged in. It registers a user, logs in, and sets
    the Authorization header on the client for all subsequent requests.

    WHY THIS FIXTURE EARNS ITS KEEP:
      Almost every endpoint requires auth. Without this, every test would repeat
      the register+login dance. This fixture does it once and hands back a
      ready-to-use authenticated client — tests read cleanly and focus on the
      behavior under test, not the login boilerplate.
    """
    # Register.
    await client.post(
        "/auth/register",
        json={
            "email": "tester@example.com",
            "full_name": "Test User",
            "password": "supersecret123",
        },
    )
    # Log in (form-encoded, per the OAuth2 password flow).
    resp = await client.post(
        "/auth/login",
        data={"username": "tester@example.com", "password": "supersecret123"},
    )
    token = resp.json()["access_token"]
    # Attach the bearer token to EVERY request this client makes from now on.
    client.headers["Authorization"] = f"Bearer {token}"
    return client
