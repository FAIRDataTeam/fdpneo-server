"""Unit tests for :class:`MetadataRepository` with an in-memory fake adapter."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF

from fdp.metadata.graphs import audit_graph_uri, meta_graph_uri
from fdp.metadata.repository import MetadataRepository
from fdp.shared.namespaces import DCT, OWL, PROV

RECORD = "https://example.org/records/r1"
RECORD_URI = URIRef(RECORD)
ALICE = "https://idp.example/realms/fdp#alice"
BOB = "https://idp.example/realms/fdp#bob"


def _record_graph() -> Graph:
    g = Graph()
    g.add((RECORD_URI, DCT.title, Literal("hello")))
    return g


def _empty_named_graphs() -> dict[str, Graph]:
    return {}


def _empty_calls() -> list[str]:
    return []


@dataclass
class FakeAdapter:
    """Stand-in for :class:`TripleStoreAdapter` backed by an in-memory map."""

    graphs: dict[str, Graph] = field(default_factory=_empty_named_graphs)
    update_calls: list[str] = field(default_factory=_empty_calls)

    async def query(self, sparql: str, *, accept: str = "application/sparql-results+json") -> bytes:
        del accept
        target = _extract_graph_uri(sparql)
        if target is None or target not in self.graphs:
            return b""
        return self.graphs[target].serialize(format="turtle").encode("utf-8")

    async def update(self, sparql: str) -> None:
        self.update_calls.append(sparql)

    async def replace_graph(
        self,
        graph_uri: str,
        data: bytes | str | Graph,
        *,
        mime: str = "text/turtle",
    ) -> None:
        del mime
        new = Graph()
        if isinstance(data, Graph):
            for triple in data:
                new.add(triple)
        else:
            blob = data.decode("utf-8") if isinstance(data, bytes) else data
            new.parse(data=blob, format="nt")
        self.graphs[graph_uri] = new

    async def drop_graph(self, graph_uri: str) -> None:
        self.graphs.pop(graph_uri, None)

    async def clear_all(self) -> None:
        self.graphs.clear()


def _extract_graph_uri(sparql: str) -> str | None:
    marker = "GRAPH <"
    start = sparql.find(marker)
    if start == -1:
        return None
    start += len(marker)
    end = sparql.find(">", start)
    return sparql[start:end] if end != -1 else None


def _repo(*, now: datetime | None = None) -> tuple[MetadataRepository, FakeAdapter]:
    adapter = FakeAdapter()
    moment = now or datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    repo = MetadataRepository(adapter, clock=lambda: moment)  # type: ignore[arg-type]
    return repo, adapter


@pytest.mark.unit
async def test_get_graph_empty_when_record_absent() -> None:
    repo, _ = _repo()
    graph = await repo.get_graph(RECORD)
    assert len(graph) == 0


@pytest.mark.unit
async def test_clear_all_wipes_every_graph() -> None:
    repo, adapter = _repo()
    await repo.put_graph(RECORD, _record_graph(), subject=ALICE)
    assert adapter.graphs  # something was written
    await repo.clear_all()
    assert adapter.graphs == {}


@pytest.mark.unit
async def test_put_writes_record_graph_and_meta() -> None:
    repo, adapter = _repo(now=datetime(2026, 6, 1, 12, 0, tzinfo=UTC))
    etag = await repo.put_graph(RECORD, _record_graph(), subject=ALICE)

    assert etag and len(etag) == 32
    assert RECORD in adapter.graphs
    stored = adapter.graphs[RECORD]
    assert (RECORD_URI, DCT.title, Literal("hello")) in stored

    meta = adapter.graphs[str(meta_graph_uri(RECORD))]
    assert (RECORD_URI, RDF.type, PROV.Entity) in meta
    assert (RECORD_URI, DCT.creator, URIRef(ALICE)) in meta
    assert (RECORD_URI, OWL.versionInfo, Literal(1)) in meta
    # PROV Activity is stamped on every write.
    activities = list(meta.objects(RECORD_URI, PROV.wasGeneratedBy))
    assert len(activities) == 1
    activity = activities[0]
    assert (activity, RDF.type, PROV.Activity) in meta
    assert (activity, PROV.wasAssociatedWith, URIRef(ALICE)) in meta


@pytest.mark.unit
async def test_second_put_bumps_version_and_preserves_creator() -> None:
    repo, adapter = _repo(now=datetime(2026, 6, 1, 12, 0, tzinfo=UTC))
    await repo.put_graph(RECORD, _record_graph(), subject=ALICE)

    # Bob tries to overwrite — creator should NOT change, version increments.
    second = Graph()
    second.add((RECORD_URI, DCT.title, Literal("hello again")))
    await repo.put_graph(RECORD, second, subject=BOB)

    meta = adapter.graphs[str(meta_graph_uri(RECORD))]
    assert (RECORD_URI, DCT.creator, URIRef(ALICE)) in meta
    assert (RECORD_URI, DCT.creator, URIRef(BOB)) not in meta
    assert (RECORD_URI, OWL.versionInfo, Literal(2)) in meta


@pytest.mark.unit
async def test_put_etag_changes_when_content_changes() -> None:
    repo, _ = _repo()
    etag1 = await repo.put_graph(RECORD, _record_graph(), subject=ALICE)

    altered = Graph()
    altered.add((RECORD_URI, DCT.title, Literal("changed")))
    etag2 = await repo.put_graph(RECORD, altered, subject=ALICE)
    assert etag1 != etag2


@pytest.mark.unit
async def test_delete_drops_record_meta_and_audit_graphs() -> None:
    repo, adapter = _repo()
    await repo.put_graph(RECORD, _record_graph(), subject=ALICE)
    # Pretend an audit graph also exists.
    audit = Graph()
    audit.add((RECORD_URI, DCT.subject, Literal("anything")))
    adapter.graphs[str(audit_graph_uri(RECORD))] = audit

    await repo.delete_graph(RECORD)

    assert RECORD not in adapter.graphs
    assert str(meta_graph_uri(RECORD)) not in adapter.graphs
    assert str(audit_graph_uri(RECORD)) not in adapter.graphs


@pytest.mark.unit
async def test_get_graph_round_trips_after_put() -> None:
    repo, _ = _repo()
    await repo.put_graph(RECORD, _record_graph(), subject=ALICE)
    fetched = await repo.get_graph(RECORD)
    assert (RECORD_URI, DCT.title, Literal("hello")) in fetched


@pytest.mark.unit
async def test_put_with_no_creator_still_writes_meta() -> None:
    repo, adapter = _repo()
    await repo.put_graph(RECORD, _record_graph(), subject=None)
    meta = adapter.graphs[str(meta_graph_uri(RECORD))]
    assert (RECORD_URI, OWL.versionInfo, Literal(1)) in meta
    # dct:creator omitted when no authenticated principal supplied
    assert not list(meta.objects(RECORD_URI, DCT.creator))
