"""End-to-end test: data router against a real Oxigraph + MetadataRepository.

Verifies that:

* the per-distribution SPARQL endpoint scopes the query to the
  distribution's data graph (triples in unrelated graphs stay hidden);
* the redirect path issues a 302 to the upstream URL;
* the policy gate denies when the synthetic anonymous read is rejected.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import HttpUrl
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

from fdpneo_server.config import DataSettings, TripleStoreSettings
from fdpneo_server.data.router import build_data_router
from fdpneo_server.metadata.repository import MetadataRepository
from fdpneo_server.policy.model import Action, Decision, Outcome
from fdpneo_server.shared.context import RequestContext
from fdpneo_server.shared.errors import register_exception_handlers
from fdpneo_server.storage.triplestore.adapter import TripleStoreAdapter

BASE_URL = "https://fdp.example"
DIST_ID = "dist-1"
DIST_IRI = f"{BASE_URL}/data/{DIST_ID}"
RECORD_GRAPH = DIST_IRI
DATA_GRAPH = f"{DIST_IRI}/data"
OTHER_GRAPH = "https://example.org/g/other"
OXIGRAPH_PORT = 7878


# --- fixtures -------------------------------------------------------------


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
async def adapter(
    oxigraph_container: DockerContainer,
) -> AsyncIterator[TripleStoreAdapter]:
    host = oxigraph_container.get_container_host_ip()
    port = oxigraph_container.get_exposed_port(OXIGRAPH_PORT)
    base = f"http://{host}:{port}"
    settings = TripleStoreSettings(
        query_endpoint=HttpUrl(f"{base}/query"),
        update_endpoint=HttpUrl(f"{base}/update"),
        graph_store_endpoint=HttpUrl(f"{base}/store"),
    )
    async with TripleStoreAdapter.from_settings(settings) as a:
        yield a


@pytest.fixture
async def seeded_adapter(adapter: TripleStoreAdapter) -> TripleStoreAdapter:
    """Seed the metadata record graph + the distribution's data graph."""
    record_turtle = f"""\
@prefix dcat: <http://www.w3.org/ns/dcat#> .
@prefix dct:  <http://purl.org/dc/terms/> .
<{DIST_IRI}> a dcat:Distribution ;
    dcat:downloadURL <https://files.example.org/dist-1.csv> ;
    dcat:accessURL   <{DIST_IRI}/sparql> ;
    dct:rights       <{BASE_URL}/offers/public> .
"""
    await adapter.ingest_graph(RECORD_GRAPH, record_turtle)

    data_turtle = """\
@prefix ex: <http://example.org/> .
ex:s1 ex:p ex:o1 .
ex:s2 ex:p ex:o2 .
"""
    await adapter.ingest_graph(DATA_GRAPH, data_turtle)

    # A triple in an unrelated graph that must NOT appear in scoped queries.
    other_turtle = "<http://example.org/x> <http://example.org/p> <http://example.org/y> .\n"
    await adapter.ingest_graph(OTHER_GRAPH, other_turtle, mime="application/n-triples")
    return adapter


# --- fake PDP -------------------------------------------------------------


@dataclass
class _FakePDP:
    decision: Decision = field(
        default_factory=lambda: Decision(outcome=Outcome.PERMIT, rule=None, reason="open")
    )

    async def authorize(self, ctx: RequestContext, action: Action, resource_iri: str) -> Decision:
        del ctx, action, resource_iri
        return self.decision


# --- app builder ----------------------------------------------------------


def _build_app(*, adapter: TripleStoreAdapter, pdp: _FakePDP) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    repository = MetadataRepository(adapter)
    app.include_router(
        build_data_router(
            repository=repository,
            pdp=pdp,  # type: ignore[arg-type]
            adapter=adapter,
            settings=DataSettings(),
            base_url=BASE_URL,
            http_client=httpx.AsyncClient(),
        )
    )
    return app


# --- tests ---------------------------------------------------------------


@pytest.mark.integration
def test_sparql_returns_data_graph_triples_only(seeded_adapter: TripleStoreAdapter) -> None:
    """A SELECT against the distribution endpoint scopes to its data graph."""
    app = _build_app(adapter=seeded_adapter, pdp=_FakePDP())
    client = TestClient(app)
    response = client.get(
        f"/data/{DIST_ID}/sparql",
        params={"query": "SELECT ?s WHERE { ?s ?p ?o } ORDER BY ?s"},
        headers={"accept": "application/sparql-results+json"},
    )
    assert response.status_code == 200
    bindings = json.loads(response.content)["results"]["bindings"]
    subjects = {b["s"]["value"] for b in bindings}
    assert subjects == {"http://example.org/s1", "http://example.org/s2"}
    # The other-graph triple must not leak in.
    assert "http://example.org/x" not in subjects


@pytest.mark.integration
def test_download_redirects_to_upstream_url(seeded_adapter: TripleStoreAdapter) -> None:
    app = _build_app(adapter=seeded_adapter, pdp=_FakePDP())
    client = TestClient(app, follow_redirects=False)
    response = client.get(f"/data/{DIST_ID}")
    assert response.status_code == 302
    assert response.headers["location"] == "https://files.example.org/dist-1.csv"


@pytest.mark.integration
def test_denied_distribution_returns_403(seeded_adapter: TripleStoreAdapter) -> None:
    pdp = _FakePDP(Decision(outcome=Outcome.DENY, rule=None, reason="not open"))
    app = _build_app(adapter=seeded_adapter, pdp=pdp)
    client = TestClient(app, follow_redirects=False)
    assert client.get(f"/data/{DIST_ID}").status_code == 403


@pytest.mark.integration
def test_unknown_distribution_returns_404(adapter: TripleStoreAdapter) -> None:
    """No seeding — distribution record is absent."""
    app = _build_app(adapter=adapter, pdp=_FakePDP())
    client = TestClient(app)
    assert client.get(f"/data/{DIST_ID}").status_code == 404
