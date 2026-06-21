"""End-to-end persistent identifiers (ADR-0014) over a real Oxigraph + Postgres.

Drives the whole HTTP stack via :func:`fdp.main.create_app` with a distinct
``IDENTIFIER_BASE`` (a W3ID-style URL) and ``BASE_URL`` (the serving origin the
TestClient talks to). Proves:

1. A record created on the serving host is minted + stored under the canonical
   ``IDENTIFIER_BASE`` IRI (not the serving host), and resolves back through the
   serving path — i.e. in-server canonicalization works against a real store.
2. The dual identifier model: a foreign subject in the body is rebound to the
   canonical IRI and preserved as ``owl:sameAs``.

Requires Docker (testcontainers). Marked ``integration``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from rdflib import Graph, URIRef
from rdflib.namespace import RDF
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy
from testcontainers.postgres import PostgresContainer

from fdp.shared.context import RequestContext
from fdp.shared.namespaces import DCAT, DCT, OWL

REPO_ROOT = Path(__file__).resolve().parents[3]
OXIGRAPH_PORT = 7878
SERVING_URL = "http://testserver"
IDENTIFIER_BASE = "https://w3id.org/it-fdp"

pytestmark = pytest.mark.integration


PROFILE_MANIFEST = """\
apiVersion: fdp/v1
kind: DeploymentProfile
metadata:
  name: pid-test
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
    a sh:NodeShape ;
    sh:targetClass fdp:Repository ;
    sh:property [ sh:path dct:title ; sh:minCount 1 ] .
"""

CATALOG_SHAPE_TTL = """\
@prefix sh:   <http://www.w3.org/ns/shacl#> .
@prefix dcat: <http://www.w3.org/ns/dcat#> .
@prefix dct:  <http://purl.org/dc/terms/> .

<http://www.w3.org/ns/dcat#Catalog>
    a sh:NodeShape ;
    sh:targetClass dcat:Catalog ;
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
        "BASE_URL": SERVING_URL,
        "IDENTIFIER_BASE": IDENTIFIER_BASE,
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


def _admin() -> RequestContext:
    return RequestContext(
        subject="http://idp.local/realms/fdp#admin",
        roles=frozenset({"admin", "steward"}),
        trace_id="it",
    )


def _make_client() -> TestClient:
    from fdp.identity.deps import current_context
    from fdp.main import create_app

    app = create_app()
    app.dependency_overrides[current_context] = _admin
    return TestClient(app, base_url=SERVING_URL)


def _graph(turtle: str) -> Graph:
    g = Graph()
    g.parse(data=turtle, format="turtle")
    return g


def test_config_exposes_distinct_identifier_and_serving_bases(app_env: None) -> None:
    with _make_client() as client:
        body = client.get("/fdp-api/config").json()
    assert body["fdp_url"] == IDENTIFIER_BASE
    assert body["serving_url"] == SERVING_URL


def test_record_minted_under_identifier_base_resolves_via_serving_host(app_env: None) -> None:
    canonical = f"{IDENTIFIER_BASE}/catalog/c1"
    serving_iri = f"{SERVING_URL}/catalog/c1"
    body = '<> a <http://www.w3.org/ns/dcat#Catalog> ; <http://purl.org/dc/terms/title> "C1" .'

    with _make_client() as client:
        # Created on the serving host…
        created = client.put("/catalog/c1", content=body, headers={"content-type": "text/turtle"})
        assert created.status_code == 201
        # …but the canonical identity is the IDENTIFIER_BASE IRI.
        assert created.headers["location"] == canonical

        # And it resolves back through the serving path.
        got = client.get("/catalog/c1", headers={"accept": "text/turtle"})
        assert got.status_code == 200

    g = _graph(got.text)
    assert (URIRef(canonical), DCT.title, None) in [(s, p, None) for s, p, _ in g]
    assert (URIRef(canonical), RDF.type, DCAT.Catalog) in g
    # The serving-host IRI is never a subject — identity is the canonical PID.
    assert URIRef(serving_iri) not in set(g.subjects())


def test_foreign_identifier_preserved_as_sameas(app_env: None) -> None:
    canonical = f"{IDENTIFIER_BASE}/catalog/c2"
    foreign = "https://doi.org/10.1234/brought-along"
    body = (
        f"<{foreign}> a <http://www.w3.org/ns/dcat#Catalog> ; "
        '<http://purl.org/dc/terms/title> "C2" .'
    )

    with _make_client() as client:
        created = client.put("/catalog/c2", content=body, headers={"content-type": "text/turtle"})
        assert created.status_code == 201
        got = client.get("/catalog/c2", headers={"accept": "text/turtle"})
        assert got.status_code == 200

    g = _graph(got.text)
    canon = URIRef(canonical)
    # Rebound to canonical; foreign kept as a cross-reference, never the subject.
    assert (canon, RDF.type, DCAT.Catalog) in g
    assert (canon, OWL.sameAs, URIRef(foreign)) in g
    assert URIRef(foreign) not in set(g.subjects())


def test_identifier_is_immutable_across_a_content_edit(app_env: None) -> None:
    """A record's persistent identifier must not change when it is edited (ADR-0014).

    Conformance property: the canonical IRI is the record's stable identity, so a
    content update keeps it — the serving-host IRI never becomes the subject.
    """
    canonical = f"{IDENTIFIER_BASE}/catalog/c3"
    serving_iri = f"{SERVING_URL}/catalog/c3"
    create = '<> a <http://www.w3.org/ns/dcat#Catalog> ; <http://purl.org/dc/terms/title> "v1" .'
    edit = '<> a <http://www.w3.org/ns/dcat#Catalog> ; <http://purl.org/dc/terms/title> "v2" .'

    with _make_client() as client:
        created = client.put("/catalog/c3", content=create, headers={"content-type": "text/turtle"})
        assert created.status_code == 201
        assert created.headers["location"] == canonical

        # Re-PUT updates the existing record (If-Match from the current ETag).
        etag = client.get("/catalog/c3").headers["etag"]
        edited = client.put(
            "/catalog/c3",
            content=edit,
            headers={"content-type": "text/turtle", "if-match": etag},
        )
        assert edited.status_code == 200  # PUT to an existing resource → update

        got = client.get("/catalog/c3", headers={"accept": "text/turtle"})
        assert got.status_code == 200

    g = _graph(got.text)
    canon = URIRef(canonical)
    # The edit took effect…
    assert {str(o) for o in g.objects(canon, DCT.title)} == {"v2"}
    # …but the identifier is unchanged: canonical IRI is still the subject and the
    # serving-host IRI never becomes one.
    assert canon in set(g.subjects())
    assert URIRef(serving_iri) not in set(g.subjects())


def test_sameas_survives_a_content_edit(app_env: None) -> None:
    """The dual-identifier ``owl:sameAs`` link is preserved across a re-edit."""
    canonical = f"{IDENTIFIER_BASE}/catalog/c4"
    foreign = "https://doi.org/10.1234/kept"
    create = (
        f"<{foreign}> a <http://www.w3.org/ns/dcat#Catalog> ; "
        '<http://purl.org/dc/terms/title> "v1" .'
    )
    edit = (
        f"<{foreign}> a <http://www.w3.org/ns/dcat#Catalog> ; "
        '<http://purl.org/dc/terms/title> "v2" .'
    )

    with _make_client() as client:
        created = client.put("/catalog/c4", content=create, headers={"content-type": "text/turtle"})
        assert created.status_code == 201
        etag = client.get("/catalog/c4").headers["etag"]
        edited = client.put(
            "/catalog/c4",
            content=edit,
            headers={"content-type": "text/turtle", "if-match": etag},
        )
        assert edited.status_code == 200  # PUT to an existing resource → update
        got = client.get("/catalog/c4", headers={"accept": "text/turtle"})
        assert got.status_code == 200

    g = _graph(got.text)
    canon = URIRef(canonical)
    assert {str(o) for o in g.objects(canon, DCT.title)} == {"v2"}
    assert (canon, OWL.sameAs, URIRef(foreign)) in g
    assert URIRef(foreign) not in set(g.subjects())
