"""End-to-end: profile schemas are first-class editable schemas (task 10.5).

Drives the whole HTTP stack via :func:`fdp.main.create_app` against a real
Oxigraph + Postgres, with the request context injected through a
``current_context`` override (no OIDC needed). A small profile is auto-applied
on startup.

Proves what task 10.5 promises:

1. After apply, the profile's SHACL schemas are listed by ``GET /fdp-api/schemas``
   (the namespace they now live in) and are fetchable at their storage IRI —
   they are no longer hidden at vocabulary IRIs.
2. The FDP root schema (the root resource definition's schema) is flagged
   ``deletable: false``, can be **edited** (``PUT`` → 200) but not **deleted**
   (``DELETE`` → 403 ``fdp.schema_protected``).
3. A non-root profile schema is ``deletable: true`` but still guarded by the
   resource-definition reference check (``DELETE`` → 409 while a type points at
   it), exactly like a user-published schema.

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

pytestmark = pytest.mark.integration


# --- bundle ---------------------------------------------------------------

PROFILE_MANIFEST = """\
apiVersion: fdp/v1
kind: DeploymentProfile
metadata:
  name: schema-ns-test
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

REPOSITORY_STORAGE_IRI = f"{BASE_URL}/fdp-api/schemas/repository"
CATALOG_STORAGE_IRI = f"{BASE_URL}/fdp-api/schemas/catalog"


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


# --- auth-context helper ---------------------------------------------------


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


# --- tests -----------------------------------------------------------------


def test_profile_schemas_listed_and_fetchable(app_env: None) -> None:
    c = _make_client()
    with c.http:
        # Both profile schemas appear in the schemas namespace (anonymous read).
        listed = c.as_anonymous().get("/fdp-api/schemas").json()["schemas"]
        by_id = {s["id"]: s for s in listed}
        assert set(by_id) == {"repository", "catalog"}

        # The FDP root schema is flagged read-only; the other is deletable.
        assert by_id["repository"]["deletable"] is False
        assert by_id["catalog"]["deletable"] is True
        assert by_id["catalog"]["target_class"] == "http://www.w3.org/ns/dcat#Catalog"

        # The shape is fetchable at its storage IRI (not the vocabulary IRI).
        got = c.as_anonymous().get("/fdp-api/schemas/catalog")
        assert got.status_code == 200
        assert got.headers["content-type"].startswith("text/turtle")
        assert "NodeShape" in got.text


def test_root_schema_editable_not_deletable(app_env: None) -> None:
    c = _make_client()
    with c.http:
        # DELETE the FDP root schema → 403 fdp.schema_protected.
        denied = c.as_admin().delete("/fdp-api/schemas/repository")
        assert denied.status_code == 403, denied.text
        assert denied.json()["code"] == "fdp.schema_protected"

        # …but editing it in place is allowed.
        edited = c.as_admin().put(
            "/fdp-api/schemas/repository",
            content=REPOSITORY_SHAPE_TTL,
            headers={"Content-Type": "text/turtle"},
        )
        assert edited.status_code == 200, edited.text
        assert edited.json()["deletable"] is False

        # A non-root profile schema is deletable in principle, but the
        # resource-definition reference guard still applies (Catalog type points
        # at it via ldp:constrainedBy) → 409, just like a user schema.
        referenced = c.as_admin().delete("/fdp-api/schemas/catalog")
        assert referenced.status_code == 409, referenced.text
