"""End-to-end forward containment links over Oxigraph + Postgres.

Drives the whole HTTP stack via :func:`fdpneo_server.main.create_app`. Proves that when a
child is created via ``PUT`` with ``dct:isPartOf``, the server writes the
forward membership links onto the PARENT (``ldp:contains`` + the typed DCAT
relation), keeps both directions in agreement, bumps the parent's ETag, and
strips the links again on delete — so a DCAT consumer can traverse
``repository → dcat:catalog → catalog → dcat:dataset → dataset`` without relying
on the ``dct:isPartOf`` back-link alone.

Requires Docker (testcontainers). Marked ``integration``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from importlib.resources import files
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from rdflib import Graph, URIRef
from testcontainers.community.postgres import PostgresContainer
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

from fdpneo_server.shared.context import RequestContext
from fdpneo_server.shared.namespaces import DCAT, DCT, LDP

OXIGRAPH_PORT = 7878
BASE_URL = "http://testserver"

pytestmark = pytest.mark.integration


PROFILE_MANIFEST = """\
apiVersion: fdp/v1
kind: DeploymentProfile
metadata:
  name: containment-test
  version: 0.1.0
schemas:
  - id: fdp:Repository
    path: schemas/repository.ttl
  - id: dcat:Catalog
    path: schemas/catalog.ttl
  - id: dcat:Dataset
    path: schemas/dataset.ttl
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
    children:
      - relationUri: dcat:dataset
        target: dataset
        title: Datasets
  - urlPrefix: dataset
    name: Dataset
    schema: dcat:Dataset
"""


def _shape(class_iri: str, prefixes: str) -> str:
    return f"""\
@prefix sh:   <http://www.w3.org/ns/shacl#> .
@prefix dct:  <http://purl.org/dc/terms/> .
{prefixes}
<{class_iri}>
    a sh:NodeShape ;
    sh:targetClass <{class_iri}> ;
    sh:property [ sh:path dct:title ; sh:minCount 1 ] .
"""


REPOSITORY_SHAPE = _shape(
    "https://w3id.org/fdp/fdp-o#Repository", "@prefix fdp: <https://w3id.org/fdp/fdp-o#> ."
)
CATALOG_SHAPE = _shape(
    "http://www.w3.org/ns/dcat#Catalog", "@prefix dcat: <http://www.w3.org/ns/dcat#> ."
)
DATASET_SHAPE = _shape(
    "http://www.w3.org/ns/dcat#Dataset", "@prefix dcat: <http://www.w3.org/ns/dcat#> ."
)

OFFER_TTL = """\
@prefix odrl: <http://www.w3.org/ns/odrl/2/> .
<http://example.org/offers/public>
    a odrl:Offer ;
    odrl:permission
        [ a odrl:Permission ; odrl:action odrl:read ] ,
        [ a odrl:Permission ; odrl:action odrl:modify ] ,
        [ a odrl:Permission ; odrl:action odrl:delete ] .
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
    (root / "schemas" / "repository.ttl").write_text(REPOSITORY_SHAPE, encoding="utf-8")
    (root / "schemas" / "catalog.ttl").write_text(CATALOG_SHAPE, encoding="utf-8")
    (root / "schemas" / "dataset.ttl").write_text(DATASET_SHAPE, encoding="utf-8")
    (root / "offers").mkdir()
    (root / "offers" / "public.ttl").write_text(OFFER_TTL, encoding="utf-8")
    return root


@pytest.fixture
def app_env(
    postgres_container: PostgresContainer,
    oxigraph_container: DockerContainer,
    bundle: Path,
) -> Iterator[None]:
    from fdpneo_server.config import get_settings

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
    config = Config(str(files("fdpneo_server") / "alembic.ini"))
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


def _admin() -> RequestContext:
    return RequestContext(
        subject="http://idp.local/realms/fdp#admin",
        roles=frozenset({"admin", "steward"}),
        trace_id="it",
    )


def _make_client() -> tuple[TestClient, dict[str, RequestContext]]:
    from fdpneo_server.identity.deps import current_context
    from fdpneo_server.main import create_app

    app = create_app()
    holder: dict[str, RequestContext] = {"ctx": _admin()}
    app.dependency_overrides[current_context] = lambda: holder["ctx"]
    return TestClient(app, base_url=BASE_URL), holder


def _record_ttl(iri: str, type_iri: str, title: str, parent: str) -> str:
    return (
        f"@prefix dcat: <http://www.w3.org/ns/dcat#> ."
        f"@prefix dct: <http://purl.org/dc/terms/> ."
        f"<{iri}> a <{type_iri}> ; dct:title {title!r} ; dct:isPartOf <{parent}> ."
    )


def _graph_of(client: TestClient, path: str) -> Graph:
    resp = client.get(path, headers={"Accept": "text/turtle"})
    assert resp.status_code == 200, resp.text
    g = Graph()
    g.parse(data=resp.text, format="turtle")
    return g


def test_create_writes_forward_links_and_delete_removes_them(app_env: None) -> None:
    client, _ = _make_client()
    root = URIRef(BASE_URL)
    catalog = URIRef(f"{BASE_URL}/catalog/c1")
    dataset = URIRef(f"{BASE_URL}/dataset/d1")

    with client:
        # Capture the root's ETag before the child exists.
        before = client.get("/", headers={"Accept": "text/turtle"})
        assert before.status_code == 200
        root_etag_before = before.headers["ETag"]

        # Create a catalog under the root.
        created = client.put(
            "/catalog/c1",
            content=_record_ttl(str(catalog), "http://www.w3.org/ns/dcat#Catalog", "C1", BASE_URL),
            headers={"Content-Type": "text/turtle"},
        )
        assert created.status_code == 201, created.text

        # Forward links now live on the parent (root) graph.
        root_graph = _graph_of(client, "/")
        assert (root, LDP.contains, catalog) in root_graph
        assert (root, DCAT.catalog, catalog) in root_graph
        # The child keeps its back-link — both directions agree.
        child_graph = _graph_of(client, "/catalog/c1")
        assert (catalog, DCT.isPartOf, root) in child_graph
        # The parent record changed → its ETag changed (meta/dct:modified refreshed).
        assert (
            client.get("/", headers={"Accept": "text/turtle"}).headers["ETag"] != root_etag_before
        )

        # Two levels down: dataset under the catalog → catalog gains the links.
        ds = client.put(
            "/dataset/d1",
            content=_record_ttl(
                str(dataset), "http://www.w3.org/ns/dcat#Dataset", "D1", str(catalog)
            ),
            headers={"Content-Type": "text/turtle"},
        )
        assert ds.status_code == 201, ds.text
        catalog_graph = _graph_of(client, "/catalog/c1")
        assert (catalog, LDP.contains, dataset) in catalog_graph
        assert (catalog, DCAT.dataset, dataset) in catalog_graph

        # Delete the catalog → the root's forward links to it are stripped.
        etag = client.get("/catalog/c1", headers={"Accept": "text/turtle"}).headers["ETag"]
        deleted = client.delete("/catalog/c1", headers={"If-Match": etag})
        assert deleted.status_code == 204, deleted.text
        root_after = _graph_of(client, "/")
        assert (root, LDP.contains, catalog) not in root_after
        assert (root, DCAT.catalog, catalog) not in root_after
