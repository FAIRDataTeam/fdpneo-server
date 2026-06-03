"""End-to-end search wiring against Oxigraph + Postgres (Phase 7).

Proves the event-driven path the unit/repository tests can't: creating a record
through the LDP API indexes it via the bus subscriber, the visibility gate
hides a draft from anonymous search, and publishing it (a state transition)
re-indexes it so anonymous search then finds it.

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
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy
from testcontainers.postgres import PostgresContainer

from fdp.shared.context import RequestContext

REPO_ROOT = Path(__file__).resolve().parents[4]
OXIGRAPH_PORT = 7878
BASE_URL = "http://testserver"

pytestmark = pytest.mark.integration

PROFILE_MANIFEST = """\
apiVersion: fdp/v1
kind: DeploymentProfile
metadata:
  name: search-e2e
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
  - urlPrefix: catalog
    name: Catalog
    schema: dcat:Catalog
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


def _user() -> RequestContext:
    return RequestContext(
        subject="http://idp.local/realms/fdp#alice", roles=frozenset(), trace_id="it"
    )


class _Client:
    def __init__(self, client: TestClient, holder: dict[str, RequestContext]) -> None:
        self.http = client
        self._holder = holder

    def as_user(self) -> TestClient:
        self._holder["ctx"] = _user()
        return self.http

    def as_anonymous(self) -> TestClient:
        self._holder["ctx"] = RequestContext.anonymous(trace_id="it")
        return self.http


def _make_client() -> _Client:
    from fdp.identity.deps import current_context
    from fdp.main import create_app

    app = create_app()
    holder: dict[str, RequestContext] = {"ctx": RequestContext.anonymous(trace_id="it")}
    app.dependency_overrides[current_context] = lambda: holder["ctx"]
    return _Client(TestClient(app, base_url=BASE_URL), holder)


_BODY = (
    "@prefix dcat: <http://www.w3.org/ns/dcat#> .\n"
    "@prefix dct:  <http://purl.org/dc/terms/> .\n"
    '<> a dcat:Catalog ; dct:title "Zebrafish Genome Catalog" .\n'
)


def _search_anon(c: _Client, term: str) -> set[str]:
    resp = c.as_anonymous().post("/search", json={"query": term})
    assert resp.status_code == 200, resp.text
    return {item["recordIri"] for item in resp.json()["items"]}


def test_create_indexes_draft_hidden_then_published_visible(app_env: None) -> None:
    c = _make_client()
    with c.http:
        # Create through the LDP API → RecordCreated → indexer upserts the row.
        put = c.as_user().put(
            "/catalog/zf",
            content=_BODY,
            headers={"Content-Type": "text/turtle"},
        )
        assert put.status_code == 201, put.text
        iri = f"{BASE_URL}/catalog/zf"

        # DRAFT → anonymous search must not surface it (even though its offer
        # permits anonymous read).
        assert iri not in _search_anon(c, "zebrafish")

        # Publish → RecordStateChanged → re-index → now anonymously searchable.
        pub = c.as_user().post("/catalog/zf/state", json={"to": "PUBLISHED"})
        assert pub.status_code == 200, pub.text
        assert iri in _search_anon(c, "zebrafish")


def test_facets_returned_for_published(app_env: None) -> None:
    c = _make_client()
    with c.http:
        c.as_user().put("/catalog/zf", content=_BODY, headers={"Content-Type": "text/turtle"})
        c.as_user().post("/catalog/zf/state", json={"to": "PUBLISHED"})
        resp = c.as_anonymous().post("/search", json={})
        assert resp.status_code == 200
        facets = resp.json()["facets"]
        # Built-in dimensions present (no 9.4 filters configured in this bundle).
        assert "type" in facets and "license" in facets
