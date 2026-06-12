"""End-to-end instance lookup over Oxigraph + Postgres.

Proves ``GET /fdp-api/instances?class=<C>`` enumerates records typed ``C`` and
gates them by read visibility: an anonymous caller sees published instances only,
while a curator sees their drafts too. Also checks the ``q`` filter and the
``X-FDP-Page-*`` headers.

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

REPO_ROOT = Path(__file__).resolve().parents[3]
OXIGRAPH_PORT = 7878
BASE_URL = "http://testserver"
CATALOG = "http://www.w3.org/ns/dcat#Catalog"

pytestmark = pytest.mark.integration


PROFILE_MANIFEST = """\
apiVersion: fdp/v1
kind: DeploymentProfile
metadata:
  name: instances-test
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

REPOSITORY_SHAPE = """\
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix fdp: <https://w3id.org/fdp/o#> .
@prefix dct: <http://purl.org/dc/terms/> .
<https://w3id.org/fdp/o#Repository> a sh:NodeShape ; sh:targetClass fdp:Repository ;
    sh:property [ sh:path dct:title ; sh:minCount 1 ] .
"""

CATALOG_SHAPE = """\
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix dcat: <http://www.w3.org/ns/dcat#> .
@prefix dct: <http://purl.org/dc/terms/> .
<http://www.w3.org/ns/dcat#Catalog> a sh:NodeShape ; sh:targetClass dcat:Catalog ;
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
    (root / "schemas" / "repository.ttl").write_text(REPOSITORY_SHAPE, encoding="utf-8")
    (root / "schemas" / "catalog.ttl").write_text(CATALOG_SHAPE, encoding="utf-8")
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


def _admin() -> RequestContext:
    return RequestContext(
        subject="http://idp.local/realms/fdp#admin",
        roles=frozenset({"admin", "steward"}),
        trace_id="it",
    )


class _Client:
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
    from fdp.identity.deps import current_context
    from fdp.main import create_app

    app = create_app()
    holder: dict[str, RequestContext] = {"ctx": RequestContext.anonymous(trace_id="it")}
    app.dependency_overrides[current_context] = lambda: holder["ctx"]
    return _Client(TestClient(app, base_url=BASE_URL), holder)


def _catalog_ttl(iri: str, title: str) -> str:
    return (
        "@prefix dcat: <http://www.w3.org/ns/dcat#> ."
        "@prefix dct: <http://purl.org/dc/terms/> ."
        f"<{iri}> a dcat:Catalog ; dct:title {title!r} ; dct:isPartOf <{BASE_URL}> ."
    )


def test_instances_are_visibility_gated(app_env: None) -> None:
    c = _make_client()
    c1, c2 = f"{BASE_URL}/catalog/c1", f"{BASE_URL}/catalog/c2"
    with c.http:
        # Author two catalogs (created DRAFT), publish only c1.
        assert (
            c.as_admin()
            .put(
                "/catalog/c1",
                content=_catalog_ttl(c1, "Alpha Catalog"),
                headers={"Content-Type": "text/turtle"},
            )
            .status_code
            == 201
        )
        assert (
            c.as_admin()
            .put(
                "/catalog/c2",
                content=_catalog_ttl(c2, "Beta Catalog"),
                headers={"Content-Type": "text/turtle"},
            )
            .status_code
            == 201
        )
        assert (
            c.as_admin().post("/fdp-api/catalog/c1/state", json={"to": "PUBLISHED"}).status_code
            == 200
        )

        # Anonymous: only the published catalog is returned; the draft is dropped
        # by the per-item state gate. (X-FDP-Page-Total is the pre-gate candidate
        # count — 2 — consistent with /page; the page itself is gated.)
        resp = c.as_anonymous().get("/fdp-api/instances", params={"class": CATALOG})
        assert resp.status_code == 200
        body = resp.json()
        assert [i["iri"] for i in body["items"]] == [c1]
        assert body["items"][0] == {"iri": c1, "label": "Alpha Catalog", "type": CATALOG}
        assert resp.headers["X-FDP-Page-Total"] == "2"

        # Admin (curator): sees the draft too.
        admin_items = (
            c.as_admin().get("/fdp-api/instances", params={"class": CATALOG}).json()["items"]
        )
        assert {i["iri"] for i in admin_items} == {c1, c2}

        # q filter (case-insensitive) narrows to Alpha.
        filtered = (
            c.as_admin()
            .get("/fdp-api/instances", params={"class": CATALOG, "q": "alpha"})
            .json()["items"]
        )
        assert [i["iri"] for i in filtered] == [c1]


def test_unknown_class_returns_empty(app_env: None) -> None:
    c = _make_client()
    with c.http:
        resp = c.as_anonymous().get(
            "/fdp-api/instances", params={"class": "http://example.org/Nope"}
        )
        assert resp.status_code == 200
        assert resp.json() == {"items": []}
        assert resp.headers["X-FDP-Page-Total"] == "0"
