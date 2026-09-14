"""
config.py — CENTRALIZED, TYPE-SAFE APPLICATION CONFIGURATION.

THE PROBLEM THIS SOLVES
-----------------------
Apps need configuration: database URLs, secret keys, feature flags. Bad
approaches you'll see in the wild:
    - Hard-coding values in the source (can't change per environment; leaks secrets).
    - Reading os.environ["X"] scattered everywhere (no validation, crashes late,
      typos become runtime bugs, no single place to see all config).

THE FASTAPI/PYDANTIC WAY
------------------------
Define ONE class describing every setting with its type. pydantic-settings
reads values from environment variables and the .env file, VALIDATES and
CONVERTS them to the declared types, and fails LOUDLY at startup if something
is wrong (e.g. a required secret is missing). One typed object, one source of
truth, validated once.

HOW IT'S CONSUMED
-----------------
Everywhere else we get settings through a dependency (see dependencies.py:
get_settings), which calls the cached factory below. That keeps config
injectable and overridable in tests.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Every attribute here is one configuration value.

    HOW VALUES ARE RESOLVED (highest priority first):
      1. Real environment variables (e.g. set by Docker / the OS / CI).
      2. Values in the .env file.
      3. The default assigned right here in the class.

    The NAME matching is case-insensitive by default, so the env var
    DATABASE_URL fills the `database_url` field below. That mapping is the
    entire "magic" of pydantic-settings.
    """

    # model_config replaces pydantic v1's inner "class Config". It tells
    # pydantic-settings HOW to behave.
    model_config = SettingsConfigDict(
        # Load this file if present. In production you typically don't ship a
        # .env and instead inject real environment variables — both work.
        env_file=".env",
        env_file_encoding="utf-8",
        # If the .env contains a variable we DIDN'T declare below, ignore it
        # instead of raising. Convenient; set to "forbid" if you want strictness.
        extra="ignore",
    )

    # ---- Core app metadata (shown in the OpenAPI docs at /docs) ----
    app_name: str = "Tasks API"
    app_version: str = "0.1.0"
    environment: str = "development"

    # ---- Database ----
    # The full SQLAlchemy connection URL. The "+aiosqlite" / "+asyncpg" suffix
    # picks the async driver. This is the ONLY thing you change to move between
    # SQLite and Postgres.
    database_url: str = "sqlite+aiosqlite:///./app.db"
    # Echo SQL to logs. Note the type is `bool`: pydantic converts the string
    # "false"/"true" (or "0"/"1") from the env into a real Python bool for us.
    db_echo: bool = False

    # ---- Security / JWT ----
    jwt_secret_key: str = "dev-only-insecure-change-me"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60  # pydantic converts "60" -> int 60

    # ---- Redis / caching ----
    redis_url: str = "redis://localhost:6379/0"
    cache_enabled: bool = False

    # ---- Rate limiting ----
    rate_limit: str = "100/minute"

    # WHERE THE RATE-LIMIT COUNTERS LIVE. This one setting decides whether
    # `rate_limit` above means what it says.
    #
    # THE BUG IT FIXES (and it is worth understanding, because the shape of it
    # recurs everywhere):
    #   "memory://" keeps the counters in the process. With ONE process that is
    #   correct. With THREE replicas behind a load balancer there are three
    #   independent counters, so "100/minute" silently becomes 300/minute — and
    #   which counter a request lands on depends on which replica the load
    #   balancer picked. So the effective limit also becomes non-deterministic:
    #   a client can be throttled on one request and allowed on the next.
    #
    #   Nothing errors. Nothing logs. The limit is simply a lie, and it gets
    #   worse every time someone scales up — including when an autoscaler does it
    #   at 3am with nobody watching.
    #
    # THE FIX: point the counters at shared storage, so all replicas increment
    # the same number.
    #   Local / tests:  "memory://"                  (zero dependencies)
    #   Kubernetes:     "redis://redis:6379/1"       (see k8s/10-configmap.yaml)
    #
    # WHY DATABASE 1 AND NOT 0: `redis_url` above uses database 0 for the cache.
    # Redis logical databases are separate keyspaces, so a FLUSHDB while
    # debugging the cache cannot wipe the rate-limit counters, and `KEYS *` in one
    # doesn't drown in the other's keys. Cheap isolation between two unrelated
    # uses of one server.
    #
    # DEFAULT IS memory:// ON PURPOSE: the app must still start and behave
    # sensibly with no Redis at all. Defaults should describe the simplest
    # working configuration, not the production one — otherwise `git clone &&
    # pytest` fails for reasons that have nothing to do with the code.
    rate_limit_storage_uri: str = "memory://"

    # ---- Readiness probe ----
    # How long the /ready endpoint waits for the database before declaring the
    # instance not-ready.
    #
    # ⚠️ THIS MUST BE SMALLER THAN THE PROBE'S OWN timeoutSeconds (3s in
    #    k8s/40-deployment.yaml). The reason is not obvious and it matters:
    #
    #    kubelet giving up after 3s abandons its HTTP request, but it does NOT
    #    stop the work happening inside the app. Our handler would keep waiting on
    #    the database, holding a connection from the pool. With periodSeconds: 5,
    #    a stalled database means a new abandoned probe request every 5 seconds,
    #    each holding a pool connection, until the pool is exhausted — at which
    #    point the health check has caused the outage it was meant to report.
    #
    #    Bounding the wait INSIDE the app means the connection is always released.
    #    General rule: every dependency call in a health check needs its own
    #    timeout, set below the caller's.
    readiness_timeout_seconds: float = 2.0

    # ---- A convenience computed property ----
    @property
    def is_sqlite(self) -> bool:
        """
        True when we're running on SQLite. We need this because SQLite has a
        few quirks the Postgres path doesn't (e.g. it needs
        `check_same_thread=False` and doesn't support real connection pooling).
        Centralizing the check here keeps db.py clean.
        """
        return self.database_url.startswith("sqlite")


@lru_cache
def get_settings() -> Settings:
    """
    Factory that builds the Settings object exactly ONCE and caches it.

    WHY @lru_cache (this is a genuinely important pattern):
      - Reading + validating env/.env has a small cost; we don't want to repeat
        it on every request. lru_cache memoizes: the first call builds Settings,
        every later call returns the SAME cached instance instantly.
      - It also gives us a single shared instance across the whole app.

    WHY A FUNCTION INSTEAD OF A MODULE-LEVEL `settings = Settings()`:
      - Testability. FastAPI can OVERRIDE this function in tests via
        app.dependency_overrides (see tests/conftest.py) to inject a test
        config. A module-level global can't be swapped as cleanly.
      - Import-time safety. A module-level instance would run validation the
        instant the module is imported, which can crash tooling that merely
        imports the app. Deferring to first-call is safer.
    """
    return Settings()
