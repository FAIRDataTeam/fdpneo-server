"""Unit tests for :mod:`fdpneo_server.metadata.patch`."""

from __future__ import annotations

import pytest
from rdflib import Graph, Literal, URIRef

from fdpneo_server.metadata.patch import simulate_update
from fdpneo_server.shared.errors import BadRequest
from fdpneo_server.shared.namespaces import DCT

RECORD_IRI = "https://example.org/records/r1"
RECORD = URIRef(RECORD_IRI)
DCAT_KEYWORD = URIRef("http://www.w3.org/ns/dcat#keyword")


def _record_graph(*, title: str = "hello") -> Graph:
    g = Graph()
    g.add((RECORD, DCT.title, Literal(title)))
    return g


@pytest.mark.unit
def test_insert_data_adds_triple_with_relative_iri_resolved() -> None:
    current = _record_graph()
    updated = simulate_update(
        current,
        'INSERT DATA { <> <http://www.w3.org/ns/dcat#keyword> "diabetes" }',
        RECORD_IRI,
    )
    assert (RECORD, DCAT_KEYWORD, Literal("diabetes")) in updated


@pytest.mark.unit
def test_delete_data_removes_a_triple() -> None:
    current = _record_graph()
    updated = simulate_update(
        current,
        f'DELETE DATA {{ <> <{DCT.title}> "hello" }}',
        RECORD_IRI,
    )
    assert (RECORD, DCT.title, Literal("hello")) not in updated


@pytest.mark.unit
def test_delete_where_removes_by_pattern() -> None:
    current = _record_graph(title="anything")
    updated = simulate_update(
        current,
        f"DELETE WHERE {{ <> <{DCT.title}> ?t }}",
        RECORD_IRI,
    )
    assert not list(updated.objects(RECORD, DCT.title))


@pytest.mark.unit
def test_insert_delete_where_round_trips() -> None:
    current = _record_graph()
    update = (
        f"DELETE {{ <> <{DCT.title}> ?t }} "
        f'INSERT {{ <> <{DCT.title}> "renamed" }} '
        f"WHERE  {{ <> <{DCT.title}> ?t }}"
    )
    updated = simulate_update(current, update, RECORD_IRI)
    titles = [str(o) for o in updated.objects(RECORD, DCT.title)]
    assert titles == ["renamed"]


@pytest.mark.unit
def test_simulation_does_not_mutate_input_graph() -> None:
    current = _record_graph()
    before = set(current)
    _ = simulate_update(
        current,
        'INSERT DATA { <> <http://www.w3.org/ns/dcat#keyword> "x" }',
        RECORD_IRI,
    )
    assert set(current) == before


@pytest.mark.unit
def test_empty_body_is_rejected() -> None:
    with pytest.raises(BadRequest, match="empty"):
        simulate_update(_record_graph(), "   ", RECORD_IRI)


@pytest.mark.unit
def test_service_clause_is_rejected() -> None:
    body = (
        "INSERT { ?s <http://example.org/p> ?o } WHERE { "
        "SERVICE <http://attacker.example/> { ?s ?p ?o } }"
    )
    with pytest.raises(BadRequest, match="SERVICE"):
        simulate_update(_record_graph(), body, RECORD_IRI)


@pytest.mark.unit
def test_malformed_sparql_is_rejected_as_bad_request() -> None:
    with pytest.raises(BadRequest, match="parse"):
        simulate_update(_record_graph(), "INSERT DATA { <oops", RECORD_IRI)


@pytest.mark.unit
def test_base_resolves_for_trailing_slash_resource_iri() -> None:
    current = Graph()
    slashed = "https://example.org/records/r2/"
    updated = simulate_update(
        current,
        f'INSERT DATA {{ <> <{DCAT_KEYWORD}> "diabetes" }}',
        slashed,
    )
    assert (URIRef(slashed), DCAT_KEYWORD, Literal("diabetes")) in updated
