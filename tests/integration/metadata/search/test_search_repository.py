"""Integration tests for the Postgres FTS search repository (Phase 7.2).

Exercises the real ``tsvector`` query, filters, facets, and the ADR-0010
visibility gate against a live Postgres (testcontainers). Postgres-only — no
triple store needed; rows are written directly through the repository.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from importlib.resources import files

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from testcontainers.postgres import PostgresContainer

from fdp.metadata.search.extract import ExtractedRecord
from fdp.metadata.search.repository import SearchIndexRepository, SearchQuery
from fdp.metadata.states import MetadataState

NOW = datetime(2026, 6, 2, 12, 0, tzinfo=UTC)

pytestmark = pytest.mark.integration


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
def migrated(postgres_container: PostgresContainer) -> Iterator[str]:
    """Apply ``alembic upgrade head`` (sync — Alembic drives its own loop)."""
    from fdp.config import get_settings

    dsn = _async_dsn(postgres_container)
    original = os.environ.get("POSTGRES_DSN")
    os.environ["POSTGRES_DSN"] = dsn
    get_settings.cache_clear()
    config = Config(str(files("fdp") / "alembic.ini"))
    try:
        command.upgrade(config, "head")
        yield dsn
    finally:
        if original is None:
            os.environ.pop("POSTGRES_DSN", None)
        else:
            os.environ["POSTGRES_DSN"] = original
        get_settings.cache_clear()


@pytest.fixture
async def repo(migrated: str) -> AsyncIterator[SearchIndexRepository]:
    engine = create_async_engine(migrated, future=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield SearchIndexRepository(session_factory=factory)
    finally:
        await engine.dispose()


CATALOG = "http://www.w3.org/ns/dcat#Catalog"
DATASET = "http://www.w3.org/ns/dcat#Dataset"
CC_BY = "https://creativecommons.org/licenses/by/4.0/"
MIT = "https://opensource.org/licenses/MIT"

PUB1 = "http://testserver/catalog/genomics"
PUB2 = "http://testserver/dataset/proteomics"
DRAFT1 = "http://testserver/dataset/secret"
RESTRICTED1 = "http://testserver/dataset/private"


async def _seed(repo: SearchIndexRepository) -> None:
    async def put(
        iri: str,
        *,
        title: str,
        type_iri: str,
        license: str | None,
        state: MetadataState,
        anon_read: bool,
        updated_at: datetime = NOW,
    ) -> None:
        rec = ExtractedRecord(
            record_iri=iri,
            type_iri=type_iri,
            title=title,
            description="",
            license=license,
            keywords=None,
            state=state,
            updated_at=updated_at,
        )
        await repo.upsert(rec, anon_read=anon_read, language="english")

    await put(
        PUB1,
        title="Genomics Catalog",
        type_iri=CATALOG,
        license=CC_BY,
        state=MetadataState.PUBLISHED,
        anon_read=True,
    )
    await put(
        PUB2,
        title="Proteomics Dataset",
        type_iri=DATASET,
        license=CC_BY,
        state=MetadataState.PUBLISHED,
        anon_read=True,
        updated_at=NOW - timedelta(days=1),
    )
    await put(
        DRAFT1,
        title="Secret Genomics Draft",
        type_iri=DATASET,
        license=MIT,
        state=MetadataState.DRAFT,
        anon_read=False,
    )
    await put(
        RESTRICTED1,
        title="Private Genomics",
        type_iri=DATASET,
        license=MIT,
        state=MetadataState.PUBLISHED,
        anon_read=False,
    )


def _q(**kw: object) -> SearchQuery:
    return SearchQuery(text=kw.pop("text", None), **kw)  # type: ignore[arg-type]


async def test_anonymous_text_search_is_public_only(repo: SearchIndexRepository) -> None:
    await _seed(repo)
    result = await repo.search(_q(text="genomics", anonymous=True, limit=20))
    iris = {h.record_iri for h in result.hits}
    # Only the public genomics catalog — the draft and the anon-restricted
    # record both match the text but are filtered by visibility.
    assert iris == {PUB1}
    assert result.total == 1


async def test_authenticated_sees_visible_tail(repo: SearchIndexRepository) -> None:
    await _seed(repo)
    result = await repo.search(
        _q(text="genomics", anonymous=False, visible=(DRAFT1, RESTRICTED1), limit=20)
    )
    iris = {h.record_iri for h in result.hits}
    assert iris == {PUB1, DRAFT1, RESTRICTED1}


async def test_browse_all_orders_by_recency(repo: SearchIndexRepository) -> None:
    await _seed(repo)
    result = await repo.search(_q(text=None, anonymous=True, limit=20))
    # Both public records, most-recent first (PUB1 is newer than PUB2).
    assert [h.record_iri for h in result.hits] == [PUB1, PUB2]


async def test_type_filter(repo: SearchIndexRepository) -> None:
    await _seed(repo)
    result = await repo.search(_q(text=None, anonymous=True, types=(DATASET,), limit=20))
    assert {h.record_iri for h in result.hits} == {PUB2}


async def test_facets_count_visible_set(repo: SearchIndexRepository) -> None:
    await _seed(repo)
    result = await repo.search(_q(text=None, anonymous=True, limit=20))
    by_type = {b.value: b.count for b in result.facet_type}
    by_license = {b.value: b.count for b in result.facet_license}
    assert by_type == {CATALOG: 1, DATASET: 1}  # only the two public records
    assert by_license == {CC_BY: 2}


async def test_date_range_filter(repo: SearchIndexRepository) -> None:
    await _seed(repo)
    # Only records updated on/after NOW (excludes PUB2 at NOW-1d).
    result = await repo.search(_q(text=None, anonymous=True, updated_from=NOW, limit=20))
    assert {h.record_iri for h in result.hits} == {PUB1}
