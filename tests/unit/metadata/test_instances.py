"""Unit tests for the instance/subclass lookup service + router."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fdpneo_server.metadata.instances import InstanceLookupService, build_instances_router
from fdpneo_server.policy.model import Action, Outcome
from fdpneo_server.shared.context import RequestContext
from fdpneo_server.shared.errors import BadRequest

BASE = "http://localhost:8000"
FOAF_AGENT = "http://xmlns.com/foaf/0.1/Agent"


def _rows(*bindings: dict[str, dict[str, str]]) -> bytes:
    return json.dumps({"results": {"bindings": list(bindings)}}).encode()


def _uri(value: str) -> dict[str, str]:
    return {"type": "uri", "value": value}


def _lit(value: str) -> dict[str, str]:
    return {"type": "literal", "value": value}


@dataclass
class _FakeAdapter:
    """Returns canned SPARQL-JSON per query kind; records the queries it saw."""

    count: int = 0
    instance_rows: list[dict[str, dict[str, str]]] = field(default_factory=list)
    subclass_rows: list[dict[str, dict[str, str]]] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)

    async def query(self, sparql: str, *, accept: str = "") -> bytes:
        del accept
        self.queries.append(sparql)
        if "COUNT(DISTINCT ?s)" in sparql:
            return _rows({"n": _lit(str(self.count))})
        if "subClassOf" in sparql:
            return _rows(*self.subclass_rows)
        return _rows(*self.instance_rows)


@dataclass
class _Decision:
    outcome: Outcome


@dataclass
class _FakePDP:
    denied: set[str] = field(default_factory=set)

    async def authorize(self, ctx: RequestContext, action: Action, iri: str) -> _Decision:
        del ctx, action
        return _Decision(Outcome.DENY if iri in self.denied else Outcome.PERMIT)


@dataclass
class _FakeStateGate:
    hidden: set[str] = field(default_factory=set)

    async def is_visible(self, ctx: RequestContext, iri: str) -> bool:
        del ctx
        return iri not in self.hidden


def _service(
    adapter: _FakeAdapter,
    *,
    pdp: _FakePDP | None = None,
    state_gate: _FakeStateGate | None = None,
) -> InstanceLookupService:
    return InstanceLookupService(
        adapter=adapter,  # type: ignore[arg-type]
        pdp=pdp or _FakePDP(),  # type: ignore[arg-type]
        base_url=BASE,
        state_gate=state_gate,  # type: ignore[arg-type]
    )


def _anon() -> RequestContext:
    return RequestContext.anonymous(trace_id="t")


# --- instances -------------------------------------------------------------


@pytest.mark.unit
async def test_instances_returns_items_with_label_and_type() -> None:
    adapter = _FakeAdapter(
        count=2,
        instance_rows=[
            {"s": _uri(f"{BASE}/agent/a1"), "label": _lit("Alice Agent")},
            {"s": _uri(f"{BASE}/agent/a2"), "label": _lit("Bob Agent")},
        ],
    )
    items, total = await _service(adapter).instances(
        class_iri=FOAF_AGENT, q=None, limit=20, offset=0, ctx=_anon()
    )
    assert total == 2
    assert [(i.iri, i.label, i.type) for i in items] == [
        (f"{BASE}/agent/a1", "Alice Agent", FOAF_AGENT),
        (f"{BASE}/agent/a2", "Bob Agent", FOAF_AGENT),
    ]


@pytest.mark.unit
async def test_instances_label_falls_back_to_iri_short_form() -> None:
    adapter = _FakeAdapter(count=1, instance_rows=[{"s": _uri(f"{BASE}/agent/a9")}])
    items, _ = await _service(adapter).instances(
        class_iri=FOAF_AGENT, q=None, limit=20, offset=0, ctx=_anon()
    )
    assert items[0].label == "a9"


@pytest.mark.unit
async def test_instances_empty_when_no_candidates() -> None:
    adapter = _FakeAdapter(count=0)
    items, total = await _service(adapter).instances(
        class_iri=FOAF_AGENT, q=None, limit=20, offset=0, ctx=_anon()
    )
    assert items == []
    assert total == 0


@pytest.mark.unit
async def test_q_filter_is_escaped_into_the_query() -> None:
    adapter = _FakeAdapter(count=0)
    await _service(adapter).instances(
        class_iri=FOAF_AGENT, q='ali"ce', limit=20, offset=0, ctx=_anon()
    )
    count_query = adapter.queries[0]
    assert "CONTAINS(LCASE(STR(COALESCE(?lbl, STR(?s))))" in count_query
    # The quote in the needle is JSON/SPARQL-escaped, not raw.
    assert r"\"" in count_query


@pytest.mark.unit
async def test_invalid_class_iri_rejected() -> None:
    with pytest.raises(BadRequest, match="absolute http"):
        await _service(_FakeAdapter()).instances(
            class_iri="not-an-iri", q=None, limit=20, offset=0, ctx=_anon()
        )


@pytest.mark.unit
async def test_instances_drops_unreadable_but_total_is_pre_gate() -> None:
    a1, a2 = f"{BASE}/agent/a1", f"{BASE}/agent/a2"
    adapter = _FakeAdapter(
        count=2,
        instance_rows=[{"s": _uri(a1)}, {"s": _uri(a2)}],
    )
    # a2 denied by ODRL.
    items, total = await _service(adapter, pdp=_FakePDP(denied={a2})).instances(
        class_iri=FOAF_AGENT, q=None, limit=20, offset=0, ctx=_anon()
    )
    assert [i.iri for i in items] == [a1]
    assert total == 2  # pre-gate count, like /page


@pytest.mark.unit
async def test_instances_drops_unpublished_via_state_gate() -> None:
    a1, a2 = f"{BASE}/agent/a1", f"{BASE}/agent/a2"
    adapter = _FakeAdapter(count=2, instance_rows=[{"s": _uri(a1)}, {"s": _uri(a2)}])
    items, _ = await _service(adapter, state_gate=_FakeStateGate(hidden={a1})).instances(
        class_iri=FOAF_AGENT, q=None, limit=20, offset=0, ctx=_anon()
    )
    assert [i.iri for i in items] == [a2]


# --- subclasses ------------------------------------------------------------


@pytest.mark.unit
async def test_subclasses_returns_descendants_excluding_root() -> None:
    adapter = _FakeAdapter(
        subclass_rows=[
            {"s": _uri("http://www.w3.org/ns/dcat#Catalog"), "label": _lit("Catalog")},
            {"s": _uri(FOAF_AGENT)},  # the root itself — must be excluded
        ]
    )
    items = await _service(adapter).subclasses(class_iri=FOAF_AGENT)
    assert [i.iri for i in items] == ["http://www.w3.org/ns/dcat#Catalog"]
    assert items[0].label == "Catalog"


# --- router ----------------------------------------------------------------


@dataclass
class _FakeService:
    items: list[Any] = field(default_factory=list)
    total: int = 0

    async def instances(self, **_kw: Any) -> tuple[list[Any], int]:
        return self.items, self.total

    async def subclasses(self, **_kw: Any) -> list[Any]:
        return []


def _client(service: Any) -> TestClient:
    from fdpneo_server.identity.deps import current_context

    app = FastAPI()
    app.include_router(build_instances_router(service=service))
    app.dependency_overrides[current_context] = _anon
    return TestClient(app)


@pytest.mark.unit
def test_router_sets_page_headers_and_accepts_class_alias() -> None:
    from fdpneo_server.metadata.instances import InstanceItem

    svc = _FakeService(items=[InstanceItem(iri=f"{BASE}/x", label="X", type=FOAF_AGENT)], total=7)
    resp = _client(svc).get("/instances", params={"class": FOAF_AGENT, "limit": 5, "offset": 0})
    assert resp.status_code == 200
    assert resp.json() == {"items": [{"iri": f"{BASE}/x", "label": "X", "type": FOAF_AGENT}]}
    assert resp.headers["X-FDP-Page-Total"] == "7"
    assert resp.headers["X-FDP-Page-Limit"] == "5"


@pytest.mark.unit
def test_router_requires_class_param() -> None:
    assert _client(_FakeService()).get("/instances").status_code == 422
