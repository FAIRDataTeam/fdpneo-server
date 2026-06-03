"""Unit tests for the search-index event subscriber (Phase 7.1)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF

from fdp.metadata.events import RecordCreated, RecordDeleted, RecordStateChanged
from fdp.metadata.search.extract import ExtractedRecord
from fdp.metadata.search.indexer import SearchIndexer
from fdp.policy.model import Action, Decision, Outcome
from fdp.shared.context import RequestContext
from fdp.shared.events import EventBus
from fdp.shared.namespaces import DCT, FDP_METADATA_STATE, SH

REC = "http://localhost:8000/catalog/c1"
NOW = datetime(2026, 6, 2, 12, 0, tzinfo=UTC)


@dataclass
class _FakeRecords:
    record: Graph
    meta: Graph

    async def get_graph(self, iri: str) -> Graph:
        del iri
        return self.record

    async def get_meta(self, iri: str) -> Graph:
        del iri
        return self.meta


@dataclass
class _FakeSearch:
    upserts: list[tuple[ExtractedRecord, bool]] = field(default_factory=list)
    deletes: list[str] = field(default_factory=list)

    async def upsert(self, rec: ExtractedRecord, *, anon_read: bool, language: str) -> None:
        del language
        self.upserts.append((rec, anon_read))

    async def delete(self, record_iri: str) -> None:
        self.deletes.append(record_iri)


@dataclass
class _FakePDP:
    permit: bool = True

    async def authorize(self, ctx: RequestContext, action: Action, resource: str) -> Decision:
        del ctx, action, resource
        return Decision(
            outcome=Outcome.PERMIT if self.permit else Outcome.DENY, rule=None, reason=""
        )

    async def authorized_graphs(self, ctx: RequestContext, action: Action) -> set[str]:
        return set()


def _catalog() -> Graph:
    g = Graph()
    s = URIRef(REC)
    g.add((s, RDF.type, URIRef("http://www.w3.org/ns/dcat#Catalog")))
    g.add((s, DCT.title, Literal("Cat")))
    return g


def _meta() -> Graph:
    g = Graph()
    g.add((URIRef(REC), FDP_METADATA_STATE, Literal("PUBLISHED")))
    g.add((URIRef(REC), DCT.modified, Literal(NOW)))
    return g


def _indexer(records: _FakeRecords, search: _FakeSearch, *, permit: bool = True) -> SearchIndexer:
    return SearchIndexer(
        records=records,  # type: ignore[arg-type]
        search=search,  # type: ignore[arg-type]
        pdp=_FakePDP(permit=permit),  # type: ignore[arg-type]
        language="english",
        enabled=True,
    )


@pytest.mark.unit
async def test_index_upserts_with_anon_read_flag() -> None:
    search = _FakeSearch()
    idx = _indexer(_FakeRecords(_catalog(), _meta()), search, permit=True)
    assert await idx.index(REC) is True
    assert len(search.upserts) == 1
    rec, anon_read = search.upserts[0]
    assert rec.title == "Cat"
    assert anon_read is True


@pytest.mark.unit
async def test_index_anon_read_false_when_denied() -> None:
    search = _FakeSearch()
    idx = _indexer(_FakeRecords(_catalog(), _meta()), search, permit=False)
    await idx.index(REC)
    assert search.upserts[0][1] is False


@pytest.mark.unit
async def test_index_deletes_non_indexable() -> None:
    shape = Graph()
    shape.add((URIRef(REC), RDF.type, SH.NodeShape))
    search = _FakeSearch()
    idx = _indexer(_FakeRecords(shape, Graph()), search)
    assert await idx.index(REC) is False
    assert search.deletes == [REC]
    assert search.upserts == []


@pytest.mark.unit
async def test_index_deletes_empty_record() -> None:
    search = _FakeSearch()
    idx = _indexer(_FakeRecords(Graph(), Graph()), search)
    assert await idx.index(REC) is False
    assert search.deletes == [REC]


@pytest.mark.unit
async def test_event_subscriptions_dispatch() -> None:
    search = _FakeSearch()
    idx = _indexer(_FakeRecords(_catalog(), _meta()), search)
    bus = EventBus()
    idx.start(bus)
    assert bus.subscriber_count(RecordCreated) == 1
    assert bus.subscriber_count(RecordStateChanged) == 1

    await bus.publish(RecordCreated(record_iri=REC, subject="u", etag="e", timestamp=NOW))
    assert len(search.upserts) == 1

    await bus.publish(RecordDeleted(record_iri=REC, subject="u", timestamp=NOW))
    assert search.deletes == [REC]

    idx.stop()
    assert bus.subscriber_count(RecordCreated) == 0


@pytest.mark.unit
def test_disabled_indexer_does_not_subscribe() -> None:
    idx = SearchIndexer(
        records=_FakeRecords(_catalog(), _meta()),  # type: ignore[arg-type]
        search=_FakeSearch(),  # type: ignore[arg-type]
        pdp=_FakePDP(),  # type: ignore[arg-type]
        language="english",
        enabled=False,
    )
    bus = EventBus()
    idx.start(bus)
    assert bus.subscriber_count(RecordCreated) == 0
