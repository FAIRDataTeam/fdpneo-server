"""End-to-end: Direct Container membership backfill (task 15.1).

Applies a small profile against a real Oxigraph + Postgres (so the root is
seeded as a genuine ``ldp:DirectContainer``), then *downgrades* the root graph
to its pre-15.1 shape (``ldp:BasicContainer``, no membership config) and proves
``backfill_direct_container_membership`` — driven exactly as the
``fdp ldp backfill-membership`` CLI drives it — restores the Direct Container
configuration in place. Idempotent on a second pass.

Requires Docker (testcontainers). Marked ``integration``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy
from testcontainers.postgres import PostgresContainer

from fdp.shared.namespaces import DCT, LDP

REPO_ROOT = Path(__file__).resolve().parents[3]
OXIGRAPH_PORT = 7878
BASE_URL = "http://testserver"
DCAT_CATALOG_REL = URIRef("http://www.w3.org/ns/dcat#catalog")

pytestmark = pytest.mark.integration


PROFILE_MANIFEST = """\
apiVersion: fdp/v1
kind: DeploymentProfile
metadata:
  name: backfill-test
  version: 0.1.0
schemas:
  - id: fdp:Repository
    path: schemas/repository.ttl
  - id: dcat:Catalog
    path: schemas/catalog.ttl
offers:
  - id: system-default
    path: offers/public.ttl
    isSystemDefault: true
resourceDefinitions:
  - urlPrefix: ""
    name: Repository
    schema: fdp:Repository
    children:
      - relationUri: dcat:catalog
        target: catalog
        title: Catalogs
  - urlPrefix: catalog
    name: Catalog
    schema: dcat:Catalog
"""

REPOSITORY_SHAPE_TTL = """\
@prefix sh:   <http://www.w3.org/ns/shacl#> .
@prefix fdp:  <https://w3id.org/fdp/o#> .
@prefix dct:  <http://purl.org/dc/terms/> .
<https://w3id.org/fdp/o#Repository>
    a sh:NodeShape ; sh:targetClass fdp:Repository ;
    sh:property [ sh:path dct:title ; sh:minCount 1 ] .
"""

CATALOG_SHAPE_TTL = """\
@prefix sh:   <http://www.w3.org/ns/shacl#> .
@prefix dcat: <http://www.w3.org/ns/dcat#> .
@prefix dct:  <http://purl.org/dc/terms/> .
<http://www.w3.org/ns/dcat#Catalog>
    a sh:NodeShape ; sh:targetClass dcat:Catalog ;
    sh:property [ sh:path dct:title ; sh:minCount 1 ] .
"""

OFFER_TTL = """\
@prefix odrl: <http://www.w3.org/ns/odrl/2/> .
<http://example.org/offers/public>
    a odrl:Offer ;
    odrl:permission
        [ a odrl:Permission ; odrl:action odrl:read ] ,
        [ a odrl:Permission ; odrl:action odrl:modify ] .
"""


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
def bundle(tmp_path: Path) -> Path:
    root = tmp_path / "profile"
    root.mkdir()
    (root / "profile.yaml").write_text(PROFILE_MANIFEST, encoding="utf-8")
    (root / "schemas").mkdir()
    (root / "schemas" / "repository.ttl").write_text(REPOSITORY_SHAPE_TTL, encoding="utf-8")
    (root / "schemas" / "catalog.ttl").write_text(CATALOG_SHAPE_TTL, encoding="utf-8")
    (root / "offers").mkdir()
    (root / "offers" / "public.ttl").write_text(OFFER_TTL, encoding="utf-8")
    return root


@pytest.fixture
def app_env(
    postgres_container: PostgresContainer,
    oxigraph_container: DockerContainer,
    bundle: Path,
) -> Iterator[None]:
    from fdp.config import get_settings

    host = oxigraph_container.get_container_host_ip()
    port = oxigraph_container.get_exposed_port(OXIGRAPH_PORT)
    oxi = f"http://{host}:{port}"
    env = {
        "POSTGRES_DSN": _async_dsn(postgres_container),
        "FDP_TRIPLESTORE_QUERY_ENDPOINT": f"{oxi}/query",
        "FDP_TRIPLESTORE_UPDATE_ENDPOINT": f"{oxi}/update",
        "FDP_TRIPLESTORE_GRAPH_STORE_ENDPOINT": f"{oxi}/store",
        "FDP_OIDC_ISSUER": "http://idp.local/realms/fdp",
        "FDP_OIDC_AUDIENCE": "fdp",
        "BASE_URL": BASE_URL,
        "FDP_PROFILE_AUTO_APPLY": "true",
        "FDP_PROFILE_PATH": str(bundle),
    }
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    get_settings.cache_clear()
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    command.upgrade(config, "head")
    try:
        yield
    finally:
        for key, prior in saved.items():
            if prior is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prior
        get_settings.cache_clear()


async def _apply_profile_via_startup() -> None:
    """Trigger the app lifespan once so the profile is auto-applied to the store."""
    from fastapi.testclient import TestClient

    from fdp.main import create_app

    with TestClient(create_app(), base_url=BASE_URL):
        pass


def test_backfill_restores_direct_container(app_env: None) -> None:
    import asyncio

    asyncio.run(_run())


async def _run() -> None:
    from fdp.config import get_settings
    from fdp.metadata.profiles.backfill import backfill_direct_container_membership
    from fdp.metadata.profiles.rd_service import build_cache_from_repository
    from fdp.metadata.repository import MetadataRepository
    from fdp.storage.triplestore.adapter import TripleStoreAdapter

    await _apply_profile_via_startup()

    settings = get_settings()
    root = URIRef(BASE_URL)
    async with TripleStoreAdapter.from_settings(settings.triplestore) as adapter:
        repository = MetadataRepository(adapter)

        # The seed already made the root a Direct Container (post-15.1).
        seeded = await repository.get_graph(BASE_URL)
        assert (root, RDF.type, LDP.DirectContainer) in seeded

        # Downgrade it to the pre-15.1 shape: BasicContainer, no membership config.
        downgraded = Graph()
        downgraded.add((root, RDF.type, URIRef("https://w3id.org/fdp/o#Repository")))
        downgraded.add((root, RDF.type, LDP.BasicContainer))
        downgraded.add((root, DCT.title, Literal("Root")))
        await adapter.replace_graph(
            BASE_URL, downgraded.serialize(format="nt"), mime="application/n-triples"
        )

        # Backfill, exactly as the CLI builds its collaborators.
        cache = await build_cache_from_repository(adapter, base_url=str(settings.base_url))
        report = await backfill_direct_container_membership(
            repository=repository, adapter=adapter, cache=cache
        )
        assert BASE_URL in report.stamped

        restored = await repository.get_graph(BASE_URL)
        assert (root, RDF.type, LDP.DirectContainer) in restored
        assert (root, LDP.membershipResource, root) in restored
        assert (root, LDP.insertedContentRelation, LDP.MemberSubject) in restored
        assert (root, LDP.hasMemberRelation, DCAT_CATALOG_REL) in restored
        assert (root, RDF.type, LDP.BasicContainer) not in restored
        # Original content preserved.
        assert (root, DCT.title, Literal("Root")) in restored

        # Idempotent: a second pass changes nothing.
        again = await backfill_direct_container_membership(
            repository=repository, adapter=adapter, cache=cache
        )
        assert again.stamped == []
        assert BASE_URL in again.already_conformant
