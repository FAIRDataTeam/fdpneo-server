"""Unit tests for the Postgres engine and session factory builders.

These run without a real database — they only verify the factory wiring
and the asyncpg-driver guard.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from fdpneo_server.config import OIDCSettings, Settings, TripleStoreSettings
from fdpneo_server.storage.postgres import build_engine, build_session_factory


def _make_settings(dsn: str) -> Settings:
    """Tiny factory — broader settings stitching lives in tests/unit/shared/conftest.py."""
    from pydantic import HttpUrl

    return Settings(
        postgres_dsn=dsn,  # type: ignore[arg-type]
        triplestore=TripleStoreSettings(
            query_endpoint=HttpUrl("http://triplestore.local/query"),
            update_endpoint=HttpUrl("http://triplestore.local/update"),
        ),
        oidc=OIDCSettings(
            issuer=HttpUrl("http://idp.local/realms/fdp"),
            audience="fdp",
        ),
    )


@pytest.mark.unit
def test_build_engine_returns_async_engine() -> None:
    settings = _make_settings("postgresql+asyncpg://fdp:fdp@db.local:5432/fdp")
    engine = build_engine(settings)
    assert isinstance(engine, AsyncEngine)
    assert "asyncpg" in str(engine.url)


@pytest.mark.unit
def test_build_engine_rejects_sync_dsn() -> None:
    settings = _make_settings("postgresql://fdp:fdp@db.local:5432/fdp")
    with pytest.raises(ValueError, match="asyncpg driver"):
        build_engine(settings)


@pytest.mark.unit
def test_build_session_factory_binds_to_engine() -> None:
    settings = _make_settings("postgresql+asyncpg://fdp:fdp@db.local:5432/fdp")
    engine = build_engine(settings)
    factory: async_sessionmaker[Any] = build_session_factory(engine)
    session = factory()
    assert session.bind is engine
