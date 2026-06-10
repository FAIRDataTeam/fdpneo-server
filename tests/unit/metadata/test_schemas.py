"""Unit tests for the schema admin API (Phase 10.1).

Two layers: the :class:`SchemaService` exercised over an in-memory fake of
the repository/adapter/shape-provider (so real pySHACL runs for parse-checks
and previews), and the router driven over a FastAPI app with a fake service
(auth gating + status codes).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from rdflib import Graph
from rdflib.namespace import RDF

from fdp.metadata.schemas import (
    SchemaService,
    ValidationResultView,
    build_schema_router,
)
from fdp.metadata.shacl import ShaclValidator, UnknownShapeError
from fdp.shared.context import RequestContext
from fdp.shared.errors import BadRequest, Conflict, NotFound
from fdp.shared.graphs import record_graph_uri
from fdp.shared.namespaces import SH

BASE = "http://localhost:8000"

ONTOLOGY_SHAPE = """\
@prefix sh:  <http://www.w3.org/ns/shacl#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix dct: <http://purl.org/dc/terms/> .
<http://localhost:8000/fdp-api/schemas/ontology>
    a sh:NodeShape ;
    sh:targetClass owl:Ontology ;
    sh:property [ sh:path dct:title ; sh:minCount 1 ; sh:datatype <http://www.w3.org/2001/XMLSchema#string> ] .
"""


# --- fake store (repository + adapter + shape provider over one dict) -------


@dataclass
class _Store:
    graphs: dict[str, Graph] = field(default_factory=dict)
    referenced: bool = False

    # repository
    async def put_graph(self, record_uri: str, graph: Graph, *, subject: str | None) -> str:
        del subject
        self.graphs[str(record_graph_uri(record_uri))] = graph
        return "etag"

    async def get_graph(self, record_uri: str) -> Graph:
        return self.graphs.get(str(record_graph_uri(record_uri)), Graph())

    async def delete_graph(self, record_uri: str) -> None:
        self.graphs.pop(str(record_graph_uri(record_uri)), None)

    # adapter
    async def ask(self, sparql: str) -> bool:
        if "ResourceDefinition" in sparql:
            return self.referenced
        match = re.search(r"GRAPH <([^>]+)>", sparql)
        if match is None:
            return False
        graph = self.graphs.get(match.group(1), Graph())
        return (None, RDF.type, SH.NodeShape) in graph

    async def query(self, sparql: str, *, accept: str = "") -> bytes:
        del accept
        if "versionInfo" in sparql:
            return json.dumps({"results": {"bindings": []}}).encode()
        # list query
        bindings = []
        for iri, g in self.graphs.items():
            if iri.startswith(f"{BASE}/fdp-api/schemas/") and (None, RDF.type, SH.NodeShape) in g:
                row: dict[str, dict[str, str]] = {"g": {"value": iri}}
                tc = next(iter(g.objects(None, SH.targetClass)), None)
                if tc is not None:
                    row["target"] = {"value": str(tc)}
                bindings.append(row)
        return json.dumps({"results": {"bindings": bindings}}).encode()

    # shape provider
    async def fetch(self, shape_iri: str) -> str:
        g = self.graphs.get(shape_iri)
        if g is None or len(g) == 0:
            raise UnknownShapeError(shape_iri)
        return g.serialize(format="turtle")


def _service(store: _Store) -> SchemaService:
    validator = ShaclValidator(store)  # type: ignore[arg-type]
    return SchemaService(
        repository=store,  # type: ignore[arg-type]
        adapter=store,  # type: ignore[arg-type]
        validator=validator,
        base_url=BASE,
    )


# --- service tests ---------------------------------------------------------


@pytest.mark.unit
async def test_put_stores_shape_and_reports_target_class() -> None:
    store = _Store()
    info = await _service(store).put("ontology", ONTOLOGY_SHAPE, subject="admin")
    assert info.iri == f"{BASE}/fdp-api/schemas/ontology"
    assert info.target_class == "http://www.w3.org/2002/07/owl#Ontology"
    assert f"{BASE}/fdp-api/schemas/ontology" in store.graphs


@pytest.mark.unit
async def test_put_rejects_non_shape() -> None:
    store = _Store()
    with pytest.raises(BadRequest, match="SHACL shape"):
        await _service(store).put("x", "<a> <b> <c> .", subject="admin")


@pytest.mark.unit
async def test_put_rejects_malformed_turtle() -> None:
    store = _Store()
    with pytest.raises(BadRequest, match="Turtle"):
        await _service(store).put("x", "this is not turtle {{{", subject="admin")


@pytest.mark.unit
async def test_bad_slug_rejected() -> None:
    store = _Store()
    with pytest.raises(BadRequest, match="slug"):
        await _service(store).put("bad/slug", ONTOLOGY_SHAPE, subject="admin")


@pytest.mark.unit
async def test_get_turtle_404_when_absent() -> None:
    with pytest.raises(NotFound):
        await _service(_Store()).get_turtle("nope")


@pytest.mark.unit
async def test_validate_sample_conforms_and_reports_violations() -> None:
    store = _Store()
    svc = _service(store)
    await svc.put("ontology", ONTOLOGY_SHAPE, subject="admin")

    ok = await svc.validate_sample(
        "ontology",
        "@prefix owl: <http://www.w3.org/2002/07/owl#> ."
        "@prefix dct: <http://purl.org/dc/terms/> ."
        '<urn:o> a owl:Ontology ; dct:title "Gene Ontology" .',
    )
    assert ok.conforms is True

    bad = await svc.validate_sample(
        "ontology",
        "@prefix owl: <http://www.w3.org/2002/07/owl#> . <urn:o> a owl:Ontology .",
    )
    assert bad.conforms is False
    assert bad.violations  # missing required dct:title


@pytest.mark.unit
async def test_delete_refused_when_referenced_by_resource_definition() -> None:
    store = _Store()
    svc = _service(store)
    await svc.put("ontology", ONTOLOGY_SHAPE, subject="admin")
    store.referenced = True
    with pytest.raises(Conflict, match="referenced"):
        await svc.delete("ontology")
    assert f"{BASE}/fdp-api/schemas/ontology" in store.graphs  # not deleted


@pytest.mark.unit
async def test_delete_removes_shape() -> None:
    store = _Store()
    svc = _service(store)
    await svc.put("ontology", ONTOLOGY_SHAPE, subject="admin")
    await svc.delete("ontology")
    assert f"{BASE}/fdp-api/schemas/ontology" not in store.graphs


@pytest.mark.unit
async def test_list_returns_published_shapes() -> None:
    store = _Store()
    svc = _service(store)
    await svc.put("ontology", ONTOLOGY_SHAPE, subject="admin")
    listed = await svc.list_schemas()
    assert [s.id for s in listed] == ["ontology"]
    assert listed[0].target_class == "http://www.w3.org/2002/07/owl#Ontology"


# --- router tests ----------------------------------------------------------


@dataclass
class _FakeService:
    puts: list[tuple[str, str]] = field(default_factory=list)
    deletes: list[str] = field(default_factory=list)

    def iri(self, schema_id: str) -> str:
        return f"{BASE}/fdp-api/schemas/{schema_id}"

    async def put(self, schema_id: str, turtle: str, *, subject: str | None):
        del subject
        self.puts.append((schema_id, turtle))
        from fdp.metadata.schemas import SchemaInfo

        return SchemaInfo(id=schema_id, iri=self.iri(schema_id), version=1)

    async def get_turtle(self, schema_id: str) -> str:
        return f"# {schema_id}\n"

    async def delete(self, schema_id: str) -> None:
        self.deletes.append(schema_id)

    async def validate_sample(self, schema_id: str, sample: str) -> ValidationResultView:
        del schema_id, sample
        return ValidationResultView(conforms=True, violations=[])

    async def list_schemas(self):
        from fdp.metadata.schemas import SchemaInfo

        return [SchemaInfo(id="ontology", iri=self.iri("ontology"))]


def _client(service: _FakeService, *, ctx: RequestContext) -> TestClient:
    from fdp.identity.deps import current_context
    from fdp.shared.errors import register_exception_handlers

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(build_schema_router(service=service))  # type: ignore[arg-type]
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
    assert client.get("/schemas").status_code == 200
    r = client.get("/schemas/ontology")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/turtle")


@pytest.mark.unit
def test_put_requires_admin() -> None:
    svc = _FakeService()
    resp = _client(svc, ctx=_consumer()).put(
        "/schemas/ontology", content="x", headers={"Content-Type": "text/turtle"}
    )
    assert resp.status_code == 403
    assert svc.puts == []


@pytest.mark.unit
def test_put_as_admin_publishes() -> None:
    svc = _FakeService()
    resp = _client(svc, ctx=_admin()).put(
        "/schemas/ontology",
        content=ONTOLOGY_SHAPE,
        headers={"Content-Type": "text/turtle"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["iri"] == f"{BASE}/fdp-api/schemas/ontology"
    assert svc.puts and svc.puts[0][0] == "ontology"


@pytest.mark.unit
def test_put_rejects_empty_body() -> None:
    resp = _client(_FakeService(), ctx=_admin()).put(
        "/schemas/ontology", content="  ", headers={"Content-Type": "text/turtle"}
    )
    assert resp.status_code == 400


@pytest.mark.unit
def test_delete_requires_admin() -> None:
    svc = _FakeService()
    assert _client(svc, ctx=_consumer()).delete("/schemas/ontology").status_code == 403
    assert svc.deletes == []


@pytest.mark.unit
def test_validate_requires_auth_but_not_admin() -> None:
    # anonymous → 401
    assert (
        _client(_FakeService(), ctx=_anon())
        .post("/schemas/ontology/validate", content="x", headers={"Content-Type": "text/turtle"})
        .status_code
        == 401
    )
    # authenticated non-admin → allowed
    resp = _client(_FakeService(), ctx=_consumer()).post(
        "/schemas/ontology/validate",
        content="<urn:o> a <http://x/T> .",
        headers={"Content-Type": "text/turtle"},
    )
    assert resp.status_code == 200
    assert resp.json()["conforms"] is True
