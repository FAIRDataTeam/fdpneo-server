"""End-to-end profile apply against Oxigraph + Postgres.

Spins up both containers, runs Alembic to head, applies a tiny test
profile, and asserts on both stores: the schema/container/offer graphs
exist in the triple store and the ``profile_applied`` row is in
Postgres. Also covers the re-apply refusal and the force-then-clear
re-apply path.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from pydantic import HttpUrl, PostgresDsn
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy
from testcontainers.postgres import PostgresContainer

from fdp.config import OIDCSettings, Settings, TripleStoreSettings
from fdp.metadata.profiles import (
    ProfileStateRepository,
    apply_profile,
    load_profile,
)
from fdp.metadata.repository import MetadataRepository
from fdp.shared.errors import Conflict
from fdp.storage.triplestore.adapter import TripleStoreAdapter

REPO_ROOT = Path(__file__).resolve().parents[4]
OXIGRAPH_PORT = 7878

PROFILE_MANIFEST = """\
apiVersion: fdp/v1
kind: DeploymentProfile
metadata:
  name: test-bootstrap
  version: 0.1.0
schemas:
  - id: dcat:Catalog
    path: schemas/catalog.ttl
offers:
  - id: system-default
    path: offers/public.ttl
    isSystemDefault: true
resourceDefinitions:
  - urlPrefix: ""
    name: Repository
    schema: dcat:Catalog
"""

CATALOG_SHAPE_TTL = """\
@prefix sh:   <http://www.w3.org/ns/shacl#> .
@prefix dcat: <http://www.w3.org/ns/dcat#> .
@prefix dct:  <http://purl.org/dc/terms/> .

<http://www.w3.org/ns/dcat#Catalog>
    a sh:NodeShape ;
    sh:targetClass dcat:Catalog ;
    sh:property [
        sh:path dct:title ;
        sh:minCount 1 ;
    ] .
"""

OFFER_TTL = """\
@prefix odrl: <http://www.w3.org/ns/odrl/2/> .
<http://example.org/offers/public>
    a odrl:Offer ;
    odrl:permission [ a odrl:Permission ; odrl:action odrl:read ] .
"""


# --- containers -----------------------------------------------------------


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
def oxigraph_container() -> Iterator[DockerContainer]:
    container = (
        DockerContainer("oxigraph/oxigraph:latest")
        .with_exposed_ports(OXIGRAPH_PORT)
        .with_command("serve --bind 0.0.0.0:7878 --location /data")
        .waiting_for(LogMessageWaitStrategy("Listening").with_startup_timeout(60))
    )
    with container:
        yield container


@pytest.fixture
def migrated_postgres(postgres_container: PostgresContainer) -> Iterator[PostgresContainer]:
    from fdp.config import get_settings

    original = os.environ.get("POSTGRES_DSN")
    os.environ["POSTGRES_DSN"] = _async_dsn(postgres_container)
    get_settings.cache_clear()
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
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
    migrated_postgres: PostgresContainer,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(_async_dsn(migrated_postgres))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
async def adapter(
    oxigraph_container: DockerContainer,
) -> AsyncIterator[TripleStoreAdapter]:
    host = oxigraph_container.get_container_host_ip()
    port = oxigraph_container.get_exposed_port(OXIGRAPH_PORT)
    base = f"http://{host}:{port}"
    settings = TripleStoreSettings(
        query_endpoint=HttpUrl(f"{base}/query"),
        update_endpoint=HttpUrl(f"{base}/update"),
        graph_store_endpoint=HttpUrl(f"{base}/store"),
    )
    async with TripleStoreAdapter.from_settings(settings) as a:
        yield a


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    root = tmp_path / "profile"
    root.mkdir()
    (root / "profile.yaml").write_text(PROFILE_MANIFEST, encoding="utf-8")
    (root / "schemas").mkdir()
    (root / "schemas" / "catalog.ttl").write_text(CATALOG_SHAPE_TTL, encoding="utf-8")
    (root / "offers").mkdir()
    (root / "offers" / "public.ttl").write_text(OFFER_TTL, encoding="utf-8")
    return root


def _settings() -> Settings:
    return Settings(
        postgres_dsn=PostgresDsn(
            "postgresql+asyncpg://placeholder:placeholder@localhost:5432/placeholder"
        ),
        triplestore=TripleStoreSettings(
            query_endpoint=HttpUrl("http://triplestore.local/query"),
            update_endpoint=HttpUrl("http://triplestore.local/update"),
        ),
        oidc=OIDCSettings(
            issuer=HttpUrl("http://idp.local/realms/fdp"),
            audience="fdp",
        ),
    )


# --- tests ---------------------------------------------------------------


@pytest.mark.integration
async def test_apply_writes_graphs_and_marker(
    adapter: TripleStoreAdapter,
    session_factory: async_sessionmaker[AsyncSession],
    bundle: Path,
) -> None:
    profile = load_profile(bundle)
    repository = MetadataRepository(adapter)
    settings = _settings()

    async with session_factory() as session:
        state = ProfileStateRepository(session)
        report = await apply_profile(
            profile,
            repository=repository,
            state=state,
            session=session,
            settings=settings,
        )

    assert report.total_written == 3

    # Triple store: each of the three named graphs has triples.
    schema_iri = "http://www.w3.org/ns/dcat#Catalog"
    # Offer IRI is the one declared inside the TTL file (intrinsic).
    offer_iri = "http://example.org/offers/public"
    # Repository seed lives at the API root (the configured base_url).
    repo_iri = str(settings.base_url).rstrip("/")
    for iri in (schema_iri, offer_iri, repo_iri):
        assert await adapter.ask(f"ASK {{ GRAPH <{iri}> {{ ?s ?p ?o }} }}") is True

    assert report.repository_iri == repo_iri
    assert report.resource_definitions is not None
    assert report.resource_definitions.root() is not None

    # Postgres: the marker row is in place.
    async with session_factory() as session:
        state = ProfileStateRepository(session)
        applied = await state.current()
        assert applied is not None
        assert applied.name == "test-bootstrap"
        assert applied.version == "0.1.0"


@pytest.mark.integration
async def test_re_apply_without_force_is_refused(
    adapter: TripleStoreAdapter,
    session_factory: async_sessionmaker[AsyncSession],
    bundle: Path,
) -> None:
    profile = load_profile(bundle)
    repository = MetadataRepository(adapter)
    settings = _settings()

    async with session_factory() as session:
        await apply_profile(
            profile,
            repository=repository,
            state=ProfileStateRepository(session),
            session=session,
            settings=settings,
        )

    async with session_factory() as session:
        with pytest.raises(Conflict):
            await apply_profile(
                profile,
                repository=repository,
                state=ProfileStateRepository(session),
                session=session,
                settings=settings,
            )


@pytest.mark.integration
async def test_force_clear_then_apply_succeeds(
    adapter: TripleStoreAdapter,
    session_factory: async_sessionmaker[AsyncSession],
    bundle: Path,
) -> None:
    profile = load_profile(bundle)
    repository = MetadataRepository(adapter)
    settings = _settings()

    async with session_factory() as session:
        await apply_profile(
            profile,
            repository=repository,
            state=ProfileStateRepository(session),
            session=session,
            settings=settings,
        )

    # Caller-driven force: clear the marker, then apply with force=True.
    async with session_factory() as session:
        state = ProfileStateRepository(session)
        cleared = await state.clear()
        assert cleared == 1
        await session.commit()

    async with session_factory() as session:
        state = ProfileStateRepository(session)
        report = await apply_profile(
            profile,
            repository=repository,
            state=state,
            session=session,
            settings=settings,
            force=True,
        )
    assert report.total_written == 3
