"""Unit tests for the Direct Container membership backfill (task 15.1)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import pytest
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF

from fdp.metadata.profiles.backfill import backfill_direct_container_membership
from fdp.metadata.profiles.rd_records import ChildLinkRecord, ResourceDefinitionRecord
from fdp.metadata.profiles.registry import resolve_cache
from fdp.shared.graphs import meta_graph_uri, record_graph_uri
from fdp.shared.namespaces import DCT, LDP

BASE = "http://localhost:8000"
DCAT_CATALOG = URIRef("http://www.w3.org/ns/dcat#Catalog")
DCAT_DATASET_REL = "http://www.w3.org/ns/dcat#dataset"
DCAT_CATALOG_REL = "http://www.w3.org/ns/dcat#catalog"


@dataclass
class _FakeStore:
    """Backs both the repository (``get_graph``) and the adapter over a graph dict."""

    graphs: dict[str, Graph] = field(default_factory=dict)

    # --- repository ---
    async def get_graph(self, record_uri: str) -> Graph:
        return self.graphs.get(str(record_graph_uri(record_uri)), Graph())

    # --- adapter ---
    async def query(self, sparql: str, *, accept: str = "") -> bytes:
        del accept
        if "SELECT DISTINCT ?g" in sparql:
            bindings = [{"g": {"value": g}} for g in self.graphs]
            return json.dumps({"results": {"bindings": bindings}}).encode()
        match = re.search(r"GRAPH <([^>]+)>", sparql)
        graph = self.graphs.get(match.group(1), Graph()) if match else Graph()
        return graph.serialize(format="turtle").encode()

    async def replace_graph(self, graph_uri: str, data: str, *, mime: str = "") -> None:
        del mime
        graph = Graph()
        graph.parse(data=data, format="nt")
        self.graphs[graph_uri] = graph


def _cache() -> object:
    """A root → catalog → dataset hierarchy; distribution is a leaf."""
    records = [
        ResourceDefinitionRecord(
            url_prefix="",
            name="Repository",
            schema_iri="http://x/repo",
            children=(ChildLinkRecord(relation_uri=DCAT_CATALOG_REL, target_prefix="catalog"),),
        ),
        ResourceDefinitionRecord(
            url_prefix="catalog",
            name="Catalog",
            schema_iri="http://x/catalog",
            children=(ChildLinkRecord(relation_uri=DCAT_DATASET_REL, target_prefix="dataset"),),
        ),
        ResourceDefinitionRecord(
            url_prefix="dataset", name="Dataset", schema_iri="http://x/dataset"
        ),
        ResourceDefinitionRecord(
            url_prefix="distribution", name="Distribution", schema_iri="http://x/dist"
        ),
    ]
    return resolve_cache(records, base_url=BASE)


def _basic_container(iri: str, type_iri: URIRef) -> Graph:
    """A pre-15.1 container graph: stale ``ldp:BasicContainer``, no membership config."""
    g = Graph()
    s = URIRef(iri)
    g.add((s, RDF.type, type_iri))
    g.add((s, RDF.type, LDP.BasicContainer))
    g.add((s, DCT.title, Literal("X")))
    return g


def _single(subject: str, predicate: URIRef, value: object) -> Graph:
    g = Graph()
    g.add((URIRef(subject), predicate, Literal(value)))
    return g


def _pre_15_1_store() -> _FakeStore:
    catalog_iri = f"{BASE}/catalog/c1"
    return _FakeStore(
        graphs={
            BASE: _basic_container(BASE, URIRef("http://x/Repository")),
            catalog_iri: _basic_container(catalog_iri, DCAT_CATALOG),
            # A leaf record — must be left untouched.
            f"{BASE}/distribution/d1": _single(f"{BASE}/distribution/d1", DCT.title, "D"),
            # An internal sibling — must be skipped by the graph walk.
            str(meta_graph_uri(catalog_iri)): _single(catalog_iri, DCT.modified, "2026"),
        }
    )


@pytest.mark.unit
async def test_backfill_stamps_membership_and_strips_basic_container() -> None:
    store = _pre_15_1_store()
    cache = _cache()
    catalog_iri = f"{BASE}/catalog/c1"

    report = await backfill_direct_container_membership(
        repository=store,  # type: ignore[arg-type]
        adapter=store,  # type: ignore[arg-type]
        cache=cache,  # type: ignore[arg-type]
    )

    # Both containers stamped; the leaf and the meta sibling are not.
    assert set(report.stamped) == {BASE, catalog_iri}
    assert report.changed is True

    cat = store.graphs[catalog_iri]
    s = URIRef(catalog_iri)
    assert (s, RDF.type, LDP.DirectContainer) in cat
    assert (s, LDP.membershipResource, s) in cat
    assert (s, LDP.insertedContentRelation, LDP.MemberSubject) in cat
    assert (s, LDP.hasMemberRelation, URIRef(DCAT_DATASET_REL)) in cat
    # Stale BasicContainer removed; original content preserved.
    assert (s, RDF.type, LDP.BasicContainer) not in cat
    assert (s, RDF.type, DCAT_CATALOG) in cat
    assert (s, DCT.title, Literal("X")) in cat

    # Root gets its own relation (dcat:catalog); leaf untouched.
    root = store.graphs[BASE]
    assert (URIRef(BASE), LDP.hasMemberRelation, URIRef(DCAT_CATALOG_REL)) in root
    leaf = store.graphs[f"{BASE}/distribution/d1"]
    assert not any(p == LDP.DirectContainer for _, _, p in leaf.triples((None, RDF.type, None)))


@pytest.mark.unit
async def test_backfill_is_idempotent() -> None:
    store = _pre_15_1_store()
    cache = _cache()

    first = await backfill_direct_container_membership(
        repository=store,  # type: ignore[arg-type]
        adapter=store,  # type: ignore[arg-type]
        cache=cache,  # type: ignore[arg-type]
    )
    assert first.changed is True

    second = await backfill_direct_container_membership(
        repository=store,  # type: ignore[arg-type]
        adapter=store,  # type: ignore[arg-type]
        cache=cache,  # type: ignore[arg-type]
    )
    assert second.stamped == []
    assert second.changed is False
    assert set(second.already_conformant) == {BASE, f"{BASE}/catalog/c1"}
