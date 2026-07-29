"""Integration test for :class:`CacheRepository` against a real Postgres.

Runs the full Alembic migration chain (0001 reserves the table, 0002
adds the columns and indexes) and exercises every repository method.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from importlib.resources import files

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from testcontainers.postgres import PostgresContainer

from fdpneo_server.policy.cache import CacheRepository


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
    """Apply ``alembic upgrade head`` against the container."""
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
async def session(migrated: PostgresContainer) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(_async_dsn(migrated))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


@pytest.fixture
def repo(session: AsyncSession) -> CacheRepository:
    return CacheRepository(session)


SUBJ_A = "https://idp.example/realms/fdp#alice#0123456789abcdef"
SUBJ_B = "https://idp.example/realms/fdp#bob#fedcba9876543210"
R1 = "https://example.org/records/1"
R2 = "https://example.org/records/2"


@pytest.mark.integration
async def test_upsert_then_lookup(repo: CacheRepository, session: AsyncSession) -> None:
    await repo.upsert(
        subject_key=SUBJ_A,
        action="read",
        graph_uri=R1,
        decision="permit",
        policy_version="https://example.org/offer/1",
    )
    await session.commit()

    row = await repo.lookup(subject_key=SUBJ_A, action="read", graph_uri=R1)
    assert row is not None
    assert row.decision == "permit"
    assert row.policy_version == "https://example.org/offer/1"
    assert row.computed_at is not None


@pytest.mark.integration
async def test_upsert_is_idempotent_on_composite_key(
    repo: CacheRepository, session: AsyncSession
) -> None:
    await repo.upsert(
        subject_key=SUBJ_A,
        action="read",
        graph_uri=R1,
        decision="permit",
        policy_version="https://example.org/offer/1",
    )
    await repo.upsert(
        subject_key=SUBJ_A,
        action="read",
        graph_uri=R1,
        decision="deny",
        policy_version="https://example.org/offer/2",
    )
    await session.commit()

    row = await repo.lookup(subject_key=SUBJ_A, action="read", graph_uri=R1)
    assert row is not None
    assert row.decision == "deny"
    assert row.policy_version == "https://example.org/offer/2"


@pytest.mark.integration
async def test_authorized_resources_returns_only_permits(
    repo: CacheRepository, session: AsyncSession
) -> None:
    await repo.upsert(
        subject_key=SUBJ_A, action="read", graph_uri=R1, decision="permit", policy_version=None
    )
    await repo.upsert(
        subject_key=SUBJ_A, action="read", graph_uri=R2, decision="deny", policy_version=None
    )
    await repo.upsert(
        subject_key=SUBJ_B, action="read", graph_uri=R1, decision="permit", policy_version=None
    )
    await session.commit()

    permitted = await repo.authorized_resources(subject_key=SUBJ_A, action="read")
    assert permitted == {R1}


@pytest.mark.integration
async def test_invalidate_by_resource(repo: CacheRepository, session: AsyncSession) -> None:
    await repo.upsert(
        subject_key=SUBJ_A, action="read", graph_uri=R1, decision="permit", policy_version=None
    )
    await repo.upsert(
        subject_key=SUBJ_B, action="read", graph_uri=R1, decision="permit", policy_version=None
    )
    await repo.upsert(
        subject_key=SUBJ_A, action="read", graph_uri=R2, decision="permit", policy_version=None
    )
    await session.commit()

    dropped = await repo.invalidate_by_resource(R1)
    await session.commit()
    assert dropped == 2

    assert await repo.lookup(subject_key=SUBJ_A, action="read", graph_uri=R1) is None
    assert await repo.lookup(subject_key=SUBJ_A, action="read", graph_uri=R2) is not None


@pytest.mark.integration
async def test_invalidate_by_subject(repo: CacheRepository, session: AsyncSession) -> None:
    await repo.upsert(
        subject_key=SUBJ_A, action="read", graph_uri=R1, decision="permit", policy_version=None
    )
    await repo.upsert(
        subject_key=SUBJ_A, action="modify", graph_uri=R2, decision="deny", policy_version=None
    )
    await repo.upsert(
        subject_key=SUBJ_B, action="read", graph_uri=R1, decision="permit", policy_version=None
    )
    await session.commit()

    dropped = await repo.invalidate_by_subject(SUBJ_A)
    await session.commit()
    assert dropped == 2
    assert await repo.lookup(subject_key=SUBJ_B, action="read", graph_uri=R1) is not None


@pytest.mark.integration
async def test_invalidate_many_resources_empty_is_noop(
    repo: CacheRepository, session: AsyncSession
) -> None:
    await repo.upsert(
        subject_key=SUBJ_A, action="read", graph_uri=R1, decision="permit", policy_version=None
    )
    await session.commit()

    dropped = await repo.invalidate_many_resources([])
    assert dropped == 0
    assert await repo.lookup(subject_key=SUBJ_A, action="read", graph_uri=R1) is not None
