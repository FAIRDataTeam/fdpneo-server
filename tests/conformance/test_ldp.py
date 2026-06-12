"""LDP conformance checks against the running FDP (task 15.1).

Encodes the W3C LDP requirements the FDP claims (ADR-0008) as HTTP-level
assertions over the *real* default profile + a real triple store: methods and
status codes, the ``Link: rel=type`` interaction model, ``Allow`` / ``Accept-Post``
/ ``Accept-Patch``, ETag + ``If-Match`` concurrency control, content negotiation,
and Direct-Container membership.

Documented deviations (not failures) — see ADR-0008: custom ``X-FDP-Page-*``
paging instead of LDP Paging, and SPARQL-Update PATCH only (no LD-PATCH).

Requires Docker (testcontainers). Marked ``integration``.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from rdflib import Graph, URIRef
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy
from testcontainers.postgres import PostgresContainer

from fdp.shared.context import RequestContext
from fdp.shared.namespaces import LDP

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PROFILE = REPO_ROOT / "profiles" / "default"
OXIGRAPH_PORT = 7878
BASE_URL = "http://testserver"

pytestmark = pytest.mark.integration


def _async_dsn(container: PostgresContainer) -> str:
    raw = container.get_connection_url()
    return raw.replace("postgresql+psycopg2://", "postgresql+asyncpg://").replace(
        "postgresql://", "postgresql+asyncpg://"
    )


@pytest.fixture(scope="module")
def postgres_container() -> Iterator[PostgresContainer]:
    with PostgresContainer("postgres:16-alpine") as container:
        yield container


@pytest.fixture(scope="module")
def oxigraph_container() -> Iterator[DockerContainer]:
    container = (
        DockerContainer("oxigraph/oxigraph:latest")
        .with_exposed_ports(OXIGRAPH_PORT)
        .with_command("serve --bind 0.0.0.0:7878 --location /data")
        .waiting_for(LogMessageWaitStrategy("Listening").with_startup_timeout(60))
    )
    with container:
        yield container


@pytest.fixture(scope="module")
def client(
    postgres_container: PostgresContainer, oxigraph_container: DockerContainer
) -> Iterator[TestClient]:
    from fdp.config import get_settings
    from fdp.identity.deps import current_context
    from fdp.main import create_app

    host = oxigraph_container.get_container_host_ip()
    oxi = f"http://{host}:{oxigraph_container.get_exposed_port(OXIGRAPH_PORT)}"
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
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    command.upgrade(config, "head")

    app = create_app()
    # Author as a steward; reads below use anonymous where LDP allows. The
    # subject must be an *absolute* IRI — the identity middleware mints it as
    # ``{issuer}#{sub}`` (see identity/middleware.py), and it ends up as the
    # ``dct:creator`` object in the meta graph. A relative IRI here produces
    # invalid N-Triples that strict, spec-conformant parsers (Oxigraph) reject.
    ctx = {
        "v": RequestContext(
            subject="http://idp.local/realms/fdp#a",
            roles=frozenset({"admin", "steward"}),
            trace_id="c",
        )
    }
    app.dependency_overrides[current_context] = lambda: ctx["v"]
    try:
        with TestClient(app, base_url=BASE_URL) as c:
            yield c
    finally:
        for k, prior in saved.items():
            os.environ[k] = prior if prior is not None else os.environ.pop(k, "")
        get_settings.cache_clear()


def _types(link: str) -> set[str]:
    return {m.split("#")[-1] for m in re.findall(r'<([^>]+)>;\s*rel="type"', link)}


# --- LDP Resource / RDFSource (LDP §4) -------------------------------------


def test_get_root_advertises_ldp_interaction_model(client: TestClient) -> None:
    r = client.get("/", headers={"Accept": "text/turtle"})
    assert r.status_code == 200, r.text
    types = _types(r.headers["Link"])
    # Root is an RDF Source AND a Direct Container.
    assert {"Resource", "RDFSource", "Container", "DirectContainer"} <= types
    assert "ldp#constrainedBy" in r.headers["Link"]
    assert r.headers["ETag"]
    # Allow lists the container method set; Accept-Post advertises POST bodies.
    assert "POST" in r.headers["Allow"]
    assert r.headers["Accept-Post"]
    assert r.headers["Accept-Patch"] == "application/sparql-update"


def test_head_matches_get_headers_without_body(client: TestClient) -> None:
    r = client.head("/", headers={"Accept": "text/turtle"})
    assert r.status_code == 200
    assert r.content == b""
    assert r.headers["ETag"] and "DirectContainer" in r.headers["Link"]


def test_content_negotiation_turtle_and_jsonld_and_406(client: TestClient) -> None:
    assert client.get("/", headers={"Accept": "text/turtle"}).status_code == 200
    jsonld = client.get("/", headers={"Accept": "application/ld+json"})
    assert jsonld.status_code == 200
    assert jsonld.headers["content-type"].startswith("application/ld+json")
    # An unsupported representation → 406 Not Acceptable.
    assert client.get("/", headers={"Accept": "application/pdf"}).status_code == 406


# --- LDP write semantics (PUT / If-Match / DELETE) -------------------------


def _catalog(iri: str, title: str = "C") -> str:
    return (
        "@prefix dcat: <http://www.w3.org/ns/dcat#> ."
        "@prefix dct: <http://purl.org/dc/terms/> ."
        f"<{iri}> a dcat:Catalog ; dct:title {title!r} ; dct:isPartOf <{BASE_URL}> ."
    )


def test_put_create_then_if_match_concurrency_then_delete(client: TestClient) -> None:
    iri = f"{BASE_URL}/catalog/x1"
    ttl = {"Content-Type": "text/turtle"}
    created = client.put("/catalog/x1", content=_catalog(iri), headers=ttl)
    assert created.status_code == 201
    assert created.headers["Location"] == iri

    etag = client.get("/catalog/x1", headers={"Accept": "text/turtle"}).headers["ETag"]
    # Update without If-Match on an existing resource → 428 Precondition Required.
    assert client.put("/catalog/x1", content=_catalog(iri, "C2"), headers=ttl).status_code == 428
    # Stale If-Match → 412 Precondition Failed.
    stale = client.put(
        "/catalog/x1", content=_catalog(iri, "C2"), headers={**ttl, "If-Match": '"deadbeef"'}
    )
    assert stale.status_code == 412
    # Correct If-Match → 200.
    ok = client.put("/catalog/x1", content=_catalog(iri, "C2"), headers={**ttl, "If-Match": etag})
    assert ok.status_code == 200
    # DELETE with If-Match → 204; the resource is then gone.
    new_etag = client.get("/catalog/x1", headers={"Accept": "text/turtle"}).headers["ETag"]
    assert client.delete("/catalog/x1", headers={"If-Match": new_etag}).status_code == 204
    assert client.get("/catalog/x1", headers={"Accept": "text/turtle"}).status_code == 404


def test_put_rejects_unsupported_media_type(client: TestClient) -> None:
    r = client.put("/catalog/x2", content="<a/>", headers={"Content-Type": "application/xml"})
    assert r.status_code == 415


# --- LDP Direct Container membership (LDP §5) ------------------------------


def test_post_to_leaf_is_method_not_allowed(client: TestClient) -> None:
    # Create a distribution (a leaf, non-container) then POST to it → 405.
    iri = f"{BASE_URL}/distribution/d1"
    body = (
        "@prefix dcat: <http://www.w3.org/ns/dcat#> ."
        "@prefix dct: <http://purl.org/dc/terms/> ."
        f"<{iri}> a dcat:Distribution ; dct:title 'D' ."
    )
    assert (
        client.put(
            "/distribution/d1", content=body, headers={"Content-Type": "text/turtle"}
        ).status_code
        == 201
    )
    r = client.post("/distribution/d1", content=body, headers={"Content-Type": "text/turtle"})
    assert r.status_code == 405


def test_created_catalog_is_a_direct_container(client: TestClient) -> None:
    iri = f"{BASE_URL}/catalog/x3"
    client.put("/catalog/x3", content=_catalog(iri), headers={"Content-Type": "text/turtle"})
    g = Graph()
    g.parse(data=client.get("/catalog/x3", headers={"Accept": "text/turtle"}).text, format="turtle")
    ref = URIRef(iri)
    assert (ref, URIRef(str(LDP.membershipResource)), ref) in g
    assert (
        ref,
        URIRef("http://www.w3.org/1999/02/22-rdf-syntax-ns#type"),
        LDP.DirectContainer,
    ) in g
