"""Integration test: ``alembic upgrade head`` against a real Postgres.

Spins up a Postgres container with testcontainers, runs the migration
chain from scratch, and asserts that every reserved table exists.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.postgres import PostgresContainer

REPO_ROOT = Path(__file__).resolve().parents[3]
EXPECTED_TABLES = {
    "metrics_hourly",
    "metrics_daily",
    "authz_index",
    "policy_decisions_audit",
    "job_state",
    "profile_applied",
}


def _async_dsn(container: PostgresContainer) -> str:
    raw = container.get_connection_url()
    return raw.replace("postgresql+psycopg2://", "postgresql+asyncpg://").replace(
        "postgresql://", "postgresql+asyncpg://"
    )


async def _public_table_names(dsn: str) -> set[str]:
    engine = create_async_engine(dsn)
    try:
        async with engine.connect() as conn:

            def _inspect(sync_conn: sa.Connection) -> list[str]:
                return sa.inspect(sync_conn).get_table_names()

            names = await conn.run_sync(_inspect)
    finally:
        await engine.dispose()
    return set(names)


@pytest.fixture
def postgres_container() -> Iterator[PostgresContainer]:
    with PostgresContainer("postgres:16-alpine") as container:
        yield container


@pytest.fixture
def alembic_config(postgres_container: PostgresContainer) -> Iterator[Config]:
    """Build an Alembic Config pointed at the live Postgres container.

    ``env.py`` reads the DSN from :func:`fdp.config.get_settings`, so we
    override the env var and clear the LRU cache around the test.
    """
    from fdp.config import get_settings

    async_dsn = _async_dsn(postgres_container)
    original = os.environ.get("POSTGRES_DSN")
    os.environ["POSTGRES_DSN"] = async_dsn
    get_settings.cache_clear()

    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))

    try:
        yield config
    finally:
        if original is None:
            os.environ.pop("POSTGRES_DSN", None)
        else:
            os.environ["POSTGRES_DSN"] = original
        get_settings.cache_clear()


@pytest.mark.integration
def test_upgrade_head_creates_all_reserved_tables(
    postgres_container: PostgresContainer,
    alembic_config: Config,
) -> None:
    command.upgrade(alembic_config, "head")
    tables = asyncio.run(_public_table_names(_async_dsn(postgres_container)))
    missing = EXPECTED_TABLES - tables
    assert not missing, f"Migration did not create: {sorted(missing)}"


@pytest.mark.integration
def test_downgrade_base_removes_all_reserved_tables(
    postgres_container: PostgresContainer,
    alembic_config: Config,
) -> None:
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "base")
    tables = asyncio.run(_public_table_names(_async_dsn(postgres_container)))
    leftover = EXPECTED_TABLES & tables
    assert not leftover, f"Downgrade left tables behind: {sorted(leftover)}"
