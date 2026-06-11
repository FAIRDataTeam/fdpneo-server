"""Unit tests for forward containment-link maintenance (LDP membership)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from rdflib import Graph, URIRef
from rdflib.namespace import RDF

from fdp.metadata.containment import ContainmentManager
from fdp.shared.graphs import record_graph_uri
from fdp.shared.namespaces import DCAT, DCT, LDP

BASE = "http://localhost:8000"
PARENT = BASE
CHILD = f"{BASE}/catalog/c1"
CATALOG_REL = str(DCAT.catalog)
NOW = datetime(2026, 6, 11, tzinfo=UTC)


# --- fakes -----------------------------------------------------------------


@dataclass
class _Repo:
    graphs: dict[str, Graph] = field(default_factory=dict)
    put_calls: list[str] = field(default_factory=list)

    async def get_graph(self, iri: str) -> Graph:
        return self.graphs.get(str(record_graph_uri(iri)), Graph())

    async def put_graph(self, iri: str, graph: Graph, *, subject: str | None) -> str:
        del subject
        self.graphs[str(record_graph_uri(iri))] = graph
        self.put_calls.append(str(record_graph_uri(iri)))
        return "etag"


@dataclass
class _Resolver:
    relation: str | None = CATALOG_REL

    def containment_relation(self, parent_iri: str, child_iri: str) -> str | None:
        del parent_iri, child_iri
        return self.relation


def _child_graph(*, parent: str | None = PARENT) -> Graph:
    g = Graph()
    cs = URIRef(CHILD)
    g.add((cs, RDF.type, DCAT.Catalog))
    g.add((cs, DCT.title, URIRef("urn:x")))  # value type irrelevant here
    if parent is not None:
        g.add((cs, DCT.isPartOf, URIRef(parent)))
    return g


def _parent_with(*links: URIRef) -> Graph:
    g = Graph()
    ps = URIRef(PARENT)
    g.add((ps, RDF.type, LDP.BasicContainer))
    for pred in links:
        g.add((ps, pred, URIRef(CHILD)))
    return g


def _manager(repo: _Repo, resolver: _Resolver | None = None) -> ContainmentManager:
    return ContainmentManager(repo=repo, resolver=resolver or _Resolver())  # type: ignore[arg-type]


# --- create ----------------------------------------------------------------


@pytest.mark.unit
async def test_create_adds_contains_and_typed_relation() -> None:
    repo = _Repo({PARENT: _parent_with()})
    events = await _manager(repo).reconcile_create(
        CHILD, _child_graph(), subject="u", timestamp=NOW
    )
    parent = repo.graphs[PARENT]
    assert (URIRef(PARENT), LDP.contains, URIRef(CHILD)) in parent
    assert (URIRef(PARENT), DCAT.catalog, URIRef(CHILD)) in parent
    assert [e.record_iri for e in events] == [PARENT]
    assert events[0].etag == "etag"


@pytest.mark.unit
async def test_create_without_ispartof_is_a_noop() -> None:
    repo = _Repo({PARENT: _parent_with()})
    events = await _manager(repo).reconcile_create(
        CHILD, _child_graph(parent=None), subject="u", timestamp=NOW
    )
    assert events == []
    assert repo.put_calls == []


@pytest.mark.unit
async def test_create_with_missing_parent_does_not_fabricate_one() -> None:
    repo = _Repo()  # no parent record stored
    events = await _manager(repo).reconcile_create(
        CHILD, _child_graph(), subject="u", timestamp=NOW
    )
    assert events == []
    assert repo.put_calls == []


@pytest.mark.unit
async def test_create_contains_only_when_relation_unresolved() -> None:
    repo = _Repo({PARENT: _parent_with()})
    events = await _manager(repo, _Resolver(relation=None)).reconcile_create(
        CHILD, _child_graph(), subject="u", timestamp=NOW
    )
    parent = repo.graphs[PARENT]
    assert (URIRef(PARENT), LDP.contains, URIRef(CHILD)) in parent
    assert not list(parent.objects(URIRef(PARENT), DCAT.catalog))
    assert len(events) == 1


@pytest.mark.unit
async def test_create_is_idempotent_no_write_when_links_present() -> None:
    repo = _Repo({PARENT: _parent_with(LDP.contains, DCAT.catalog)})
    events = await _manager(repo).reconcile_create(
        CHILD, _child_graph(), subject="u", timestamp=NOW
    )
    assert events == []
    assert repo.put_calls == []  # nothing to add → no parent write


# --- delete ----------------------------------------------------------------


@pytest.mark.unit
async def test_delete_removes_forward_links() -> None:
    repo = _Repo({PARENT: _parent_with(LDP.contains, DCAT.catalog)})
    events = await _manager(repo).reconcile_delete(
        CHILD, _child_graph(), subject="u", timestamp=NOW
    )
    parent = repo.graphs[PARENT]
    assert (URIRef(PARENT), LDP.contains, URIRef(CHILD)) not in parent
    assert (URIRef(PARENT), DCAT.catalog, URIRef(CHILD)) not in parent
    # The parent's own non-membership triples are untouched.
    assert (URIRef(PARENT), RDF.type, LDP.BasicContainer) in parent
    assert [e.record_iri for e in events] == [PARENT]


# --- update / reparent -----------------------------------------------------


@pytest.mark.unit
async def test_reparent_moves_links_between_parents() -> None:
    new_parent = f"{BASE}/catalog/other"
    repo = _Repo(
        {
            PARENT: _parent_with(LDP.contains, DCAT.catalog),
            new_parent: Graph(),
        }
    )
    repo.graphs[new_parent].add((URIRef(new_parent), RDF.type, LDP.BasicContainer))

    old_graph = _child_graph(parent=PARENT)
    new_graph = _child_graph(parent=new_parent)
    events = await _manager(repo).reconcile_update(
        CHILD, old_graph, new_graph, subject="u", timestamp=NOW
    )

    # Old parent stripped, new parent gained the links.
    assert (URIRef(PARENT), LDP.contains, URIRef(CHILD)) not in repo.graphs[PARENT]
    assert (URIRef(new_parent), LDP.contains, URIRef(CHILD)) in repo.graphs[new_parent]
    assert {e.record_iri for e in events} == {PARENT, new_parent}


@pytest.mark.unit
async def test_update_same_parent_self_heals_missing_links() -> None:
    # Parent has no forward links yet (record created before this feature).
    repo = _Repo({PARENT: _parent_with()})
    g = _child_graph()
    events = await _manager(repo).reconcile_update(CHILD, g, g, subject="u", timestamp=NOW)
    assert (URIRef(PARENT), LDP.contains, URIRef(CHILD)) in repo.graphs[PARENT]
    assert [e.record_iri for e in events] == [PARENT]
