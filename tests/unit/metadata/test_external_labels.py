"""Unit tests for external (remote) label resolution (Phase 21).

Grows across the phase; this first slice covers the ``RemoteLabelSettings``
configuration group (env parsing + the ``effective_enabled`` gate).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fdp.config import RemoteLabelSettings
from fdp.metadata.external_labels import ExternalLabelCache, ExternalLabelRow
from fdp.storage.postgres.models import Base, register_all_models

pytestmark = pytest.mark.unit


def _settings(**over: object) -> RemoteLabelSettings:
    return RemoteLabelSettings(_env_file=None, **over)  # type: ignore[arg-type]


def test_defaults_are_off_and_deny_all() -> None:
    s = _settings()
    assert s.enabled is False
    assert s.allowed_hosts == []
    assert s.hosts == frozenset()
    assert s.effective_enabled is False


def test_allowed_hosts_parses_csv() -> None:
    s = _settings(allowed_hosts="ror.org, doi.org , orcid.org")
    assert s.allowed_hosts == ["ror.org", "doi.org", "orcid.org"]
    assert s.hosts == frozenset({"ror.org", "doi.org", "orcid.org"})


def test_allowed_hosts_parses_json_array() -> None:
    s = _settings(allowed_hosts='["ror.org", "doi.org"]')
    assert s.allowed_hosts == ["ror.org", "doi.org"]


def test_effective_enabled_requires_switch_and_hosts() -> None:
    # Switch on but no hosts → still inert.
    assert _settings(enabled=True, allowed_hosts=[]).effective_enabled is False
    # Hosts listed but switch off → inert.
    assert _settings(enabled=False, allowed_hosts="ror.org").effective_enabled is False
    # Both → live.
    assert _settings(enabled=True, allowed_hosts="ror.org").effective_enabled is True


# --- ExternalLabelCache (SQLite variant) -----------------------------------

ROR = "https://ror.org/006hf6230"


@pytest.fixture
async def session_factory() -> Any:
    register_all_models()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def test_upsert_then_get_many_roundtrips(session_factory: Any) -> None:
    cache = ExternalLabelCache(session_factory=session_factory)
    await cache.upsert(ROR, "en", "University of Twente", ttl_seconds=3600, source_host="ror.org")
    got = await cache.get_many([ROR, "https://ror.org/unknown"], language="en")
    assert got == {ROR: "University of Twente"}


async def test_negative_result_is_cached_and_distinguishable(session_factory: Any) -> None:
    cache = ExternalLabelCache(session_factory=session_factory)
    await cache.upsert(ROR, "en", None, ttl_seconds=3600)
    got = await cache.get_many([ROR], language="en")
    # Present-with-None = cached miss; absent = unknown.
    assert ROR in got
    assert got[ROR] is None


async def test_language_is_part_of_the_key(session_factory: Any) -> None:
    cache = ExternalLabelCache(session_factory=session_factory)
    await cache.upsert(ROR, "en", "Twente", ttl_seconds=3600)
    assert await cache.get_many([ROR], language="nl") == {}


async def test_upsert_replaces_existing_row(session_factory: Any) -> None:
    cache = ExternalLabelCache(session_factory=session_factory)
    await cache.upsert(ROR, "en", "old", ttl_seconds=3600)
    await cache.upsert(ROR, "en", "new", ttl_seconds=3600)
    assert await cache.get_many([ROR], language="en") == {ROR: "new"}


async def test_expired_rows_are_not_returned(session_factory: Any) -> None:
    cache = ExternalLabelCache(session_factory=session_factory)
    # Write a pre-expired row directly.
    past = datetime.now(UTC) - timedelta(hours=1)
    async with session_factory() as session:
        session.add(
            ExternalLabelRow(
                iri=ROR, language="en", label="stale", resolved_at=past, expires_at=past
            )
        )
        await session.commit()
    assert await cache.get_many([ROR], language="en") == {}


async def test_purge_expired_removes_only_stale(session_factory: Any) -> None:
    cache = ExternalLabelCache(session_factory=session_factory)
    now = datetime.now(UTC)
    async with session_factory() as session:
        session.add(
            ExternalLabelRow(
                iri="https://ror.org/live",
                language="en",
                label="live",
                resolved_at=now,
                expires_at=now + timedelta(hours=1),
            )
        )
        session.add(
            ExternalLabelRow(
                iri="https://ror.org/dead",
                language="en",
                label="dead",
                resolved_at=now,
                expires_at=now - timedelta(hours=1),
            )
        )
        await session.commit()
    assert await cache.purge_expired() == 1
    assert await cache.get_many(["https://ror.org/live"], language="en") == {
        "https://ror.org/live": "live"
    }
