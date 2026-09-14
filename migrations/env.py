"""
migrations/env.py — Alembic's runtime configuration.

WHAT ALEMBIC IS (and why it matters):
  Your ORM models describe the DESIRED schema. But a live database has an
  EXISTING schema. Alembic bridges them with versioned MIGRATION scripts: small,
  ordered, repeatable files that alter the DB (add a column, create a table)
  step by step. This lets you evolve a production schema safely and reproducibly
  instead of dropping and recreating tables (which would destroy data).

  Typical workflow:
    alembic revision --autogenerate -m "add tasks table"   # generate a script
    alembic upgrade head                                    # apply it
    alembic downgrade -1                                    # roll back one step

WHAT WE CUSTOMIZED IN THIS FILE (vs the generated template):
  1. Point `target_metadata` at OUR models' Base.metadata, so --autogenerate can
     compare "models vs database" and write the diff. Importing src.models
     registers every table (see src/models/__init__.py).
  2. Pull the DB URL from OUR Settings (the .env / env vars), so migrations hit
     the SAME database the app uses — one source of truth, no duplicated URL.
"""

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# --- OUR ADDITIONS: bring in the app's settings and models ---------------------
from src.config import get_settings

# Importing the models package imports every model class, which registers all
# tables onto Base.metadata. Without this import, autogenerate would see an
# empty schema and try to DROP all your tables. This import is load-bearing.
from src.models import Base  # noqa: E402  (import after context for clarity)

# The Alembic Config object provides access to values in alembic.ini.
config = context.config

# Inject our real database URL at runtime, OVERRIDING the placeholder in
# alembic.ini. get_settings() reads .env / environment, so `alembic upgrade`
# targets exactly the DB the app uses. set_main_option needs a plain string.
config.set_main_option("sqlalchemy.url", get_settings().database_url)

# Set up Python logging from the ini file, if configured.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# THIS is what autogenerate compares against the live DB. Our whole schema.
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """
    'Offline' mode: emit SQL to a script instead of executing it, using just the
    URL (no live DB connection needed). Useful for generating a .sql file a DBA
    can review/run. We rarely use this directly but keep it for completeness.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Configure Alembic with a live connection and run the pending migrations."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # compare_type=True: detect column TYPE changes (e.g. String(50)->String(100))
        # during autogenerate, which Alembic skips by default. Catches more drift.
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """
    'Online' async mode: build an async engine and run migrations through it.

    Because our app uses an ASYNC driver (aiosqlite/asyncpg), Alembic must too.
    run_sync bridges Alembic's synchronous migration API onto the async
    connection. NullPool = don't pool connections for this one-shot CLI process.
    """
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    """Entry point for online mode: run the async migrations on a fresh loop."""
    asyncio.run(run_async_migrations())


# Alembic decides offline vs online based on how it was invoked.
if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
