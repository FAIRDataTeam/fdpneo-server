"""End-to-end test of the modular DCAT 3 + FDP-O default profile (task 15.2).

Applies the *real* ``profiles/default`` bundle over Oxigraph + Postgres and
proves the composed shapes work end to end:

* ``GET /fdp-api/{type}/spec`` returns the merged shape **closure** — a Catalog's
  spec carries the ``dct:title`` property it inherits from Resource (via Dataset)
  plus its own catalog properties, in one response.
* Composition is enforced on write: a Catalog without ``dct:title`` fails the
  constraint inherited from the Resource base; with it, the write succeeds.
* The root is seeded as ``fdp-o:FAIRDataPoint`` and serves over LDP.

Requires Docker (testcontainers). Marked ``integration``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from importlib.resources import files

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from rdflib import Graph, URIRef
from rdflib.namespace import RDF
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy
from testcontainers.postgres import PostgresContainer

from fdpneo_server.metadata.profiles import bundled_default_profile
from fdpneo_server.shared.context import RequestContext
from fdpneo_server.shared.namespaces import DCAT, DCT, SH

DEFAULT_PROFILE = bundled_default_profile()
OXIGRAPH_PORT = 7878
BASE_URL = "http://testserver"
FDP_FAIRDATAPOINT = URIRef("https://w3id.org/fdp/o#FAIRDataPoint")

pytestmark = pytest.mark.integration


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
def app_env(
    postgres_container: PostgresContainer,
    oxigraph_container: DockerContainer,
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
        "FDP_PROFILE_PATH": str(DEFAULT_PROFILE),
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
    from fdpneo_server.identity.deps import current_context
    from fdpneo_server.main import create_app

    app = create_app()
    holder: dict[str, RequestContext] = {"ctx": RequestContext.anonymous(trace_id="it")}
    app.dependency_overrides[current_context] = lambda: holder["ctx"]
    return _Client(TestClient(app, base_url=BASE_URL), holder)


def _catalog_ttl(iri: str, *, title: str | None) -> str:
    title_line = f" ; dct:title {title!r}" if title is not None else ""
    return (
        "@prefix dcat: <http://www.w3.org/ns/dcat#> ."
        "@prefix dct: <http://purl.org/dc/terms/> ."
        f"<{iri}> a dcat:Catalog ; dct:isPartOf <{BASE_URL}>{title_line} ."
    )


def test_spec_returns_merged_closure(app_env: None) -> None:
    c = _make_client()
    with c.http:
        resp = c.as_anonymous().get("/fdp-api/catalog/spec", headers={"Accept": "text/turtle"})
        assert resp.status_code == 200, resp.text
        g = Graph()
        g.parse(data=resp.text, format="turtle")
        property_paths = set(g.objects(None, SH.path))
        # Inherited from Resource (via Dataset) AND a catalog-specific property —
        # both present in the single closure response.
        assert DCT.title in property_paths
        assert DCAT.dataset in property_paths


def test_composition_enforced_on_write(app_env: None) -> None:
    c = _make_client()
    cat = f"{BASE_URL}/catalog/c1"
    with c.http:
        # Missing dct:title (required by the Resource base) → 422.
        bad = c.as_admin().put(
            "/catalog/c1",
            content=_catalog_ttl(cat, title=None),
            headers={"Content-Type": "text/turtle"},
        )
        assert bad.status_code == 422, bad.text
        assert "title" in bad.text.lower()

        # With the inherited property present → created.
        ok = c.as_admin().put(
            "/catalog/c1",
            content=_catalog_ttl(cat, title="Alpha Catalog"),
            headers={"Content-Type": "text/turtle"},
        )
        assert ok.status_code == 201, ok.text

        # The created catalog is itself a Direct Container (task 15.1): the
        # server stamped its membership config from the Catalog RD child links.
        from fdpneo_server.shared.namespaces import DCAT as _DCAT, LDP as _LDP

        g = Graph()
        g.parse(
            data=c.as_admin().get("/catalog/c1", headers={"Accept": "text/turtle"}).text,
            format="turtle",
        )
        cat_ref = URIRef(cat)
        assert (cat_ref, RDF.type, _LDP.DirectContainer) in g
        assert (cat_ref, _LDP.membershipResource, cat_ref) in g
        assert (cat_ref, _LDP.hasMemberRelation, _DCAT.dataset) in g
        assert (cat_ref, _LDP.hasMemberRelation, _DCAT.service) in g


def test_root_is_a_fairdatapoint_direct_container(app_env: None) -> None:
    from fdpneo_server.shared.namespaces import LDP

    c = _make_client()
    root = URIRef(BASE_URL)
    with c.http:
        resp = c.as_anonymous().get("/", headers={"Accept": "text/turtle"})
        assert resp.status_code == 200, resp.text
        g = Graph()
        g.parse(data=resp.text, format="turtle")
        assert (root, RDF.type, FDP_FAIRDATAPOINT) in g
        # A genuine LDP Direct Container (task 15.1), not a Basic Container.
        assert (root, RDF.type, LDP.DirectContainer) in g
        assert (root, RDF.type, LDP.BasicContainer) not in g
        assert (root, LDP.membershipResource, root) in g
        assert (root, LDP.insertedContentRelation, LDP.MemberSubject) in g
        # The membership relation is the root RD's child link (servesMetadata).
        assert (
            root,
            LDP.hasMemberRelation,
            URIRef("https://w3id.org/fdp/o#servesMetadata"),
        ) in g
        # The container advertises its type + shape via Link headers.
        link = c.as_anonymous().get("/", headers={"Accept": "text/turtle"}).headers["Link"]
        assert "ldp#DirectContainer" in link
        assert "ldp#constrainedBy" in link
