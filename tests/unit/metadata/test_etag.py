"""Unit tests for ``fdpneo_server.metadata.etag``."""

from __future__ import annotations

import pytest
from rdflib import Graph, Literal, URIRef

from fdpneo_server.metadata.etag import compute_etag

EX = "https://example.org/"


def _graph_with(triples: list[tuple[URIRef, URIRef, URIRef | Literal]]) -> Graph:
    g = Graph()
    for s, p, o in triples:
        g.add((s, p, o))
    return g


@pytest.mark.unit
def test_empty_graph_has_stable_etag() -> None:
    a = compute_etag(Graph())
    b = compute_etag(Graph())
    assert a == b
    assert len(a) == 32  # BLAKE2b-128 → 32 hex chars


@pytest.mark.unit
def test_same_triples_yield_same_etag() -> None:
    g1 = _graph_with([(URIRef(EX + "a"), URIRef(EX + "p"), URIRef(EX + "b"))])
    g2 = _graph_with([(URIRef(EX + "a"), URIRef(EX + "p"), URIRef(EX + "b"))])
    assert compute_etag(g1) == compute_etag(g2)


@pytest.mark.unit
def test_etag_is_triple_order_independent() -> None:
    a, b = URIRef(EX + "a"), URIRef(EX + "b")
    p = URIRef(EX + "p")
    g_forward = _graph_with([(a, p, b), (b, p, a)])
    g_reverse = _graph_with([(b, p, a), (a, p, b)])
    assert compute_etag(g_forward) == compute_etag(g_reverse)


@pytest.mark.unit
def test_etag_changes_when_a_triple_changes() -> None:
    a, b, c = URIRef(EX + "a"), URIRef(EX + "b"), URIRef(EX + "c")
    p = URIRef(EX + "p")
    g_ab = _graph_with([(a, p, b)])
    g_ac = _graph_with([(a, p, c)])
    assert compute_etag(g_ab) != compute_etag(g_ac)


@pytest.mark.unit
def test_etag_distinguishes_literal_and_uri_objects() -> None:
    s, p = URIRef(EX + "s"), URIRef(EX + "p")
    g_uri = _graph_with([(s, p, URIRef(EX + "x"))])
    g_lit = _graph_with([(s, p, Literal("x"))])
    assert compute_etag(g_uri) != compute_etag(g_lit)


@pytest.mark.unit
def test_etag_is_stable_across_blank_node_relabeling() -> None:
    # A graph with a blank node (e.g. an adms:Identifier node, ADR-0017) must
    # hash the same after a serialization round-trip relabels its _:bN — otherwise
    # If-Match breaks the moment the record is re-read and edited.
    ttl = f'<{EX}rec> <{EX}identifier> [ <{EX}notation> "urn:x" ] .'
    g1 = Graph()
    g1.parse(data=ttl, format="turtle")
    g2 = Graph()
    g2.parse(data=g1.serialize(format="nt"), format="nt")  # fresh _:bN labels
    assert compute_etag(g1) == compute_etag(g2)


@pytest.mark.unit
def test_etag_distinguishes_different_blank_node_content() -> None:
    g1 = Graph()
    g1.parse(data=f'<{EX}rec> <{EX}identifier> [ <{EX}notation> "a" ] .', format="turtle")
    g2 = Graph()
    g2.parse(data=f'<{EX}rec> <{EX}identifier> [ <{EX}notation> "b" ] .', format="turtle")
    assert compute_etag(g1) != compute_etag(g2)
