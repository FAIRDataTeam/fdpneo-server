"""End-to-end metadata publication lifecycle against Oxigraph + Postgres (ADR-0010).

Drives the whole HTTP stack via :func:`fdp.main.create_app` — real triple
store, Postgres-backed PDP, the LDP + SPARQL + state-transition routers — with
the request context injected through a ``current_context`` override.

Proves the Phase-12 promises:

1. Bootstrap-seeded records (the root Repository) are PUBLISHED — anonymously
   readable from the first request.
2. A record created through the LDP layer is DRAFT: hidden (404) from anonymous
   and from a non-owner, visible to its owner (ODRL ``modify``).
3. ``POST /{record}/state`` publishes it (owner-authorized); it then becomes
   anonymously readable, and ``ARCHIVED`` hides it again.
4. The SPARQL projection never exposes a draft graph to anonymous, even though
   its Offer permits anonymous read.

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

pytestmark = pytest.mark.integration


# --- bundle ----------------------------------------------------------------

PROFILE_MANIFEST = """\
apiVersion: fdp/v1
kind: DeploymentProfile
metadata:
  name: lifecycle-test
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

# Offer permits anonymous read AND modify, so even anonymous *could* write —
# the state gate is what still hides drafts from anonymous regardless of ODRL.
OFFER_TTL = """\
@prefix odrl: <http://www.w3.org/ns/odrl/2/> .
<http://example.org/offers/public>
    a odrl:Offer ;
    odrl:permission
        [ a odrl:Permission ; odrl:action odrl:read ] ,
        [ a odrl:Permission ; odrl:action odrl:modify ] .
"""


# --- containers ------------------------------------------------------------


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


# --- auth-context helper ---------------------------------------------------


def _user() -> RequestContext:
    """An authenticated, non-admin user. Holds ODRL modify via the public offer."""
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


_CATALOG_BODY = (
    "@prefix dcat: <http://www.w3.org/ns/dcat#> .\n"
    "@prefix dct:  <http://purl.org/dc/terms/> .\n"
    '<> a dcat:Catalog ; dct:title "{title}" .\n'
)


def _put_catalog(c: _Client, slug: str, title: str) -> str:
    """PUT a catalog record (created DRAFT) and return its IRI."""
    iri = f"{BASE_URL}/catalog/{slug}"
    resp = c.as_user().put(
        f"/catalog/{slug}",
        content=_CATALOG_BODY.format(title=title),
        headers={"Content-Type": "text/turtle"},
    )
    assert resp.status_code == 201, resp.text
    return iri


def _sparql_titles_anon(c: _Client) -> set[str]:
    q = "SELECT ?t WHERE { GRAPH ?g { ?s <http://purl.org/dc/terms/title> ?t } }"
    resp = c.as_anonymous().get(
        "/sparql",
        params={"query": q},
        headers={"Accept": "application/sparql-results+json"},
    )
    assert resp.status_code == 200, resp.text
    payload: dict[str, Any] = json.loads(resp.content)
    return {b["t"]["value"] for b in payload.get("results", {}).get("bindings", []) if "t" in b}


# --- tests -----------------------------------------------------------------


def test_root_repository_is_published_and_anonymously_readable(app_env: None) -> None:
    c = _make_client()
    with c.http:
        resp = c.as_anonymous().get("/", headers={"Accept": "text/turtle"})
        assert resp.status_code == 200, resp.text


def test_draft_hidden_then_published_then_archived(app_env: None) -> None:
    c = _make_client()
    with c.http:
        _put_catalog(c, "cat1", "Cat One")

        # DRAFT: hidden from anonymous (404, not 403 — existence doesn't leak)…
        assert (
            c.as_anonymous().get("/catalog/cat1", headers={"Accept": "text/turtle"}).status_code
            == 404
        )
        # …but visible to the owner (holds ODRL modify).
        assert (
            c.as_user().get("/catalog/cat1", headers={"Accept": "text/turtle"}).status_code == 200
        )

        # Publish (owner-authorized).
        pub = c.as_user().post("/catalog/cat1/state", json={"to": "PUBLISHED"})
        assert pub.status_code == 200, pub.text
        assert pub.json()["to_state"] == "PUBLISHED"

        # Now anonymous can read it.
        assert (
            c.as_anonymous().get("/catalog/cat1", headers={"Accept": "text/turtle"}).status_code
            == 200
        )

        # Archive → hidden from anonymous again.
        arch = c.as_user().post("/catalog/cat1/state", json={"to": "ARCHIVED"})
        assert arch.status_code == 200, arch.text
        assert (
            c.as_anonymous().get("/catalog/cat1", headers={"Accept": "text/turtle"}).status_code
            == 404
        )


def test_anonymous_transition_is_unauthenticated(app_env: None) -> None:
    c = _make_client()
    with c.http:
        _put_catalog(c, "cat2", "Cat Two")
        resp = c.as_anonymous().post("/catalog/cat2/state", json={"to": "PUBLISHED"})
        assert resp.status_code == 401


def test_disallowed_transition_conflicts(app_env: None) -> None:
    c = _make_client()
    with c.http:
        _put_catalog(c, "cat3", "Cat Three")
        # DRAFT -> ARCHIVED is not in the state machine.
        resp = c.as_user().post("/catalog/cat3/state", json={"to": "ARCHIVED"})
        assert resp.status_code == 409, resp.text


def test_sparql_projection_excludes_drafts_for_anonymous(app_env: None) -> None:
    c = _make_client()
    with c.http:
        _put_catalog(c, "pubcat", "Published Cat")
        _put_catalog(c, "draftcat", "Secret Draft Cat")
        # Publish only the first.
        assert (
            c.as_user().post("/catalog/pubcat/state", json={"to": "PUBLISHED"}).status_code == 200
        )

        # Touch both as anonymous so the ODRL read decision is cached for each
        # (authorized_graphs is cache-bounded). The draft GET 404s at the state
        # gate but still caches the *read* permit beneath it — so the draft is
        # in the ODRL read set yet must be filtered out of the projection.
        assert (
            c.as_anonymous().get("/catalog/pubcat", headers={"Accept": "text/turtle"}).status_code
            == 200
        )
        assert (
            c.as_anonymous().get("/catalog/draftcat", headers={"Accept": "text/turtle"}).status_code
            == 404
        )

        titles = _sparql_titles_anon(c)
        # The security property: the draft's title is never projected to anon,
        # even though the Offer permits anonymous read on its graph.
        assert "Secret Draft Cat" not in titles
        assert "Published Cat" in titles
