"""Unit tests for the license admin API (Phase 14 / ADR-0012).

Licenses are descriptive documents (no PDP coupling). Validation is SHACL
against the server-owned license shape: a managed license must carry a
``dct:title`` (and an IRI ``dct:source`` if present) at its stable IRI.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from rdflib import Graph

from fdp.metadata.licenses import (
    LICENSE_SHAPE_IRI,
    LicenseService,
    ValidationResultView,
    build_license_router,
    predefined_license_shape_graph,
)
from fdp.metadata.shacl import InMemoryShapeProvider, ShaclValidator
from fdp.shared.context import RequestContext
from fdp.shared.errors import BadRequest, Conflict, NotFound, SchemaViolation
from fdp.shared.graphs import record_graph_uri
from fdp.shared.namespaces import DCT

BASE = "http://localhost:8000"

CC_BY = """\
@prefix dct: <http://purl.org/dc/terms/> .
<>  a dct:LicenseDocument ;
    dct:title "Creative Commons Attribution 4.0 International" .
"""

TITLE_ONLY = """\
@prefix dct: <http://purl.org/dc/terms/> .
<> dct:title "Some license" .
"""

NOT_A_LICENSE = """\
@prefix dct: <http://purl.org/dc/terms/> .
<urn:other> dct:title "unrelated subject, not the stable IRI" .
"""


@dataclass
class _Store:
    graphs: dict[str, Graph] = field(default_factory=dict)
    states: dict[str, str] = field(default_factory=dict)
    referenced: bool = False
    events: list[str] = field(default_factory=list)

    async def put_graph(self, record_uri: str, graph: Graph, *, subject: str | None) -> str:
        del subject
        self.graphs[str(record_graph_uri(record_uri))] = graph
        return "etag-1"

    async def get_graph(self, record_uri: str) -> Graph:
        return self.graphs.get(str(record_graph_uri(record_uri)), Graph())

    async def delete_graph(self, record_uri: str) -> None:
        self.graphs.pop(str(record_graph_uri(record_uri)), None)

    async def ask(self, sparql: str) -> bool:
        if "/license>" in sparql:
            return self.referenced
        match = re.search(r"GRAPH <([^>]+)>", sparql)
        if match is None:
            return False
        return len(self.graphs.get(match.group(1), Graph())) > 0

    async def query(self, sparql: str, *, accept: str = "") -> bytes:
        del accept
        if "versionInfo" in sparql:
            return json.dumps({"results": {"bindings": []}}).encode()
        bindings = []
        for iri, g in self.graphs.items():
            if not iri.startswith(f"{BASE}/licenses/"):
                continue
            title = next(iter(g.objects(None, DCT.title)), None)
            row: dict[str, dict[str, str]] = {"g": {"value": iri}}
            if title is not None:
                row["title"] = {"value": str(title)}
            if iri in self.states:
                row["state"] = {"value": self.states[iri]}
            bindings.append(row)
        return json.dumps({"results": {"bindings": bindings}}).encode()

    async def publish(self, event: object) -> None:
        self.events.append(type(event).__name__)


def _validator() -> ShaclValidator:
    provider = InMemoryShapeProvider(
        {LICENSE_SHAPE_IRI: predefined_license_shape_graph().serialize(format="turtle")}
    )
    return ShaclValidator(provider)


def _service(store: _Store) -> LicenseService:
    return LicenseService(
        repository=store,  # type: ignore[arg-type]
        adapter=store,  # type: ignore[arg-type]
        validator=_validator(),
        base_url=BASE,
        event_bus=store,  # type: ignore[arg-type]
    )


@pytest.mark.unit
async def test_put_stores_license_document() -> None:
    store = _Store()
    info = await _service(store).put("cc-by-4.0", CC_BY, subject="admin")
    assert info.iri == f"{BASE}/licenses/cc-by-4.0"
    assert info.title.startswith("Creative Commons")
    assert f"{BASE}/licenses/cc-by-4.0" in store.graphs
    assert store.events == ["RecordModified"]


@pytest.mark.unit
async def test_put_accepts_title_only_document() -> None:
    # A plain dct:title (RDF 1.1 simple literal) satisfies the shape's xsd:string.
    store = _Store()
    info = await _service(store).put("x", TITLE_ONLY, subject="admin")
    assert info.title == "Some license"


@pytest.mark.unit
async def test_put_rejects_document_without_title_at_stable_iri() -> None:
    # The shape targets the stable IRI: a title on some *other* subject fails.
    with pytest.raises(SchemaViolation, match="SHACL"):
        await _service(_Store()).put("x", NOT_A_LICENSE, subject="admin")


@pytest.mark.unit
async def test_put_rejects_non_iri_source() -> None:
    bad_source = (
        '@prefix dct: <http://purl.org/dc/terms/> . <> dct:title "x" ; dct:source "not-an-iri" .'
    )
    with pytest.raises(SchemaViolation, match="SHACL"):
        await _service(_Store()).put("x", bad_source, subject="admin")


@pytest.mark.unit
async def test_put_rejects_malformed_turtle() -> None:
    with pytest.raises(BadRequest, match="Turtle"):
        await _service(_Store()).put("x", "nope {{{", subject="admin")


@pytest.mark.unit
async def test_validate_body_accepts_and_rejects() -> None:
    svc = _service(_Store())
    assert (await svc.validate_body("x", CC_BY)).conforms is True
    bad = await svc.validate_body("x", NOT_A_LICENSE)
    assert bad.conforms is False and bad.violations


@pytest.mark.unit
async def test_delete_refused_when_referenced() -> None:
    store = _Store()
    svc = _service(store)
    await svc.put("cc-by-4.0", CC_BY, subject="admin")
    store.referenced = True
    with pytest.raises(Conflict, match="referenced"):
        await svc.delete("cc-by-4.0")
    assert f"{BASE}/licenses/cc-by-4.0" in store.graphs


@pytest.mark.unit
async def test_delete_removes_document() -> None:
    store = _Store()
    svc = _service(store)
    await svc.put("cc-by-4.0", CC_BY, subject="admin")
    await svc.delete("cc-by-4.0")
    assert f"{BASE}/licenses/cc-by-4.0" not in store.graphs
    assert "RecordDeleted" in store.events


@pytest.mark.unit
async def test_get_turtle_404_when_absent() -> None:
    with pytest.raises(NotFound):
        await _service(_Store()).get_turtle("nope")


@pytest.mark.unit
async def test_list_returns_managed_licenses() -> None:
    store = _Store()
    svc = _service(store)
    await svc.put("cc-by-4.0", CC_BY, subject="admin")
    listed = await svc.list_licenses()
    assert [lic.id for lic in listed] == ["cc-by-4.0"]
    assert listed[0].title.startswith("Creative Commons")


@pytest.mark.unit
async def test_published_only_excludes_draft_licenses() -> None:
    store = _Store()
    svc = _service(store)
    await svc.put("cc-by-4.0", CC_BY, subject="admin")
    await svc.put("wip", TITLE_ONLY, subject="admin")
    store.states = {
        f"{BASE}/licenses/cc-by-4.0": "PUBLISHED",
        f"{BASE}/licenses/wip": "DRAFT",
    }
    assert [lic.id for lic in await svc.list_licenses(published_only=True)] == ["cc-by-4.0"]
    assert {lic.id for lic in await svc.list_licenses()} == {"cc-by-4.0", "wip"}


# --- router tests ----------------------------------------------------------


@dataclass
class _FakeService:
    puts: list[tuple[str, str]] = field(default_factory=list)
    deletes: list[str] = field(default_factory=list)
    list_published_only: bool | None = None

    def iri(self, license_id: str) -> str:
        return f"{BASE}/licenses/{license_id}"

    async def put(self, license_id: str, turtle: str, *, subject: str | None):
        del subject
        self.puts.append((license_id, turtle))
        from fdp.metadata.licenses import LicenseInfo

        return LicenseInfo(id=license_id, iri=self.iri(license_id), version=1)

    async def get_turtle(self, license_id: str) -> str:
        return f"# {license_id}\n"

    async def delete(self, license_id: str) -> None:
        self.deletes.append(license_id)

    async def validate_body(self, license_id: str, turtle: str) -> ValidationResultView:
        del license_id, turtle
        return ValidationResultView(conforms=True, violations=[])

    async def list_licenses(self, *, published_only: bool = False):
        from fdp.metadata.licenses import LicenseInfo

        self.list_published_only = published_only
        return [LicenseInfo(id="cc-by-4.0", iri=self.iri("cc-by-4.0"))]


def _client(service: _FakeService, *, ctx: RequestContext) -> TestClient:
    from fdp.identity.deps import current_context
    from fdp.shared.errors import register_exception_handlers

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(build_license_router(service=service))  # type: ignore[arg-type]
    app.dependency_overrides[current_context] = lambda: ctx
    return TestClient(app)


def _admin() -> RequestContext:
    return RequestContext(subject="u#admin", roles=frozenset({"admin"}), trace_id="t")


def _consumer() -> RequestContext:
    return RequestContext(subject="u#bob", roles=frozenset(), trace_id="t")


@pytest.mark.unit
def test_list_and_get_are_public() -> None:
    client = _client(_FakeService(), ctx=RequestContext.anonymous(trace_id="t"))
    assert client.get("/licenses").status_code == 200
    r = client.get("/licenses/cc-by-4.0")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/turtle")


@pytest.mark.unit
def test_anonymous_list_is_published_only_admin_sees_all() -> None:
    anon_svc = _FakeService()
    _client(anon_svc, ctx=RequestContext.anonymous(trace_id="t")).get("/licenses")
    assert anon_svc.list_published_only is True

    admin_svc = _FakeService()
    _client(admin_svc, ctx=_admin()).get("/licenses")
    assert admin_svc.list_published_only is False


@pytest.mark.unit
def test_put_requires_admin() -> None:
    svc = _FakeService()
    resp = _client(svc, ctx=_consumer()).put(
        "/licenses/x", content="x", headers={"Content-Type": "text/turtle"}
    )
    assert resp.status_code == 403
    assert svc.puts == []


@pytest.mark.unit
def test_put_as_admin_publishes() -> None:
    svc = _FakeService()
    resp = _client(svc, ctx=_admin()).put(
        "/licenses/cc-by-4.0",
        content=CC_BY,
        headers={"Content-Type": "text/turtle"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["iri"] == f"{BASE}/licenses/cc-by-4.0"


@pytest.mark.unit
def test_delete_requires_admin() -> None:
    svc = _FakeService()
    assert _client(svc, ctx=_consumer()).delete("/licenses/x").status_code == 403
