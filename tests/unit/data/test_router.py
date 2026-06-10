"""Unit tests for the data provider router."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from rdflib import Graph, URIRef

from fdp.config import DataSettings
from fdp.data.router import build_data_router
from fdp.policy.model import Action, Decision, Outcome
from fdp.shared.context import RequestContext
from fdp.shared.errors import register_exception_handlers
from fdp.shared.namespaces import DCAT, DCT

BASE_URL = "https://fdp.example"
DIST_ID = "dist-1"
DIST_IRI = f"{BASE_URL}/data/{DIST_ID}"
DATA_GRAPH = f"{DIST_IRI}/data"
RIGHTS_IRI = f"{BASE_URL}/offers/public"


# --- fakes ----------------------------------------------------------------


@dataclass
class _FakeRepo:
    graphs: dict[str, Graph] = field(default_factory=dict)

    async def get_graph(self, record_uri: str) -> Graph:
        return self.graphs.get(record_uri, Graph())


@dataclass
class _FakePDP:
    decision: Decision = field(
        default_factory=lambda: Decision(outcome=Outcome.PERMIT, rule=None, reason="open")
    )
    calls: list[tuple[bool, str, str]] = field(default_factory=list)

    async def authorize(
        self, ctx: RequestContext, action: Action, resource_iri: str
    ) -> Decision:
        self.calls.append((ctx.is_anonymous, action.value, resource_iri))
        return self.decision


@dataclass
class _FakeAdapter:
    chunks: list[bytes] = field(default_factory=lambda: [b'{"x":1}'])
    calls: list[tuple[str, str, tuple[str, ...]]] = field(default_factory=list)

    def query_stream(
        self,
        sparql: str,
        *,
        accept: str,
        default_graph_uris: tuple[str, ...] = (),
    ) -> AsyncIterator[bytes]:
        self.calls.append((sparql, accept, default_graph_uris))

        async def _iter() -> AsyncIterator[bytes]:
            for chunk in self.chunks:
                yield chunk

        return _iter()


# --- graph fixtures -------------------------------------------------------


def _distribution_graph(
    *,
    iri: str = DIST_IRI,
    download_url: str | None = "https://files.example.org/d1.csv",
    access_url: str | None = f"{DIST_IRI}/sparql",
    rights_iri: str | None = RIGHTS_IRI,
) -> Graph:
    g = Graph()
    s = URIRef(iri)
    # Marker so the graph is non-empty.
    g.add((s, DCAT.Distribution, URIRef("https://example.org/type")))
    if download_url is not None:
        g.add((s, DCAT.downloadURL, URIRef(download_url)))
    if access_url is not None:
        g.add((s, DCAT.accessURL, URIRef(access_url)))
    if rights_iri is not None:
        g.add((s, DCT.rights, URIRef(rights_iri)))
    return g


# --- app builder ----------------------------------------------------------


def _build_app(
    *,
    repo: _FakeRepo,
    pdp: _FakePDP,
    adapter: _FakeAdapter,
    settings: DataSettings | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(
        build_data_router(
            repository=repo,  # type: ignore[arg-type]
            pdp=pdp,  # type: ignore[arg-type]
            adapter=adapter,  # type: ignore[arg-type]
            settings=settings or DataSettings(),
            base_url=BASE_URL,
            http_client=http_client or httpx.AsyncClient(),
        )
    )
    return app


# --- 404 / 403 paths ------------------------------------------------------


@pytest.mark.unit
def test_download_returns_404_when_distribution_missing() -> None:
    app = _build_app(repo=_FakeRepo(), pdp=_FakePDP(), adapter=_FakeAdapter())
    client = TestClient(app)
    response = client.get(f"/data/{DIST_ID}")
    assert response.status_code == 404
    assert response.json()["code"] == "fdp.not_found"


@pytest.mark.unit
def test_download_returns_403_when_anonymous_read_denied() -> None:
    pdp = _FakePDP(
        decision=Decision(outcome=Outcome.DENY, rule=None, reason="not open"),
    )
    repo = _FakeRepo(graphs={DIST_IRI: _distribution_graph()})
    app = _build_app(repo=repo, pdp=pdp, adapter=_FakeAdapter())
    client = TestClient(app)
    response = client.get(f"/data/{DIST_ID}")
    assert response.status_code == 403
    assert response.json()["code"] == "fdp.forbidden"
    # Authorization was performed against an anonymous context for the
    # distribution IRI — the v1 invariant.
    assert pdp.calls == [(True, "read", DIST_IRI)]


@pytest.mark.unit
def test_download_returns_404_when_distribution_has_no_download_url() -> None:
    repo = _FakeRepo(graphs={DIST_IRI: _distribution_graph(download_url=None)})
    app = _build_app(repo=repo, pdp=_FakePDP(), adapter=_FakeAdapter())
    client = TestClient(app)
    response = client.get(f"/data/{DIST_ID}")
    assert response.status_code == 404


# --- redirect mode --------------------------------------------------------


@pytest.mark.unit
def test_download_redirects_to_upstream_in_redirect_mode() -> None:
    repo = _FakeRepo(graphs={DIST_IRI: _distribution_graph()})
    app = _build_app(
        repo=repo,
        pdp=_FakePDP(),
        adapter=_FakeAdapter(),
        settings=DataSettings(download_mode="redirect"),
    )
    client = TestClient(app, follow_redirects=False)
    response = client.get(f"/data/{DIST_ID}")
    assert response.status_code == 302
    assert response.headers["location"] == "https://files.example.org/d1.csv"


# --- stream mode ----------------------------------------------------------


@pytest.mark.unit
def test_download_streams_upstream_bytes_in_stream_mode() -> None:
    repo = _FakeRepo(graphs={DIST_IRI: _distribution_graph()})
    upstream = "https://files.example.org/d1.csv"

    with respx.mock(assert_all_called=True) as mock:
        mock.get(upstream).mock(
            return_value=httpx.Response(
                200,
                content=b"hello,world\n",
                headers={"content-type": "text/csv"},
            )
        )
        client_for_proxy = httpx.AsyncClient()
        app = _build_app(
            repo=repo,
            pdp=_FakePDP(),
            adapter=_FakeAdapter(),
            settings=DataSettings(download_mode="stream"),
            http_client=client_for_proxy,
        )
        client = TestClient(app)
        response = client.get(f"/data/{DIST_ID}")

    assert response.status_code == 200
    assert response.content == b"hello,world\n"


# --- SPARQL forwarding ----------------------------------------------------


@pytest.mark.unit
def test_sparql_get_forwards_to_adapter_with_distribution_data_graph() -> None:
    adapter = _FakeAdapter(chunks=[b'{"head":{}}'])
    repo = _FakeRepo(graphs={DIST_IRI: _distribution_graph()})
    app = _build_app(repo=repo, pdp=_FakePDP(), adapter=adapter)
    client = TestClient(app)
    response = client.get(
        f"/data/{DIST_ID}/sparql",
        params={"query": "SELECT * WHERE { ?s ?p ?o }"},
        headers={"accept": "application/sparql-results+json"},
    )
    assert response.status_code == 200
    assert response.content == b'{"head":{}}'
    assert len(adapter.calls) == 1
    sparql, accept, default_graphs = adapter.calls[0]
    assert sparql == "SELECT * WHERE { ?s ?p ?o }"
    assert accept == "application/sparql-results+json"
    assert default_graphs == (DATA_GRAPH,)


@pytest.mark.unit
def test_sparql_get_returns_400_when_query_param_missing() -> None:
    repo = _FakeRepo(graphs={DIST_IRI: _distribution_graph()})
    app = _build_app(repo=repo, pdp=_FakePDP(), adapter=_FakeAdapter())
    client = TestClient(app)
    response = client.get(f"/data/{DIST_ID}/sparql")
    assert response.status_code == 400
    assert response.json()["code"] == "fdp.bad_request"


@pytest.mark.unit
def test_sparql_returns_404_when_distribution_has_no_access_url() -> None:
    repo = _FakeRepo(graphs={DIST_IRI: _distribution_graph(access_url=None)})
    app = _build_app(repo=repo, pdp=_FakePDP(), adapter=_FakeAdapter())
    client = TestClient(app)
    response = client.get(
        f"/data/{DIST_ID}/sparql",
        params={"query": "SELECT * WHERE { ?s ?p ?o }"},
    )
    assert response.status_code == 404


@pytest.mark.unit
def test_sparql_post_accepts_raw_sparql_query_body() -> None:
    adapter = _FakeAdapter()
    repo = _FakeRepo(graphs={DIST_IRI: _distribution_graph()})
    app = _build_app(repo=repo, pdp=_FakePDP(), adapter=adapter)
    client = TestClient(app)
    response = client.post(
        f"/data/{DIST_ID}/sparql",
        content="ASK { ?s ?p ?o }",
        headers={"content-type": "application/sparql-query"},
    )
    assert response.status_code == 200
    assert adapter.calls[0][0] == "ASK { ?s ?p ?o }"


@pytest.mark.unit
def test_sparql_post_accepts_form_encoded_query() -> None:
    adapter = _FakeAdapter()
    repo = _FakeRepo(graphs={DIST_IRI: _distribution_graph()})
    app = _build_app(repo=repo, pdp=_FakePDP(), adapter=adapter)
    client = TestClient(app)
    response = client.post(
        f"/data/{DIST_ID}/sparql",
        data={"query": "SELECT * WHERE { ?s ?p ?o } LIMIT 1"},
    )
    assert response.status_code == 200
    assert "LIMIT 1" in adapter.calls[0][0]


@pytest.mark.unit
def test_sparql_post_rejects_unsupported_content_type() -> None:
    repo = _FakeRepo(graphs={DIST_IRI: _distribution_graph()})
    app = _build_app(repo=repo, pdp=_FakePDP(), adapter=_FakeAdapter())
    client = TestClient(app)
    response = client.post(
        f"/data/{DIST_ID}/sparql",
        content="DELETE WHERE { ?s ?p ?o }",
        headers={"content-type": "application/sparql-update"},
    )
    assert response.status_code == 415
    assert response.json()["code"] == "fdp.unsupported_media_type"


@pytest.mark.unit
def test_sparql_post_form_without_query_returns_400() -> None:
    repo = _FakeRepo(graphs={DIST_IRI: _distribution_graph()})
    app = _build_app(repo=repo, pdp=_FakePDP(), adapter=_FakeAdapter())
    client = TestClient(app)
    response = client.post(
        f"/data/{DIST_ID}/sparql",
        data={"other": "x"},
    )
    assert response.status_code == 400


# --- federation / SSRF gate (security audit 2026-06-10, N-01) -------------


@pytest.mark.unit
def test_sparql_get_rejects_service_clause_without_forwarding() -> None:
    adapter = _FakeAdapter()
    repo = _FakeRepo(graphs={DIST_IRI: _distribution_graph()})
    app = _build_app(repo=repo, pdp=_FakePDP(), adapter=adapter)
    client = TestClient(app)
    response = client.get(
        f"/data/{DIST_ID}/sparql",
        params={
            "query": "SELECT * WHERE { SERVICE <http://169.254.169.254/> { ?s ?p ?o } }"
        },
    )
    assert response.status_code == 400
    assert response.json()["code"] == "fdp.bad_request"
    # The query must never reach the store — that is the whole point of N-01.
    assert adapter.calls == []


@pytest.mark.unit
def test_sparql_post_rejects_service_clause_without_forwarding() -> None:
    adapter = _FakeAdapter()
    repo = _FakeRepo(graphs={DIST_IRI: _distribution_graph()})
    app = _build_app(repo=repo, pdp=_FakePDP(), adapter=adapter)
    client = TestClient(app)
    response = client.post(
        f"/data/{DIST_ID}/sparql",
        content="SELECT * WHERE { SERVICE <http://internal/> { ?s ?p ?o } }",
        headers={"content-type": "application/sparql-query"},
    )
    assert response.status_code == 400
    assert adapter.calls == []


@pytest.mark.unit
def test_sparql_post_rejects_update_body_on_query_surface() -> None:
    adapter = _FakeAdapter()
    repo = _FakeRepo(graphs={DIST_IRI: _distribution_graph()})
    app = _build_app(repo=repo, pdp=_FakePDP(), adapter=adapter)
    client = TestClient(app)
    # A LOAD smuggled in via the raw-query content type is not a read query and
    # must be rejected before forwarding (it would otherwise SSRF via the store).
    response = client.post(
        f"/data/{DIST_ID}/sparql",
        content="LOAD <http://169.254.169.254/> INTO GRAPH <urn:x>",
        headers={"content-type": "application/sparql-query"},
    )
    assert response.status_code == 400
    assert adapter.calls == []


# --- accept passthrough ---------------------------------------------------


@pytest.mark.unit
def test_sparql_forwards_explicit_accept_to_adapter() -> None:
    adapter = _FakeAdapter()
    repo = _FakeRepo(graphs={DIST_IRI: _distribution_graph()})
    app = _build_app(repo=repo, pdp=_FakePDP(), adapter=adapter)
    client = TestClient(app)
    client.get(
        f"/data/{DIST_ID}/sparql",
        params={"query": "SELECT * WHERE { ?s ?p ?o }"},
        headers={"accept": "text/csv"},
    )
    _, accept, _ = adapter.calls[0]
    assert accept == "text/csv"
