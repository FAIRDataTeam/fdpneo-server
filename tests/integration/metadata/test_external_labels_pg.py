"""Postgres-backed external label cache + resolver persistence (Phase 21.6).

Exercises :class:`ExternalLabelCache` and the resolver's external path against a
real Postgres (testcontainers + the ``0009_external_labels`` migration), which
the SQLite unit suite can't validate: composite-PK upsert semantics, expiry
filtering, and — the point of persisting at all — labels surviving a fresh
:class:`LabelResolver` (i.e. a process restart) without re-fetching.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from testcontainers.postgres import PostgresContainer

from fdp.config import RemoteLabelSettings, get_settings
from fdp.metadata.external_labels import ExternalLabelCache
from fdp.metadata.labels import LabelResolver

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]

DOI = "https://doi.example/10.1/x"


def _async_dsn(container: PostgresContainer) -> str:
    raw = container.get_connection_url()
    return raw.replace("postgresql+psycopg2://", "postgresql+asyncpg://").replace(
        "postgresql://", "postgresql+asyncpg://"
    )


def _settings(**over: object) -> RemoteLabelSettings:
    over.setdefault("enabled", True)
    over.setdefault("allowed_hosts", "doi.example")
    return RemoteLabelSettings(_env_file=None, **over)  # type: ignore[arg-type]


class _EmptyAdapter:
    async def query(self, sparql: str, *, accept: str = "") -> bytes:
        del sparql, accept
        return b'{"results": {"bindings": []}}'


class _FakeFetcher:
    def __init__(self, labels: dict[str, str | None]) -> None:
        self._labels = labels
        self.calls: list[str] = []

    async def fetch(self, iri: str, *, language: str) -> str | None:
        del language
        self.calls.append(iri)
        return self._labels.get(iri)


@pytest.fixture
def pg_dsn() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine") as container:
        dsn = _async_dsn(container)
        saved = os.environ.get("POSTGRES_DSN")
        os.environ["POSTGRES_DSN"] = dsn
        get_settings.cache_clear()
        config = Config(str(REPO_ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
        command.upgrade(config, "head")
        try:
            yield dsn
        finally:
            if saved is None:
                os.environ.pop("POSTGRES_DSN", None)
            else:
                os.environ["POSTGRES_DSN"] = saved
            get_settings.cache_clear()


@pytest.fixture
async def session_factory(pg_dsn: str) -> Any:
    engine = create_async_engine(pg_dsn, future=True)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def test_cache_roundtrip_and_expiry_on_postgres(session_factory: Any) -> None:
    cache = ExternalLabelCache(session_factory=session_factory)
    await cache.upsert(DOI, "en", "The Work", ttl_seconds=3600, source_host="doi.example")
    assert await cache.get_many([DOI], language="en") == {DOI: "The Work"}

    # Composite PK: same IRI, different language is a distinct row.
    await cache.upsert(DOI, "nl", "Het Werk", ttl_seconds=3600)
    assert await cache.get_many([DOI], language="nl") == {DOI: "Het Werk"}

    # Upsert replaces in place (on_conflict / merge on the composite key).
    await cache.upsert(DOI, "en", "Renamed", ttl_seconds=3600)
    assert await cache.get_many([DOI], language="en") == {DOI: "Renamed"}

    # An already-expired row is filtered out and purgeable.
    await cache.upsert("https://doi.example/stale", "en", "Old", ttl_seconds=-1)
    assert await cache.get_many(["https://doi.example/stale"], language="en") == {}
    assert await cache.purge_expired() >= 1


async def test_negative_result_persists_on_postgres(session_factory: Any) -> None:
    cache = ExternalLabelCache(session_factory=session_factory)
    await cache.upsert(DOI, "en", None, ttl_seconds=3600)
    got = await cache.get_many([DOI], language="en")
    assert DOI in got and got[DOI] is None


async def test_label_survives_a_fresh_resolver(session_factory: Any) -> None:
    """A label warmed by one resolver is served by a fresh one (restart) from PG."""
    cache = ExternalLabelCache(session_factory=session_factory)
    settings = _settings()

    # First resolver warms the durable cache lazily.
    warm = LabelResolver(
        adapter=_EmptyAdapter(),  # type: ignore[arg-type]
        external_cache=cache,
        external_fetcher=_FakeFetcher({DOI: "Persisted Work"}),  # type: ignore[arg-type]
        remote_settings=settings,
    )
    assert await warm.lookup([DOI], language="en") == {}  # lazy — omitted
    while warm._bg_tasks:
        await asyncio.gather(*list(warm._bg_tasks))

    # A brand-new resolver (cold in-memory) must resolve from Postgres alone —
    # its fetcher would raise if consulted, proving no re-fetch.
    cold_fetcher = _FakeFetcher({DOI: "SHOULD NOT BE USED"})
    cold = LabelResolver(
        adapter=_EmptyAdapter(),  # type: ignore[arg-type]
        external_cache=ExternalLabelCache(session_factory=session_factory),
        external_fetcher=cold_fetcher,  # type: ignore[arg-type]
        remote_settings=settings,
    )
    got = await cold.lookup([DOI], language="en", wait_ms=2000)
    assert got == {DOI: "Persisted Work"}
    assert cold_fetcher.calls == []
