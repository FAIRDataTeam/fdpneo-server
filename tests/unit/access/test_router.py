"""Unit tests for the SPARQL endpoint router."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import HttpUrl

from fdp.access.results import (
    SPARQL_RESULTS_CSV,
    SPARQL_RESULTS_JSON,
)
from fdp.access.router import build_sparql_router
from fdp.config import TripleStoreSettings
from fdp.identity.deps import current_context
from fdp.policy.model import Action, Decision, Outcome
from fdp.shared.context import RequestContext
from fdp.shared.errors import register_exception_handlers
from fdp.shared.negotiation import TURTLE
from fdp.storage.triplestore import TripleStoreAdapter

QUERY_URL = "http://triplestore.local/query"
UPDATE_URL = "http://triplestore.local/update"
ALICE = "https://idp.example/realms/fdp#alice"
G1 = "https://example.org/g1"
G2 = "https://example.org/g2"


def _settings() -> TripleStoreSettings:
    return TripleStoreSettings(
        query_endpoint=HttpUrl(QUERY_URL),
        update_endpoint=HttpUrl(UPDATE_URL),
        graph_store_endpoint=None,
    )


def _empty_authorized() -> dict[str, set[str]]:
    return {}


@dataclass
class FakePDP:
    """Returns configured authorized graph sets per (subject_key, action)."""

    authorized: dict[str, set[str]] = field(default_factory=_empty_authorized)

    async def authorize(self, ctx: RequestContext, action: Action, resource_iri: str) -> Decision:
        del ctx, resource_iri
        return Decision(outcome=Outcome.DENY, rule=None, reason="not-used-here")

    async def authorized_graphs(self, ctx: RequestContext, action: Action) -> set[str]:
        del ctx
        return set(self.authorized.get(action.value, set()))


def _ctx(*, anonymous: bool = False) -> RequestContext:
    if anonymous:
        return RequestContext.anonymous(
            trace_id="t-1",
            request_timestamp=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
        )
    return RequestContext(
        subject=ALICE,
        roles=frozenset({"steward"}),
        trace_id="t-1",
        request_timestamp=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
    )


def _build_app(
    *,
    pdp: FakePDP,
    adapter: TripleStoreAdapter,
    ctx: RequestContext | None = None,
) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(
        build_sparql_router(
            pdp=pdp,  # type: ignore[arg-type]
            adapter=adapter,
        )
    )
    app.dependency_overrides[current_context] = lambda: ctx or _ctx()
    return app


@pytest.fixture
def async_client() -> httpx.AsyncClient:
    return httpx.AsyncClient()


def _adapter(client: httpx.AsyncClient) -> TripleStoreAdapter:
    return TripleStoreAdapter(_settings(), client)


# --- GET reads --------------------------------------------------------------


@pytest.mark.unit
@respx.mock
def test_get_select_returns_json_results(async_client: httpx.AsyncClient) -> None:
    expected: dict[str, Any] = {"head": {"vars": ["s"]}, "results": {"bindings": []}}
    respx.post(QUERY_URL).respond(200, json=expected)
    pdp = FakePDP(authorized={"read": {G1}})
    app = _build_app(pdp=pdp, adapter=_adapter(async_client))
    with TestClient(app) as client:
        response = client.get("/sparql", params={"query": "SELECT * WHERE { ?s ?p ?o }"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(SPARQL_RESULTS_JSON)
    assert response.json() == expected


@pytest.mark.unit
@respx.mock
def test_get_without_query_param_returns_400(async_client: httpx.AsyncClient) -> None:
    pdp = FakePDP()
    app = _build_app(pdp=pdp, adapter=_adapter(async_client))
    with TestClient(app) as client:
        response = client.get("/sparql")
    assert response.status_code == 400
    assert response.json()["code"] == "fdp.bad_request"


@pytest.mark.unit
@respx.mock
def test_get_select_projects_authorized_dataset(async_client: httpx.AsyncClient) -> None:
    route = respx.post(QUERY_URL).respond(200, json={"head": {}, "results": {"bindings": []}})
    pdp = FakePDP(authorized={"read": {G1, G2}})
    app = _build_app(pdp=pdp, adapter=_adapter(async_client))
    with TestClient(app) as client:
        response = client.get("/sparql", params={"query": "SELECT * WHERE { ?s ?p ?o }"})
    assert response.status_code == 200
    # The full authorized set is forwarded as named-graph-uri query params
    # (URL, not body — see adapter "query via POST directly").
    params = route.calls.last.request.url.params.multi_items()
    assert ("named-graph-uri", G1) in params
    assert ("named-graph-uri", G2) in params


@pytest.mark.unit
@respx.mock
def test_get_with_explicit_unauthorized_from_returns_403(
    async_client: httpx.AsyncClient,
) -> None:
    pdp = FakePDP(authorized={"read": {G2}})
    app = _build_app(pdp=pdp, adapter=_adapter(async_client))
    with TestClient(app) as client:
        response = client.get(
            "/sparql",
            params={"query": f"SELECT * FROM <{G1}> WHERE {{ ?s ?p ?o }}"},
        )
    assert response.status_code == 403
    body = response.json()
    assert body["code"] == "fdp.policy_violation"
    assert body["details"]["graph"] == G1


@pytest.mark.unit
def test_get_with_service_clause_returns_400() -> None:
    pdp = FakePDP()
    app = _build_app(pdp=pdp, adapter=_adapter(httpx.AsyncClient()))
    with TestClient(app) as client:
        response = client.get(
            "/sparql",
            params={"query": "SELECT * WHERE { SERVICE <http://attacker.example/> { ?s ?p ?o } }"},
        )
    assert response.status_code == 400
    assert response.json()["code"] == "fdp.bad_request"


@pytest.mark.unit
def test_get_with_malformed_query_returns_400() -> None:
    pdp = FakePDP()
    app = _build_app(pdp=pdp, adapter=_adapter(httpx.AsyncClient()))
    with TestClient(app) as client:
        response = client.get("/sparql", params={"query": "SELECT * WHERE { <oops"})
    assert response.status_code == 400


# --- Accept negotiation -----------------------------------------------------


@pytest.mark.unit
@respx.mock
def test_select_accept_csv_is_honored(async_client: httpx.AsyncClient) -> None:
    route = respx.post(QUERY_URL).respond(200, text="s\nx\n")
    pdp = FakePDP(authorized={"read": {G1}})
    app = _build_app(pdp=pdp, adapter=_adapter(async_client))
    with TestClient(app) as client:
        response = client.get(
            "/sparql",
            params={"query": "SELECT * WHERE { ?s ?p ?o }"},
            headers={"accept": SPARQL_RESULTS_CSV},
        )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(SPARQL_RESULTS_CSV)
    assert route.calls.last.request.headers["accept"] == SPARQL_RESULTS_CSV


@pytest.mark.unit
def test_select_with_unsupported_accept_returns_406() -> None:
    pdp = FakePDP(authorized={"read": {G1}})
    app = _build_app(pdp=pdp, adapter=_adapter(httpx.AsyncClient()))
    with TestClient(app) as client:
        response = client.get(
            "/sparql",
            params={"query": "SELECT * WHERE { ?s ?p ?o }"},
            headers={"accept": "text/html"},
        )
    assert response.status_code == 406
    assert response.json()["code"] == "fdp.not_acceptable"


# --- POST shapes ------------------------------------------------------------


@pytest.mark.unit
@respx.mock
def test_post_with_sparql_query_content_type(async_client: httpx.AsyncClient) -> None:
    respx.post(QUERY_URL).respond(200, json={"head": {}, "results": {"bindings": []}})
    pdp = FakePDP(authorized={"read": {G1}})
    app = _build_app(pdp=pdp, adapter=_adapter(async_client))
    with TestClient(app) as client:
        response = client.post(
            "/sparql",
            content=b"SELECT * WHERE { ?s ?p ?o }",
            headers={"content-type": "application/sparql-query"},
        )
    assert response.status_code == 200


@pytest.mark.unit
@respx.mock
def test_post_with_form_encoded_query(async_client: httpx.AsyncClient) -> None:
    respx.post(QUERY_URL).respond(200, json={"head": {}, "results": {"bindings": []}})
    pdp = FakePDP(authorized={"read": {G1}})
    app = _build_app(pdp=pdp, adapter=_adapter(async_client))
    with TestClient(app) as client:
        response = client.post(
            "/sparql",
            data={"query": "SELECT * WHERE { ?s ?p ?o }"},
        )
    assert response.status_code == 200


@pytest.mark.unit
def test_post_without_content_type_returns_415() -> None:
    pdp = FakePDP()
    app = _build_app(pdp=pdp, adapter=_adapter(httpx.AsyncClient()))
    with TestClient(app) as client:
        # Strip default content-type by sending None — TestClient still
        # sets one for raw bytes, so send via a request with explicit
        # empty header.
        response = client.post(
            "/sparql",
            content=b"SELECT * WHERE { ?s ?p ?o }",
            headers={"content-type": "text/csv"},
        )
    assert response.status_code == 415
    assert response.json()["code"] == "fdp.unsupported_media_type"


# --- Updates ----------------------------------------------------------------


@pytest.mark.unit
def test_post_sparql_update_anonymous_returns_401() -> None:
    pdp = FakePDP(authorized={"modify": {G1}})
    app = _build_app(pdp=pdp, adapter=_adapter(httpx.AsyncClient()), ctx=_ctx(anonymous=True))
    body = f"INSERT DATA {{ GRAPH <{G1}> {{ <a> <b> <c> }} }}".encode()
    with TestClient(app) as client:
        response = client.post(
            "/sparql",
            content=body,
            headers={"content-type": "application/sparql-update"},
        )
    assert response.status_code == 401
    assert response.json()["code"] == "fdp.unauthenticated"


@pytest.mark.unit
@respx.mock
def test_post_sparql_update_authorized_returns_204(async_client: httpx.AsyncClient) -> None:
    route = respx.post(UPDATE_URL).respond(204)
    pdp = FakePDP(authorized={"modify": {G1}, "read": {G1}})
    app = _build_app(pdp=pdp, adapter=_adapter(async_client))
    body = f"INSERT DATA {{ GRAPH <{G1}> {{ <a> <b> <c> }} }}".encode()
    with TestClient(app) as client:
        response = client.post(
            "/sparql",
            content=body,
            headers={"content-type": "application/sparql-update"},
        )
    assert response.status_code == 204
    sent = route.calls.last.request
    assert sent.headers["content-type"] == "application/sparql-update"
    # WHERE scope: authorized_read graphs go through as using-named-graph-uri.
    assert ("using-named-graph-uri", G1) in sent.url.params.multi_items()


@pytest.mark.unit
def test_post_sparql_update_unauthorized_target_returns_403() -> None:
    pdp = FakePDP(authorized={"modify": {G2}, "read": {G2}})
    app = _build_app(pdp=pdp, adapter=_adapter(httpx.AsyncClient()))
    body = f"INSERT DATA {{ GRAPH <{G1}> {{ <a> <b> <c> }} }}".encode()
    with TestClient(app) as client:
        response = client.post(
            "/sparql",
            content=body,
            headers={"content-type": "application/sparql-update"},
        )
    assert response.status_code == 403
    body_json = response.json()
    assert body_json["code"] == "fdp.policy_violation"
    assert body_json["details"]["graph"] == G1


@pytest.mark.unit
def test_post_ambiguous_update_returns_400() -> None:
    pdp = FakePDP(authorized={"modify": {G1}, "read": {G1}})
    app = _build_app(pdp=pdp, adapter=_adapter(httpx.AsyncClient()))
    with TestClient(app) as client:
        response = client.post(
            "/sparql",
            content=b"DELETE { ?s ?p ?o } WHERE { ?s ?p ?o }",
            headers={"content-type": "application/sparql-update"},
        )
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "fdp.bad_request"
    assert "explicit graph targets" in body["message"]


@pytest.mark.unit
@respx.mock
def test_form_encoded_update_field(async_client: httpx.AsyncClient) -> None:
    route = respx.post(UPDATE_URL).respond(204)
    pdp = FakePDP(authorized={"modify": {G1}, "read": {G1}})
    app = _build_app(pdp=pdp, adapter=_adapter(async_client))
    with TestClient(app) as client:
        response = client.post(
            "/sparql",
            data={"update": f"INSERT DATA {{ GRAPH <{G1}> {{ <a> <b> <c> }} }}"},
        )
    assert response.status_code == 204
    assert len(route.calls) == 1


# --- CONSTRUCT streaming ----------------------------------------------------


@pytest.mark.unit
@respx.mock
def test_construct_returns_streamed_turtle(async_client: httpx.AsyncClient) -> None:
    respx.post(QUERY_URL).respond(200, text="<a> <b> <c> .\n", headers={"Content-Type": TURTLE})
    pdp = FakePDP(authorized={"read": {G1}})
    app = _build_app(pdp=pdp, adapter=_adapter(async_client))
    with TestClient(app) as client:
        response = client.get(
            "/sparql",
            params={"query": "CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o }"},
            headers={"accept": TURTLE},
        )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(TURTLE)
    assert response.text == "<a> <b> <c> .\n"


# --- Misc / sanity ----------------------------------------------------------


@pytest.mark.unit
def test_ask_default_accept_returns_json(async_client: httpx.AsyncClient) -> None:
    with respx.mock() as mock:
        mock.post(QUERY_URL).respond(200, json={"head": {}, "boolean": True})
        pdp = FakePDP(authorized={"read": {G1}})
        app = _build_app(pdp=pdp, adapter=_adapter(async_client))
        with TestClient(app) as client:
            response = client.get("/sparql", params={"query": "ASK { ?s ?p ?o }"})
        assert response.status_code == 200
        payload: dict[str, Any] = json.loads(response.content)
        assert payload["boolean"] is True


# --- store conformance gate (audit R-03) ------------------------------------


def _build_app_gated(*, pdp: FakePDP, adapter: TripleStoreAdapter, safe: bool) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(
        build_sparql_router(
            pdp=pdp,  # type: ignore[arg-type]
            adapter=adapter,
            multigraph_safe_provider=lambda: safe,
        )
    )
    app.dependency_overrides[current_context] = lambda: _ctx()
    return app


@pytest.mark.unit
@respx.mock
def test_multigraph_read_blocked_when_store_not_conformant(
    async_client: httpx.AsyncClient,
) -> None:
    # Two authorized graphs + an unconstrained query → union projection (2 graphs).
    respx.post(QUERY_URL).respond(200, json={"head": {}, "results": {"bindings": []}})
    app = _build_app_gated(
        pdp=FakePDP(authorized={"read": {G1, G2}}), adapter=_adapter(async_client), safe=False
    )
    with TestClient(app) as client:
        r = client.get("/sparql", params={"query": "SELECT * WHERE { ?s ?p ?o }"})
    assert r.status_code == 503
    assert r.json()["code"] == "fdp.service_unavailable"


@pytest.mark.unit
@respx.mock
def test_single_graph_read_allowed_even_when_not_conformant(
    async_client: httpx.AsyncClient,
) -> None:
    # One authorized graph → single-graph projection → allowed despite the flag.
    respx.post(QUERY_URL).respond(200, json={"head": {}, "results": {"bindings": []}})
    app = _build_app_gated(
        pdp=FakePDP(authorized={"read": {G1}}), adapter=_adapter(async_client), safe=False
    )
    with TestClient(app) as client:
        r = client.get("/sparql", params={"query": "SELECT * WHERE { ?s ?p ?o }"})
    assert r.status_code == 200
