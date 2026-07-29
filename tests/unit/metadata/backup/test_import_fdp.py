"""Unit tests for ``fdp backup import --from`` (reference-FDP crawl, ADR-0016 §4)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
import pytest
from rdflib import Graph, URIRef
from rdflib.namespace import DCTERMS

from fdpneo_server.metadata.backup import import_reference_fdp
from fdpneo_server.metadata.graphs import meta_graph_uri
from fdpneo_server.metadata.repository import MetadataRepository

pytestmark = pytest.mark.unit

SRC = "http://source.example"
TGT = "http://localhost:8000"

_ROOT = f"""
@prefix ldp: <http://www.w3.org/ns/ldp#> .
@prefix fdp: <https://w3id.org/fdp/o#> .
<{SRC}> a fdp:FAIRDataPoint, ldp:Container ; ldp:contains <{SRC}/catalog/c1> .
"""
_CATALOG = f"""
@prefix ldp: <http://www.w3.org/ns/ldp#> .
@prefix dcat: <http://www.w3.org/ns/dcat#> .
@prefix dct: <http://purl.org/dc/terms/> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
<{SRC}/catalog/c1> a dcat:Catalog, ldp:Container ;
    dct:title "Source catalog" ;
    dct:issued "2019-05-01T00:00:00+00:00"^^xsd:dateTime ;
    dct:modified "2020-08-01T00:00:00+00:00"^^xsd:dateTime ;
    dct:isPartOf <{SRC}> ;
    ldp:contains <{SRC}/dataset/d1> , <http://evil.example/x> .
"""
_DATASET = f"""
@prefix dcat: <http://www.w3.org/ns/dcat#> .
@prefix dct: <http://purl.org/dc/terms/> .
<{SRC}/dataset/d1> a dcat:Dataset ; dct:title "Source dataset" ; dct:isPartOf <{SRC}/catalog/c1> .
"""
_BY_IRI = {SRC: _ROOT, f"{SRC}/catalog/c1": _CATALOG, f"{SRC}/dataset/d1": _DATASET}


def _handler(request: httpx.Request) -> httpx.Response:
    body = _BY_IRI.get(str(request.url).rstrip("/"))
    if body is None:
        return httpx.Response(404)
    return httpx.Response(200, text=body, headers={"content-type": "text/turtle"})


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(_handler))


@dataclass
class _FakeAdapter:
    graphs: dict[str, Graph] = field(default_factory=dict)

    async def query(self, sparql: str, *, accept: str = "") -> bytes:
        del accept
        match = re.search(r"GRAPH <([^>]+)>", sparql)
        graph = self.graphs.get(match.group(1), Graph()) if match else Graph()
        return graph.serialize(format="turtle").encode()

    async def replace_graph(self, graph_uri: str, data: str | bytes, *, mime: str = "") -> None:
        text = data.decode("utf-8") if isinstance(data, bytes) else data
        fmt = "nt" if "n-triples" in mime else "turtle"
        g = Graph()
        g.parse(data=text, format=fmt)
        self.graphs[graph_uri] = g


def _repo() -> tuple[MetadataRepository, _FakeAdapter]:
    adapter = _FakeAdapter()
    repo = MetadataRepository(adapter, clock=lambda: datetime(2026, 7, 6, tzinfo=UTC))  # type: ignore[arg-type]
    return repo, adapter


async def test_import_crawls_reroots_and_carries_provenance() -> None:
    repo, adapter = _repo()
    async with _client() as client:
        report = await import_reference_fdp(
            repository=repo, http_client=client, source_base=SRC, target_base=TGT
        )

    # Root + catalog + dataset imported; the off-origin ldp:contains member skipped.
    assert report.count == 3
    assert (f"{SRC}/catalog/c1", f"{TGT}/catalog/c1") in report.imported
    assert not any("evil.example" in old for old, _ in report.imported)

    # IRIs re-rooted, cross-links included.
    catalog = adapter.graphs[f"{TGT}/catalog/c1"]
    subject = URIRef(f"{TGT}/catalog/c1")
    assert (subject, DCTERMS.isPartOf, URIRef(TGT)) in catalog
    # Old IRI preserved as a structured alternative identifier (ADR-0017).
    assert (subject, DCTERMS.identifier, None) in [(s, p, None) for s, p, _ in catalog]
    assert any(str(o) == f"{SRC}/catalog/c1" for o in catalog.objects(subject, DCTERMS.identifier))

    # Provenance: source dct:issued/modified → meta dct:created/dct:modified.
    meta = adapter.graphs[str(meta_graph_uri(f"{TGT}/catalog/c1"))]
    created = next(meta.objects(subject, DCTERMS.created))
    modified = next(meta.objects(subject, DCTERMS.modified))
    assert str(created).startswith("2019-05-01")
    assert str(modified).startswith("2020-08-01")


async def test_import_dry_run_writes_nothing() -> None:
    repo, adapter = _repo()
    async with _client() as client:
        report = await import_reference_fdp(
            repository=repo, http_client=client, source_base=SRC, target_base=TGT, dry_run=True
        )
    assert report.count == 3
    assert adapter.graphs == {}


async def test_import_skips_unreachable_source() -> None:
    repo, _ = _repo()
    async with _client() as client:
        report = await import_reference_fdp(
            repository=repo,
            http_client=client,
            source_base="http://missing.example",
            target_base=TGT,
        )
    assert report.count == 0
    assert len(report.skipped) == 1
