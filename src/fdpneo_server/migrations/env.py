"""Alembic environment for the fdp Postgres schema.

Reads the DSN from the same ``POSTGRES_DSN`` env var / ``.env`` entry as
:class:`~fdpneo_server.config.Settings.postgres_dsn`, so the same config
drives the running server and the migration tool — but loads *only* the
DSN, so ``fdp db migrate`` works in environments where the rest of the
configuration (triple store, OIDC) is absent. The async driver is required
at runtime; Alembic runs it through an async engine here.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig
from typing import TYPE_CHECKING

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from fdpneo_server.config import MigrationSettings
from fdpneo_server.storage.postgres.models import Base, register_all_models

if TYPE_CHECKING:
    pass

# Alembic Config object provides access to .ini file values.
config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Inject the DSN at runtime so neither alembic.ini nor env-supplied URL is required.
config.set_main_option("sqlalchemy.url", str(MigrationSettings().postgres_dsn))  # type: ignore[call-arg]  # pydantic-settings fills from env

# Populate Base.metadata with every ORM model before reading it for migrations.
register_all_models()
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Render SQL to stdout without connecting (alembic upgrade --sql)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Open an async connection and run migrations through it."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
