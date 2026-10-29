"""Integration tests: full-deployment backup and restore (ADR-0029).

Real PostgreSQL (two databases in one container: source and target) and a real
Oxigraph store. Covers the round trip at the current schema, the restore of an
archive taken at an *older* schema revision into a database already at head
(downgrade → load → upgrade), sequence repair, the raw-metrics exclusion, and
the refusal paths.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from pydantic import HttpUrl
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from testcontainers.community.postgres import PostgresContainer
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

from fdpneo_server.config import TripleStoreSettings
from fdpneo_server.metadata.backup import open_archive, orchestrate_restore
from fdpneo_server.metadata.backup.orchestrate import dump_to_archive
from fdpneo_server.metadata.backup.restore import RestoreError
from fdpneo_server.metadata.backup.seal import KdfParams, SealError
from fdpneo_server.storage.postgres.migrations import (
    current_revision,
    head_revision,
    migrate_to,
)
from fdpneo_server.storage.triplestore.adapter import TURTLE, TripleStoreAdapter

pytestmark = pytest.mark.integration

BASE = "http://localhost:8000"
PASS = "integration test passphrase"
FAST = KdfParams(log2_n=4)
OLD_REVISION = "0007_api_keys"
NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)


@dataclass
class _Search:
    default_language: str = "english"


@dataclass
class _Profile:
    path: None = None


@dataclass
class _Settings:
    """The slice of Settings the restore orchestration reads."""

    postgres_dsn: str
    resolved_identifier_base: str = BASE
    search: _Search = field(default_factory=_Search)
    profile: _Profile = field(default_factory=_Profile)


def _async_dsn(container: PostgresContainer, database: str | None = None) -> str:
    raw = container.get_connection_url()
    dsn = raw.replace("postgresql+psycopg2://", "postgresql+asyncpg://").replace(
        "postgresql://", "postgresql+asyncpg://"
    )
    if database is not None:
        dsn = dsn.rsplit("/", 1)[0] + f"/{database}"
    return dsn


@pytest.fixture(scope="module")
def postgres_container() -> Iterator[PostgresContainer]:
    with PostgresContainer("postgres:18-alpine") as container:
        yield container


@pytest.fixture(scope="module")
def oxigraph_container() -> Iterator[DockerContainer]:
    container = (
        DockerContainer("oxigraph/oxigraph:latest")
        .with_exposed_ports(7878)
        .with_command("serve --bind 0.0.0.0:7878 --location /data")
        .waiting_for(LogMessageWaitStrategy("Listening").with_startup_timeout(60))
    )
    with container:
        yield container


@pytest.fixture
async def adapter(oxigraph_container: DockerContainer) -> AsyncIterator[TripleStoreAdapter]:
    host = oxigraph_container.get_container_host_ip()
    base = f"http://{host}:{oxigraph_container.get_exposed_port(7878)}"
    settings = TripleStoreSettings(
        query_endpoint=HttpUrl(f"{base}/query"),
        update_endpoint=HttpUrl(f"{base}/update"),
        graph_store_endpoint=HttpUrl(f"{base}/store"),
    )
    async with TripleStoreAdapter.from_settings(settings) as a:
        await a.clear_all()
        yield a


_counter = 0


@pytest.fixture
async def databases(
    postgres_container: PostgresContainer,
) -> AsyncIterator[tuple[str, str]]:
    """A fresh (source, target) pair of empty databases for each test."""
    global _counter
    _counter += 1
    names = (f"src{_counter}", f"dst{_counter}")
    admin = create_async_engine(_async_dsn(postgres_container), isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            for name in names:
                await conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    finally:
        await admin.dispose()
    yield _async_dsn(postgres_container, names[0]), _async_dsn(postgres_container, names[1])


async def _seed_state(engine: AsyncEngine, *, at_head: bool = True) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            sa.text(
                "INSERT INTO runtime_settings (key, value_json, updated_by, updated_at) "
                "VALUES ('access.control', CAST(:v AS JSONB), 'admin', :t)"
            ),
            {"v": '{"mode": "odrl", "writer_roles": ["admin"]}', "t": NOW},
        )
        await conn.execute(
            sa.text(
                "INSERT INTO api_keys (id, owner_subject, label, key_hash, display_prefix, "
                "roles_json, groups_json, created_at) VALUES ('k1', 'sub-alice', 'ci', "
                "'hash-abc', 'fdp_abc', CAST('[\"steward\"]' AS JSONB), CAST('[]' AS JSONB), :t)"
            ),
            {"t": NOW},
        )
        await conn.execute(
            sa.text(
                "INSERT INTO record_audit (id, record_iri, operation, subject, etag, occurred_at) "
                "VALUES (41, :iri, 'create', 'sub-alice', 'e1', :t), "
                "(42, :iri, 'update', 'sub-alice', 'e2', :t)"
            ),
            {"iri": f"{BASE}/catalog/c1", "t": NOW.replace(tzinfo=None)},
        )
        await conn.execute(
            sa.text(
                "INSERT INTO metrics_raw (bucket, event_type, resource_iri, visitor_hash, "
                "status_code, latency_ms, recorded_at) VALUES "
                "(:t, 'view', :iri, 'pseudonymous-hash', 200, 5, :t)"
            ),
            {"iri": f"{BASE}/catalog/c1", "t": datetime.now(UTC).replace(tzinfo=None)},
        )
        if at_head:
            await conn.execute(
                sa.text(
                    "INSERT INTO index_targets (id, url, created_at) "
                    "VALUES ('t1', 'https://index.example.org', :t)"
                ),
                {"t": NOW},
            )


async def _seed_store(adapter: TripleStoreAdapter) -> None:
    await adapter.replace_graph(
        f"{BASE}/catalog/c1",
        f"<{BASE}/catalog/c1> a <http://www.w3.org/ns/dcat#Catalog> ; "
        f'<http://purl.org/dc/terms/title> "Full backup" .',
        mime=TURTLE,
    )


async def _scalar(engine: AsyncEngine, sql: str) -> object:
    async with engine.connect() as conn:
        return (await conn.execute(sa.text(sql))).scalar()


async def _dump(adapter: TripleStoreAdapter, engine: AsyncEngine, tmp_path: Path) -> Path:
    _, archive = await dump_to_archive(
        adapter,
        identifier_base=BASE,
        work_dir=tmp_path / "work",
        session_factory=async_sessionmaker(engine),
        full_engine=engine,
        passphrase=PASS,
        kdf=FAST,
    )
    return archive


async def _restore(
    adapter: TripleStoreAdapter,
    engine: AsyncEngine,
    dsn: str,
    archive: Path,
    tmp_path: Path,
    **kwargs: bool,
):  # -> RestoreOutcome
    extracted = await open_archive(archive, tmp_path / "extracted", passphrase=PASS)
    return await orchestrate_restore(
        adapter,
        async_sessionmaker(engine),
        settings=_Settings(postgres_dsn=dsn),  # type: ignore[arg-type]
        in_dir=extracted,
        engine=engine,
        dsn=dsn,
        **kwargs,
    )


async def test_full_round_trip_into_an_unmigrated_database(
    adapter: TripleStoreAdapter, databases: tuple[str, str], tmp_path: Path
) -> None:
    src_dsn, dst_dsn = databases
    src, dst = create_async_engine(src_dsn), create_async_engine(dst_dsn)
    try:
        await migrate_to(src_dsn, current=None, target=head_revision())
        await _seed_state(src)
        await _seed_store(adapter)
        archive = await _dump(adapter, src, tmp_path)
        assert archive.read_bytes().startswith(b"FDPNEO-ENC1")
        assert b"hash-abc" not in archive.read_bytes()

        await adapter.clear_all()
        outcome = await _restore(adapter, dst, dst_dsn, archive, tmp_path)

        assert outcome.scope == "full"
        assert outcome.result.graphs_loaded == 1
        assert await current_revision(dst) == head_revision()
        assert outcome.state_rows["runtime_settings"] == 1
        assert (
            await _scalar(
                dst, "SELECT value_json->>'mode' FROM runtime_settings WHERE key='access.control'"
            )
            == "odrl"
        )
        assert await _scalar(dst, "SELECT key_hash FROM api_keys WHERE id='k1'") == "hash-abc"
        assert await _scalar(dst, "SELECT count(*) FROM index_targets") == 1
        assert await _scalar(dst, "SELECT max(id) FROM record_audit") == 42
        # ADR-0002: raw metrics never travel.
        assert await _scalar(dst, "SELECT count(*) FROM metrics_raw") == 0
        # Search is rebuilt from the restored store.
        assert await _scalar(dst, "SELECT count(*) FROM metadata_search") == 1
        # Sequences were moved past the loaded ids: a new audit row does not clash.
        async with dst.begin() as conn:
            new_id = (
                await conn.execute(
                    sa.text(
                        "INSERT INTO record_audit (record_iri, operation, occurred_at) "
                        "VALUES ('x', 'create', now()) RETURNING id"
                    )
                )
            ).scalar()
        assert new_id == 43
    finally:
        await src.dispose()
        await dst.dispose()


async def test_older_archive_into_a_database_at_head(
    adapter: TripleStoreAdapter, databases: tuple[str, str], tmp_path: Path
) -> None:
    src_dsn, dst_dsn = databases
    src, dst = create_async_engine(src_dsn), create_async_engine(dst_dsn)
    try:
        await migrate_to(src_dsn, current=None, target=OLD_REVISION)
        await _seed_state(src, at_head=False)
        await _seed_store(adapter)
        archive = await _dump(adapter, src, tmp_path)

        await adapter.clear_all()
        await migrate_to(dst_dsn, current=None, target=head_revision())
        async with dst.begin() as conn:  # derived state from the target's own life
            await conn.execute(
                sa.text(
                    "INSERT INTO metadata_external_labels (iri, language, label, resolved_at, "
                    "expires_at, source_host) VALUES ('https://x', 'en', 'X', now(), now(), 'h')"
                )
            )
        outcome = await _restore(adapter, dst, dst_dsn, archive, tmp_path)

        assert outcome.schema_revision == head_revision()
        assert await current_revision(dst) == head_revision()
        assert await _scalar(dst, "SELECT key_hash FROM api_keys WHERE id='k1'") == "hash-abc"
        assert await _scalar(dst, "SELECT count(*) FROM metadata_external_labels") == 0
        # Tables newer than the archive exist (migrated to head) and are empty.
        assert await _scalar(dst, "SELECT count(*) FROM index_targets") == 0
    finally:
        await src.dispose()
        await dst.dispose()


async def test_refusals(
    adapter: TripleStoreAdapter, databases: tuple[str, str], tmp_path: Path
) -> None:
    src_dsn, dst_dsn = databases
    src, dst = create_async_engine(src_dsn), create_async_engine(dst_dsn)
    try:
        await migrate_to(src_dsn, current=None, target=head_revision())
        await _seed_state(src)
        await _seed_store(adapter)
        archive = await _dump(adapter, src, tmp_path)

        with pytest.raises(SealError):
            await open_archive(archive, tmp_path / "x", passphrase="wrong passphrase!!")

        # The source database already holds state: refused without --overwrite.
        await adapter.clear_all()
        with pytest.raises(RestoreError, match="already holds state"):
            await _restore(adapter, src, src_dsn, archive, tmp_path)
        # --merge is meaningless for Postgres state.
        with pytest.raises(RestoreError, match="--merge is not supported"):
            await _restore(adapter, dst, dst_dsn, archive, tmp_path / "m", merge=True)
        # The HTTP path never moves the schema of a running server.
        with pytest.raises(RestoreError, match="schema revision"):
            await _restore(
                adapter, dst, dst_dsn, archive, tmp_path / "h", allow_schema_migration=False
            )
        # A non-empty triple store is refused before Postgres is touched.
        await _seed_store(adapter)
        with pytest.raises(RestoreError, match="not empty"):
            await _restore(adapter, dst, dst_dsn, archive, tmp_path / "s")
        assert await current_revision(dst) is None
        # --overwrite replaces both.
        outcome = await _restore(adapter, src, src_dsn, archive, tmp_path / "o", overwrite=True)
        assert outcome.state_rows["api_keys"] == 1
        assert await _scalar(src, "SELECT count(*) FROM api_keys") == 1
    finally:
        await src.dispose()
        await dst.dispose()
