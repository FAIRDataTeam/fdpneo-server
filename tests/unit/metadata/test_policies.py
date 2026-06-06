"""Unit tests for the policy admin API (Phase 14 / ADR-0012).

Two layers: the :class:`PolicyService` over an in-memory fake of the
repository/adapter/PDP (so the real ODRL parser runs for profile checks), and
the router over a FastAPI app with a fake service (auth gating + status codes).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from rdflib import Graph, URIRef
from rdflib.namespace import RDF

from fdp.metadata.policies import (
    PolicyService,
    ValidationResultView,
    build_policy_router,
)
from fdp.shared.context import RequestContext
from fdp.shared.errors import BadRequest, Conflict, NotFound
from fdp.shared.graphs import record_graph_uri
from fdp.shared.namespaces import ODRL

BASE = "http://localhost:8000"

VALID_OFFER = """\
@prefix odrl: <http://www.w3.org/ns/odrl/2/> .
@prefix dct:  <http://purl.org/dc/terms/> .
<>  a odrl:Offer ;
    dct:title "Public read, steward modify" ;
    odrl:permission [ a odrl:Permission ; odrl:action odrl:read ] ;
    odrl:prohibition [ a odrl:Prohibition ; odrl:action odrl:delete ] .
"""

# odrl:use is outside the FDP profile action vocabulary → must be rejected.
OUT_OF_PROFILE = """\
@prefix odrl: <http://www.w3.org/ns/odrl/2/> .
<>  a odrl:Offer ;
    odrl:permission [ a odrl:Permission ; odrl:action odrl:use ] .
"""


# --- fake store (repository + adapter + pdp over one dict) ------------------


@dataclass
class _Store:
    graphs: dict[str, Graph] = field(default_factory=dict)
    referenced: bool = False
    cleared: int = 0
    events: list[str] = field(default_factory=list)

    # repository
    async def put_graph(self, record_uri: str, graph: Graph, *, subject: str | None) -> str:
        del subject
        self.graphs[str(record_graph_uri(record_uri))] = graph
        return "etag-1"

    async def get_graph(self, record_uri: str) -> Graph:
        return self.graphs.get(str(record_graph_uri(record_uri)), Graph())

    async def delete_graph(self, record_uri: str) -> None:
        self.graphs.pop(str(record_graph_uri(record_uri)), None)

    # adapter
    async def ask(self, sparql: str) -> bool:
        if "dct/terms/rights" in sparql or "/rights>" in sparql:
            return self.referenced
        match = re.search(r"GRAPH <([^>]+)>", sparql)
        if match is None:
            return False
        graph = self.graphs.get(match.group(1), Graph())
        return (None, RDF.type, ODRL.Offer) in graph

    async def query(self, sparql: str, *, accept: str = "") -> bytes:
        del accept
        if "versionInfo" in sparql:
            return json.dumps({"results": {"bindings": []}}).encode()
        bindings = []
        for iri, g in self.graphs.items():
            if iri.startswith(f"{BASE}/policies/") and (None, RDF.type, ODRL.Offer) in g:
                bindings.append({"g": {"value": iri}})
        return json.dumps({"results": {"bindings": bindings}}).encode()

    # pdp
    async def invalidate_all(self) -> int:
        self.cleared += 1
        return 7

    # event bus
    async def publish(self, event: object) -> None:
        self.events.append(type(event).__name__)


def _service(store: _Store) -> PolicyService:
    return PolicyService(
        repository=store,  # type: ignore[arg-type]
        adapter=store,  # type: ignore[arg-type]
        pdp=store,  # type: ignore[arg-type]
        base_url=BASE,
        event_bus=store,  # type: ignore[arg-type]
    )


# --- service tests ---------------------------------------------------------


@pytest.mark.unit
async def test_put_stores_offer_and_reports_rule_counts() -> None:
    store = _Store()
    info = await _service(store).put("public-read", VALID_OFFER, subject="admin")
    assert info.iri == f"{BASE}/policies/public-read"
    assert info.title == "Public read, steward modify"
    assert info.permissions == 1
    assert info.prohibitions == 1
    assert f"{BASE}/policies/public-read" in store.graphs
    # The subject <> resolved to the stable IRI.
    g = store.graphs[f"{BASE}/policies/public-read"]
    assert (URIRef(f"{BASE}/policies/public-read"), RDF.type, ODRL.Offer) in g


@pytest.mark.unit
async def test_put_clears_authz_cache_and_emits_event() -> None:
    store = _Store()
    await _service(store).put("public-read", VALID_OFFER, subject="admin")
    assert store.cleared == 1
    assert store.events == ["RecordModified"]


@pytest.mark.unit
async def test_put_rejects_out_of_profile_offer() -> None:
    store = _Store()
    with pytest.raises(Exception) as exc:  # SchemaViolation (422)
        await _service(store).put("bad", OUT_OF_PROFILE, subject="admin")
    assert "action" in str(exc.value).lower()
    assert store.cleared == 0  # nothing written, cache untouched


@pytest.mark.unit
async def test_put_rejects_non_offer() -> None:
    store = _Store()
    with pytest.raises(Exception, match="Offer"):
        await _service(store).put("x", "<> <urn:p> <urn:o> .", subject="admin")


@pytest.mark.unit
async def test_put_rejects_malformed_turtle() -> None:
    store = _Store()
    with pytest.raises(BadRequest, match="Turtle"):
        await _service(store).put("x", "not turtle {{{", subject="admin")


@pytest.mark.unit
async def test_bad_slug_rejected() -> None:
    with pytest.raises(BadRequest, match="slug"):
        await _service(_Store()).put("bad/slug", VALID_OFFER, subject="admin")


@pytest.mark.unit
async def test_get_turtle_404_when_absent() -> None:
    with pytest.raises(NotFound):
        await _service(_Store()).get_turtle("nope")


@pytest.mark.unit
async def test_validate_body_accepts_and_rejects() -> None:
    svc = _service(_Store())
    ok = await svc.validate_body("p", VALID_OFFER)
    assert ok.conforms is True and ok.violations == []

    bad = await svc.validate_body("p", OUT_OF_PROFILE)
    assert bad.conforms is False
    assert bad.violations and bad.violations[0]["message"]


@pytest.mark.unit
async def test_delete_refused_when_referenced() -> None:
    store = _Store()
    svc = _service(store)
    await svc.put("public-read", VALID_OFFER, subject="admin")
    store.referenced = True
    with pytest.raises(Conflict, match="referenced"):
        await svc.delete("public-read")
    assert f"{BASE}/policies/public-read" in store.graphs  # not deleted


@pytest.mark.unit
async def test_delete_removes_and_clears_cache() -> None:
    store = _Store()
    svc = _service(store)
    await svc.put("public-read", VALID_OFFER, subject="admin")
    store.cleared = 0
    await svc.delete("public-read")
    assert f"{BASE}/policies/public-read" not in store.graphs
    assert store.cleared == 1
    assert "RecordDeleted" in store.events


@pytest.mark.unit
async def test_list_returns_managed_policies() -> None:
    store = _Store()
    svc = _service(store)
    await svc.put("public-read", VALID_OFFER, subject="admin")
    listed = await svc.list_policies()
    assert [p.id for p in listed] == ["public-read"]
    assert listed[0].permissions == 1


# --- router tests ----------------------------------------------------------


@dataclass
class _FakeService:
    puts: list[tuple[str, str]] = field(default_factory=list)
    deletes: list[str] = field(default_factory=list)

    def iri(self, policy_id: str) -> str:
        return f"{BASE}/policies/{policy_id}"

    async def put(self, policy_id: str, turtle: str, *, subject: str | None):
        del subject
        self.puts.append((policy_id, turtle))
        from fdp.metadata.policies import PolicyInfo

        return PolicyInfo(id=policy_id, iri=self.iri(policy_id), version=1)

    async def get_turtle(self, policy_id: str) -> str:
        return f"# {policy_id}\n"

    async def delete(self, policy_id: str) -> None:
        self.deletes.append(policy_id)

    async def validate_body(self, policy_id: str, turtle: str) -> ValidationResultView:
        del policy_id, turtle
        return ValidationResultView(conforms=True, violations=[])

    async def list_policies(self):
        from fdp.metadata.policies import PolicyInfo

        return [PolicyInfo(id="public-read", iri=self.iri("public-read"))]


def _client(service: _FakeService, *, ctx: RequestContext) -> TestClient:
    from fdp.identity.deps import current_context
    from fdp.shared.errors import register_exception_handlers

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(build_policy_router(service=service))  # type: ignore[arg-type]
    app.dependency_overrides[current_context] = lambda: ctx
    return TestClient(app)


def _admin() -> RequestContext:
    return RequestContext(subject="u#admin", roles=frozenset({"admin"}), trace_id="t")


def _consumer() -> RequestContext:
    return RequestContext(subject="u#bob", roles=frozenset(), trace_id="t")


def _anon() -> RequestContext:
    return RequestContext.anonymous(trace_id="t")


@pytest.mark.unit
def test_list_and_get_are_public() -> None:
    client = _client(_FakeService(), ctx=_anon())
    assert client.get("/policies").status_code == 200
    r = client.get("/policies/public-read")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/turtle")


@pytest.mark.unit
def test_put_requires_admin() -> None:
    svc = _FakeService()
    resp = _client(svc, ctx=_consumer()).put(
        "/policies/p", content="x", headers={"Content-Type": "text/turtle"}
    )
    assert resp.status_code == 403
    assert svc.puts == []


@pytest.mark.unit
def test_put_as_admin_publishes() -> None:
    svc = _FakeService()
    resp = _client(svc, ctx=_admin()).put(
        "/policies/public-read",
        content=VALID_OFFER,
        headers={"Content-Type": "text/turtle"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["iri"] == f"{BASE}/policies/public-read"
    assert svc.puts and svc.puts[0][0] == "public-read"


@pytest.mark.unit
def test_put_rejects_empty_body() -> None:
    resp = _client(_FakeService(), ctx=_admin()).put(
        "/policies/p", content="  ", headers={"Content-Type": "text/turtle"}
    )
    assert resp.status_code == 400


@pytest.mark.unit
def test_delete_requires_admin() -> None:
    svc = _FakeService()
    assert _client(svc, ctx=_consumer()).delete("/policies/p").status_code == 403
    assert svc.deletes == []
