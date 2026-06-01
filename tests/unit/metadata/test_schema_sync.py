"""Unit tests for remote schema synchronization (Phase 10.2).

Drives :class:`SchemaSyncService` over the real :class:`SchemaService` (so
pySHACL parse-checks run on republish) backed by an in-memory fake of the
repository/adapter/shape-provider, with outbound fetches mocked by ``respx``.

Covers: discovery of ``dct:source``-bearing schemas; the host allow-list gate
(SKIPPED); isomorphism-based change detection (UNCHANGED for a blank-node-heavy
shape that re-serializes differently); republish on real change (UPDATED, with
``dct:source`` re-stamped); fetch/parse failures (FAILED); and the aggregate
report counts.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import httpx
import pytest
import respx
from rdflib import Graph, URIRef
from rdflib.namespace import RDF

from fdp.config import SchemaSyncSettings
from fdp.metadata.schema_sync import (
    RemoteSchema,
    SchemaSyncService,
    SyncStatus,
)
from fdp.metadata.schemas import SchemaService
from fdp.metadata.shacl import ShaclValidator, UnknownShapeError
from fdp.shared.graphs import record_graph_uri
from fdp.shared.namespaces import DCT, SH

BASE = "http://localhost:8000"
REMOTE = "https://shapes.example/ontology.ttl"

# A blank-node-heavy shape: re-serializing it relabels the bnode, so a byte/ETag
# compare would report a false change — the isomorphism check must not.
SHAPE_V1 = """\
@prefix sh:  <http://www.w3.org/ns/shacl#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix dct: <http://purl.org/dc/terms/> .
<http://localhost:8000/schemas/ontology>
    a sh:NodeShape ;
    sh:targetClass owl:Ontology ;
    sh:property [ sh:path dct:title ; sh:minCount 1 ;
        sh:datatype <http://www.w3.org/2001/XMLSchema#string> ] .
"""

# Same as V1 plus a second required property → a genuine content change.
SHAPE_V2 = """\
@prefix sh:  <http://www.w3.org/ns/shacl#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix dct: <http://purl.org/dc/terms/> .
<http://localhost:8000/schemas/ontology>
    a sh:NodeShape ;
    sh:targetClass owl:Ontology ;
    sh:property [ sh:path dct:title ; sh:minCount 1 ;
        sh:datatype <http://www.w3.org/2001/XMLSchema#string> ] ;
    sh:property [ sh:path dct:description ; sh:minCount 1 ;
        sh:datatype <http://www.w3.org/2001/XMLSchema#string> ] .
"""


# --- fake store (repository + adapter + shape provider over one dict) -------


@dataclass
class _Store:
    graphs: dict[str, Graph] = field(default_factory=dict)

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
            return False
        match = re.search(r"GRAPH <([^>]+)>", sparql)
        if match is None:
            return False
        graph = self.graphs.get(match.group(1), Graph())
        return (None, RDF.type, SH.NodeShape) in graph

    async def query(self, sparql: str, *, accept: str = "") -> bytes:
        del accept
        if "terms/source" in sparql:  # discovery query
            bindings = []
            for iri, g in self.graphs.items():
                if not iri.startswith(f"{BASE}/schemas/"):
                    continue
                src = next(iter(g.objects(None, DCT.source)), None)
                if src is not None:
                    bindings.append({"g": {"value": iri}, "src": {"value": str(src)}})
            return json.dumps({"results": {"bindings": bindings}}).encode()
        # versionInfo / list queries → empty (version tracking is meta-writer's
        # job, exercised in test_schemas).
        return json.dumps({"results": {"bindings": []}}).encode()

    # shape provider
    async def fetch(self, shape_iri: str) -> str:
        g = self.graphs.get(shape_iri)
        if g is None or len(g) == 0:
            raise UnknownShapeError(shape_iri)
        return g.serialize(format="turtle")


def _schema_service(store: _Store) -> SchemaService:
    return SchemaService(
        repository=store,  # type: ignore[arg-type]
        adapter=store,  # type: ignore[arg-type]
        validator=ShaclValidator(store),  # type: ignore[arg-type]
        base_url=BASE,
    )


def _settings(*, allowed_hosts: list[str] | None = None) -> SchemaSyncSettings:
    return SchemaSyncSettings(
        allowed_hosts=allowed_hosts if allowed_hosts is not None else ["shapes.example"]
    )


async def _seed_with_source(store: _Store) -> SchemaService:
    """Publish SHAPE_V1 at /schemas/ontology carrying a dct:source triple."""
    service = _schema_service(store)
    g = Graph()
    g.parse(data=SHAPE_V1, format="turtle")
    g.add((URIRef(f"{BASE}/schemas/ontology"), DCT.source, URIRef(REMOTE)))
    await service.put("ontology", g.serialize(format="turtle"), subject="admin")
    return service


def _syncer(
    store: _Store, service: SchemaService, client: httpx.AsyncClient, settings: SchemaSyncSettings
) -> SchemaSyncService:
    return SchemaSyncService(
        schema_service=service,
        adapter=store,  # type: ignore[arg-type]
        http_client=client,
        settings=settings,
        base_url=BASE,
    )


# --- discovery -------------------------------------------------------------


@pytest.mark.unit
async def test_discover_finds_sourced_schemas() -> None:
    store = _Store()
    service = await _seed_with_source(store)
    async with httpx.AsyncClient() as client:
        syncer = _syncer(store, service, client, _settings())
        remotes = await syncer.discover()
    assert remotes == [
        RemoteSchema(schema_id="ontology", iri=f"{BASE}/schemas/ontology", source_url=REMOTE)
    ]


@pytest.mark.unit
async def test_discover_ignores_schemas_without_source() -> None:
    store = _Store()
    service = _schema_service(store)
    await service.put("plain", SHAPE_V1, subject="admin")  # no dct:source
    async with httpx.AsyncClient() as client:
        syncer = _syncer(store, service, client, _settings())
        assert await syncer.discover() == []


# --- allow-list gate -------------------------------------------------------


@pytest.mark.unit
async def test_sync_skips_host_not_on_allow_list() -> None:
    store = _Store()
    service = await _seed_with_source(store)
    async with httpx.AsyncClient() as client:
        syncer = _syncer(store, service, client, _settings(allowed_hosts=[]))
        outcome = await syncer.sync_one(
            RemoteSchema("ontology", f"{BASE}/schemas/ontology", REMOTE)
        )
    assert outcome.status is SyncStatus.SKIPPED


# --- change detection ------------------------------------------------------


@pytest.mark.unit
@respx.mock
async def test_sync_unchanged_when_remote_isomorphic() -> None:
    store = _Store()
    service = await _seed_with_source(store)
    # Remote serves the same shape (no dct:source), re-serialized so bnode
    # labels differ from the stored copy.
    reserialized = Graph()
    reserialized.parse(data=SHAPE_V1, format="turtle")
    respx.get(REMOTE).mock(
        return_value=httpx.Response(
            200,
            content=reserialized.serialize(format="turtle"),
            headers={"content-type": "text/turtle"},
        )
    )
    async with httpx.AsyncClient() as client:
        syncer = _syncer(store, service, client, _settings())
        outcome = await syncer.sync_one(
            RemoteSchema("ontology", f"{BASE}/schemas/ontology", REMOTE)
        )
    assert outcome.status is SyncStatus.UNCHANGED


@pytest.mark.unit
@respx.mock
async def test_sync_updates_and_restamps_source_on_change() -> None:
    store = _Store()
    service = await _seed_with_source(store)
    respx.get(REMOTE).mock(
        return_value=httpx.Response(200, content=SHAPE_V2, headers={"content-type": "text/turtle"})
    )
    async with httpx.AsyncClient() as client:
        syncer = _syncer(store, service, client, _settings())
        outcome = await syncer.sync_one(
            RemoteSchema("ontology", f"{BASE}/schemas/ontology", REMOTE)
        )
    assert outcome.status is SyncStatus.UPDATED
    # The republished shape carries the second property and a re-stamped source.
    stored = store.graphs[f"{BASE}/schemas/ontology"]
    paths = {str(o) for o in stored.objects(None, SH.path)}
    assert str(DCT.description) in paths
    assert (URIRef(f"{BASE}/schemas/ontology"), DCT.source, URIRef(REMOTE)) in stored


# --- failures --------------------------------------------------------------


@pytest.mark.unit
@respx.mock
async def test_sync_failed_on_http_error() -> None:
    store = _Store()
    service = await _seed_with_source(store)
    respx.get(REMOTE).mock(return_value=httpx.Response(404))
    async with httpx.AsyncClient() as client:
        syncer = _syncer(store, service, client, _settings())
        outcome = await syncer.sync_one(
            RemoteSchema("ontology", f"{BASE}/schemas/ontology", REMOTE)
        )
    assert outcome.status is SyncStatus.FAILED


@pytest.mark.unit
@respx.mock
async def test_sync_failed_on_unparseable_body() -> None:
    store = _Store()
    service = await _seed_with_source(store)
    respx.get(REMOTE).mock(
        return_value=httpx.Response(
            200, content=b"<<<not rdf>>>", headers={"content-type": "text/turtle"}
        )
    )
    async with httpx.AsyncClient() as client:
        syncer = _syncer(store, service, client, _settings())
        outcome = await syncer.sync_one(
            RemoteSchema("ontology", f"{BASE}/schemas/ontology", REMOTE)
        )
    assert outcome.status is SyncStatus.FAILED


# --- full pass -------------------------------------------------------------


@pytest.mark.unit
@respx.mock
async def test_sync_all_aggregates_counts() -> None:
    store = _Store()
    service = await _seed_with_source(store)
    respx.get(REMOTE).mock(
        return_value=httpx.Response(200, content=SHAPE_V2, headers={"content-type": "text/turtle"})
    )
    async with httpx.AsyncClient() as client:
        syncer = _syncer(store, service, client, _settings())
        report = await syncer.sync_all()
    assert (report.updated, report.unchanged, report.skipped, report.failed) == (1, 0, 0, 0)


# --- config ----------------------------------------------------------------


@pytest.mark.unit
def test_allowed_hosts_parses_csv() -> None:
    s = SchemaSyncSettings(allowed_hosts="a.example, b.example")  # type: ignore[arg-type]
    assert s.allowed_hosts == ["a.example", "b.example"]


@pytest.mark.unit
def test_sync_disabled_by_default() -> None:
    assert SchemaSyncSettings().enabled is False
    assert SchemaSyncSettings().allowed_hosts == []
