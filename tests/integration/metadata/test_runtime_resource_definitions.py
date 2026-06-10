"""End-to-end runtime resource-definition admin against Oxigraph + Postgres (ADR-0009).

Drives the *whole* HTTP stack via :func:`fdp.main.create_app` — real triple
store, Postgres-backed PDP/authz cache, SHACL validator, LDP + SPARQL + the
resource-definition admin routers — with the request context injected through
a ``current_context`` dependency override (so no OIDC/JWT is needed). The
profile is auto-applied on startup from a tiny bundle.

Proves the three things the runtime-endpoint-generation feature promises:

1. An admin can register a new type at runtime and its endpoints + OpenAPI
   paths light up with no restart; a child link can be added to an existing
   type (Catalog → the new Ontology type) by replacing it.
2. The new type survives a restart, because the cache is rebuilt from the RD
   records in the triple store — not the profile manifest.
3. Internal graphs (resource-definition records, meta, audit) never appear in
   a public SPARQL query, even though RD records are REST-readable.

Requires Docker (testcontainers). Marked ``integration``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy
from testcontainers.postgres import PostgresContainer

from fdp.shared.context import RequestContext

REPO_ROOT = Path(__file__).resolve().parents[3]
OXIGRAPH_PORT = 7878
BASE_URL = "http://testserver"
ONTOLOGY_SHAPE_IRI = f"{BASE_URL}/shapes/Ontology"

pytestmark = pytest.mark.integration


# --- bundle ---------------------------------------------------------------

PROFILE_MANIFEST = """\
apiVersion: fdp/v1
kind: DeploymentProfile
metadata:
  name: runtime-rd-test
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
    children:
      - relationUri: dcat:catalog
        target: catalog
        title: Catalogs
  - urlPrefix: catalog
    name: Catalog
    schema: dcat:Catalog
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

# Offer permits anonymous read AND modify (unconstrained) so the admin context
# can publish the runtime shape and create records through the LDP layer.
OFFER_TTL = """\
@prefix odrl: <http://www.w3.org/ns/odrl/2/> .
<http://example.org/offers/public>
    a odrl:Offer ;
    odrl:permission
        [ a odrl:Permission ; odrl:action odrl:read ] ,
        [ a odrl:Permission ; odrl:action odrl:modify ] .
"""

ONTOLOGY_SHAPE_TTL = f"""\
@prefix sh:   <http://www.w3.org/ns/shacl#> .
@prefix owl:  <http://www.w3.org/2002/07/owl#> .
@prefix dct:  <http://purl.org/dc/terms/> .

<{ONTOLOGY_SHAPE_IRI}>
    a sh:NodeShape ;
    sh:targetClass owl:Ontology ;
    sh:property [ sh:path dct:title ; sh:minCount 1 ] .
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
def bundle(tmp_path: Path) -> Path:
    root = tmp_path / "profile"
    root.mkdir()
    (root / "profile.yaml").write_text(PROFILE_MANIFEST, encoding="utf-8")
    (root / "schemas").mkdir()
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
    """Configure the environment ``create_app`` reads, run migrations, and clean up."""
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
        # Settings has no env_prefix, so base_url reads the bare BASE_URL var.
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


# --- auth-context helper ---------------------------------------------------


def _admin() -> RequestContext:
    return RequestContext(
        subject="http://idp.local/realms/fdp#admin",
        roles=frozenset({"admin", "steward"}),
        trace_id="it",
    )


class _Client:
    """A TestClient whose injected request context can be flipped per call."""

    def __init__(self, client: TestClient, holder: dict[str, RequestContext]) -> None:
        self.http = client
        self._holder = holder

    def as_admin(self) -> TestClient:
        self._holder["ctx"] = _admin()
        return self.http

    def as_anonymous(self) -> TestClient:
        self._holder["ctx"] = RequestContext.anonymous(trace_id="it")
        return self.http


def _make_client() -> _Client:
    """Build a fresh app + TestClient sharing the configured containers.

    Each call runs ``create_app`` (and, on context-enter, the lifespan
    auto-bootstrap), so calling it twice simulates a server restart against
    the same stores.
    """
    from fdp.identity.deps import current_context
    from fdp.main import create_app

    app = create_app()
    holder: dict[str, RequestContext] = {"ctx": RequestContext.anonymous(trace_id="it")}
    app.dependency_overrides[current_context] = lambda: holder["ctx"]
    return _Client(TestClient(app, base_url=BASE_URL), holder)


def _ontology_definition() -> dict[str, Any]:
    return {"urlPrefix": "ontology", "name": "Ontology", "schema": ONTOLOGY_SHAPE_IRI}


def _publish_ontology_shape(c: _Client) -> None:
    resp = c.as_admin().put(
        "/shapes/Ontology",
        content=ONTOLOGY_SHAPE_TTL,
        headers={"Content-Type": "text/turtle"},
    )
    assert resp.status_code in (200, 201), resp.text


# --- tests -----------------------------------------------------------------


def test_runtime_definition_lights_up_endpoints_and_openapi(app_env: None) -> None:
    c = _make_client()
    with c.http:
        # Seed types from the profile are present.
        listed = c.as_anonymous().get("/fdp-api/resource-definitions").json()["definitions"]
        assert {d["slug"] for d in listed} == {"repository", "catalog"}

        # Two-step: publish the Ontology shape, then register the type.
        _publish_ontology_shape(c)
        created = c.as_admin().post("/fdp-api/resource-definitions", json=_ontology_definition())
        assert created.status_code == 201, created.text
        assert created.json()["slug"] == "ontology"

        # The new type is in the public catalog and the OpenAPI surface — no restart.
        listed = c.as_anonymous().get("/fdp-api/resource-definitions").json()["definitions"]
        assert any(d["slug"] == "ontology" for d in listed)
        paths = c.as_anonymous().get("/fdp-api/openapi.json").json()["paths"]
        assert "/ontology" in paths
        assert "/ontology/{id}" in paths

        # Catalog now also contains Ontology metadata (child link added by replace).
        replaced = c.as_admin().put(
            "/fdp-api/resource-definitions/catalog",
            json={
                "urlPrefix": "catalog",
                "name": "Catalog",
                "schema": "http://www.w3.org/ns/dcat#Catalog",
                "children": [
                    {
                        "relationUri": "http://www.w3.org/ns/dcat#dataset",
                        "target": "ontology",
                        "title": "Ontologies",
                    }
                ],
            },
        )
        assert replaced.status_code == 200, replaced.text
        assert any(ch["target"] == "ontology" for ch in replaced.json()["children"])


def test_create_member_of_runtime_type(app_env: None) -> None:
    c = _make_client()
    with c.http:
        _publish_ontology_shape(c)
        assert (
            c.as_admin()
            .post("/fdp-api/resource-definitions", json=_ontology_definition())
            .status_code
            == 201
        )

        # POST a member to the freshly created typed collection; it is
        # SHACL-validated against the Ontology shape (requires dct:title).
        member = (
            "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
            "@prefix dct: <http://purl.org/dc/terms/> .\n"
            '<> a owl:Ontology ; dct:title "Gene Ontology" .\n'
        )
        created = c.as_admin().post(
            "/ontology",
            content=member,
            headers={"Content-Type": "text/turtle", "Slug": "go"},
        )
        assert created.status_code == 201, created.text
        location = created.headers["Location"]
        assert c.as_admin().get(location, headers={"Accept": "text/turtle"}).status_code == 200


def test_runtime_type_survives_restart(app_env: None) -> None:
    first = _make_client()
    with first.http:
        _publish_ontology_shape(first)
        assert (
            first.as_admin()
            .post("/fdp-api/resource-definitions", json=_ontology_definition())
            .status_code
            == 201
        )

    # "Restart": a brand-new app against the same stores, profile already
    # applied → the cache is rebuilt from the RD records in the triple store.
    second = _make_client()
    with second.http:
        listed = second.as_anonymous().get("/fdp-api/resource-definitions").json()["definitions"]
        assert any(d["slug"] == "ontology" for d in listed), "runtime type did not survive restart"
        assert "/ontology" in second.as_anonymous().get("/fdp-api/openapi.json").json()["paths"]


def test_rd_record_is_rest_readable_but_hidden_from_public_sparql(app_env: None) -> None:
    """The internal-graph isolation invariant (ADR-0009).

    A resource-definition record is readable over REST (it's the public type
    catalog), but its named graph is never in a non-admin's authorized SPARQL
    dataset — so a public query that names it is rejected by the
    information-leakage rule (§9.5) before the store is ever touched, and the
    catch-all projection can never surface it.
    """
    c = _make_client()
    with c.http:
        _publish_ontology_shape(c)
        assert (
            c.as_admin()
            .post("/fdp-api/resource-definitions", json=_ontology_definition())
            .status_code
            == 201
        )
        rd_graph = f"{BASE_URL}/fdp-api/resource-definitions/ontology"

        # REST: the RD record is part of the public catalog.
        assert c.as_anonymous().get("/fdp-api/resource-definitions/ontology").status_code == 200

        # SPARQL: naming the internal RD graph is forbidden for an anonymous
        # caller — it is excluded from the authorized read set, so the
        # rewriter rejects the query (§9.5) rather than projecting it.
        resp = c.as_anonymous().get(
            "/fdp-api/sparql",
            params={"query": f"ASK {{ GRAPH <{rd_graph}> {{ ?s ?p ?o }} }}"},
            headers={"Accept": "application/sparql-results+json"},
        )
        assert resp.status_code == 403, resp.text

        # A scoped public read works: naming the (authorized) root graph
        # returns its data, confirming the projection path is wired end to end.
        # NB: a *multi*-graph projection (unscoped `GRAPH ?g`) is only exercised
        # against a protocol-conformant store — Oxigraph (this test's backend)
        # does not union repeated `named-graph-uri`; see TASKS.md Phase 10.3.
        root_ask = c.as_anonymous().get(
            "/fdp-api/sparql",
            params={
                "query": f"ASK {{ GRAPH <{BASE_URL}> {{ ?s <http://purl.org/dc/terms/title> ?t }} }}"
            },
            headers={"Accept": "application/sparql-results+json"},
        )
        assert root_ask.status_code == 200, root_ask.text
        assert json.loads(root_ask.content)["boolean"] is True
