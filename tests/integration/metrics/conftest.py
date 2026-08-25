"""Shared fixtures for the metrics integration tests.

Spins up a Postgres container, runs the Alembic migration chain (which
includes 0003 that creates the metrics tables), and yields a session
factory plus a :class:`MetricsRepository` bound to it.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from importlib.resources import files

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from testcontainers.community.postgres import PostgresContainer

from fdpneo_server.metrics.repository import MetricsRepository


def _async_dsn(container: PostgresContainer) -> str:
    raw = container.get_connection_url()
    return raw.replace("postgresql+psycopg2://", "postgresql+asyncpg://").replace(
        "postgresql://", "postgresql+asyncpg://"
    )


@pytest.fixture
def postgres_container() -> Iterator[PostgresContainer]:
    with PostgresContainer("postgres:16-alpine") as container:
        yield container


@pytest.fixture
def migrated(postgres_container: PostgresContainer) -> Iterator[PostgresContainer]:
    """Apply ``alembic upgrade head`` against the live container."""
    from fdpneo_server.config import get_settings

    async_dsn = _async_dsn(postgres_container)
    original = os.environ.get("POSTGRES_DSN")
    os.environ["POSTGRES_DSN"] = async_dsn
    get_settings.cache_clear()

    config = Config(str(files("fdpneo_server") / "alembic.ini"))
    try:
        command.upgrade(config, "head")
        yield postgres_container
    finally:
        if original is None:
            os.environ.pop("POSTGRES_DSN", None)
        else:
            os.environ["POSTGRES_DSN"] = original
        get_settings.cache_clear()


@pytest.fixture
async def session_factory(
    migrated: PostgresContainer,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(_async_dsn(migrated))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
async def session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with session_factory() as s:
        yield s


@pytest.fixture
def repo(session: AsyncSession) -> MetricsRepository:
    return MetricsRepository(session)
