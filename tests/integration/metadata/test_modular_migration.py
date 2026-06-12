"""End-to-end: migrate a pre-15.2 deployment to the modular default profile.

Bootstraps an *old-style* profile (root typed ``fdp:Repository``, a flat catalog
schema, a ``dcat:catalog`` root relation) against a real Oxigraph + Postgres,
writes an authored Catalog member, then runs ``migrate_to_modular_profile`` with
the **shipped** ``profiles/default`` bundle — exactly as ``fdp profile
migrate-modular`` drives it — and proves:

1. the root record is re-typed to ``fdp-o:FAIRDataPoint`` with the new
   ``fdp-o:servesMetadata`` membership relation, authored metadata preserved;
2. the orphaned old root resource-definition record is removed, so the rebuilt
   cache (``build_cache_from_repository``) has a single, correct root;
3. the authored Catalog member survives untouched;
4. the new modular schemas land (composed via ``sh:node``);
5. a second pass is a no-op.

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
DEFAULT_PROFILE = REPO_ROOT / "profiles" / "default"
OXIGRAPH_PORT = 7878
BASE_URL = "http://testserver"

pytestmark = pytest.mark.integration


OLD_MANIFEST = """\
apiVersion: fdp/v1
kind: DeploymentProfile
metadata:
  name: legacy
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

OLD_REPOSITORY_SHAPE = """\
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix fdp: <https://w3id.org/fdp/o#> .
@prefix dct: <http://purl.org/dc/terms/> .
<https://w3id.org/fdp/o#Repository>
    a sh:NodeShape ; sh:targetClass fdp:Repository ;
    sh:property [ sh:path dct:title ; sh:minCount 1 ] .
"""

OLD_CATALOG_SHAPE = """\
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix dcat: <http://www.w3.org/ns/dcat#> .
@prefix dct: <http://purl.org/dc/terms/> .
<http://www.w3.org/ns/dcat#Catalog>
    a sh:NodeShape ; sh:targetClass dcat:Catalog ;
    sh:property [ sh:path dct:title ; sh:minCount 1 ] .
"""

OFFER = """\
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
def old_bundle(tmp_path: Path) -> Path:
    root = tmp_path / "legacy"
    root.mkdir()
    (root / "profile.yaml").write_text(OLD_MANIFEST, encoding="utf-8")
    (root / "schemas").mkdir()
    (root / "schemas" / "repository.ttl").write_text(OLD_REPOSITORY_SHAPE, encoding="utf-8")
    (root / "schemas" / "catalog.ttl").write_text(OLD_CATALOG_SHAPE, encoding="utf-8")
    (root / "offers").mkdir()
    (root / "offers" / "public.ttl").write_text(OFFER, encoding="utf-8")
    return root


@pytest.fixture
def app_env(
    postgres_container: PostgresContainer,
    oxigraph_container: DockerContainer,
    old_bundle: Path,
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
        "FDP_PROFILE_PATH": str(old_bundle),
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


async def _apply_old_profile_via_startup() -> None:
    from fastapi.testclient import TestClient

    from fdp.main import create_app

    with TestClient(create_app(), base_url=BASE_URL):
        pass


def test_modular_migration_end_to_end(app_env: None) -> None:
    import asyncio

    asyncio.run(_run())


async def _run() -> None:
    from fdp.config import get_settings
    from fdp.metadata.profiles import load_profile
    from fdp.metadata.profiles.iri import IRIExpander
    from fdp.metadata.profiles.migrate_modular import migrate_to_modular_profile
    from fdp.metadata.profiles.rd_service import build_cache_from_repository
    from fdp.metadata.repository import MetadataRepository
    from fdp.storage.triplestore.adapter import TripleStoreAdapter

    await _apply_old_profile_via_startup()

    settings = get_settings()
    expander = IRIExpander(settings=settings)
    fdp_point_class = URIRef(expander.schema_iri("fdp-o:FAIRDataPoint"))
    serves_metadata = URIRef(expander.schema_iri("fdp-o:servesMetadata"))
    catalog_iri = f"{BASE_URL}/catalog/c1"
    modular = load_profile(DEFAULT_PROFILE)

    async with TripleStoreAdapter.from_settings(settings.triplestore) as adapter:
        repository = MetadataRepository(adapter)

        # Pre-state: root typed fdp:Repository; author a Catalog member.
        root_before = await repository.get_graph(BASE_URL)
        assert (URIRef(BASE_URL), RDF.type, URIRef(expander.schema_iri("fdp:Repository"))) in (
            root_before
        )
        catalog = Graph()
        cs = URIRef(catalog_iri)
        catalog.add((cs, RDF.type, URIRef("http://www.w3.org/ns/dcat#Catalog")))
        catalog.add((cs, DCT.title, Literal("Authored catalog")))
        await repository.put_graph(catalog_iri, catalog, subject="http://idp.local/realms/fdp#a")

        # Migrate to the shipped modular profile.
        report = await migrate_to_modular_profile(
            modular, repository=repository, adapter=adapter, settings=settings
        )
        assert report.root_retyped == BASE_URL
        assert report.schemas_written  # the modular shapes landed
        # The old root RD (slug "repository") is gone; new one ("fairdatapoint") is in.
        assert report.resource_definitions_removed

        # 1. Root re-typed + new membership relation, authored title preserved.
        root = await repository.get_graph(BASE_URL)
        rs = URIRef(BASE_URL)
        assert (rs, RDF.type, fdp_point_class) in root
        assert (rs, RDF.type, URIRef(expander.schema_iri("fdp:Repository"))) not in root
        assert (rs, LDP.hasMemberRelation, serves_metadata) in root
        assert (rs, LDP.hasMemberRelation, URIRef("http://www.w3.org/ns/dcat#catalog")) not in root
        assert (rs, RDF.type, LDP.DirectContainer) in root

        # 2. The rebuilt cache has a single, correct root.
        cache = await build_cache_from_repository(adapter, base_url=str(settings.base_url))
        assert cache.root() is not None
        assert cache.root().name == "FAIRDataPoint"

        # 3. The authored member survived untouched.
        member = await repository.get_graph(catalog_iri)
        assert (URIRef(catalog_iri), DCT.title, Literal("Authored catalog")) in member

        # 4. The new modular catalog schema is composed (references a base via sh:node).
        catalog_shape = await repository.get_graph(expander.schema_storage_iri("dcat:Catalog"))
        assert any(catalog_shape.objects(None, URIRef("http://www.w3.org/ns/shacl#node")))

        # 5. Idempotent: a second pass changes nothing.
        again = await migrate_to_modular_profile(
            modular, repository=repository, adapter=adapter, settings=settings
        )
        assert again.changed is False
        assert again.root_already_current is True
