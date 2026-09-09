"""Unit tests for the shared SPARQL federation/SSRF safety gate.

This gate is the single source of truth shared by the access ``/sparql``
endpoint and the data provider ``/data/{id}/sparql`` endpoint (security audit
2026-06-10, N-01).
"""

from __future__ import annotations

import pytest

from fdpneo_server.shared.errors import BadRequest, NotFound
from fdpneo_server.shared.sparql_safety import (
    assert_query_safe,
    is_sparql_safe_iri,
    require_sparql_safe_iri,
    sparql_iri_ref,
    sparql_string_literal,
)


@pytest.mark.unit
@pytest.mark.parametrize(
    "query",
    [
        "SELECT * WHERE { ?s ?p ?o }",
        "ASK { ?s ?p ?o }",
        "CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o }",
        "DESCRIBE <urn:x>",
        "  SELECT ?s WHERE { ?s ?p ?o } LIMIT 1  ",
    ],
)
def test_accepts_well_formed_read_queries(query: str) -> None:
    assert_query_safe(query)  # does not raise


@pytest.mark.unit
@pytest.mark.parametrize(
    "query",
    [
        "SELECT * WHERE { SERVICE <http://169.254.169.254/> { ?s ?p ?o } }",
        "SELECT * WHERE { ?s ?p ?o . SERVICE <http://internal/> { ?a ?b ?c } }",
        # SERVICE nested inside an OPTIONAL still gets caught by the walk.
        "SELECT * WHERE { OPTIONAL { SERVICE <http://x/> { ?s ?p ?o } } }",
    ],
)
def test_rejects_service_anywhere(query: str) -> None:
    with pytest.raises(BadRequest, match="SERVICE"):
        assert_query_safe(query)


@pytest.mark.unit
@pytest.mark.parametrize(
    "body",
    [
        "LOAD <http://169.254.169.254/> INTO GRAPH <urn:x>",
        "INSERT DATA { <urn:s> <urn:p> <urn:o> }",
        "DELETE WHERE { ?s ?p ?o }",
        "CLEAR ALL",
    ],
)
def test_rejects_update_forms_as_non_queries(body: str) -> None:
    with pytest.raises(BadRequest):
        assert_query_safe(body)


@pytest.mark.unit
def test_rejects_empty_body() -> None:
    with pytest.raises(BadRequest, match="empty"):
        assert_query_safe("   ")


@pytest.mark.unit
def test_rejects_malformed_query() -> None:
    with pytest.raises(BadRequest):
        assert_query_safe("SELECT WHERE not-valid-sparql {{{")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("hello", '"hello"'),
        ('with "quotes"', '"with \\"quotes\\""'),
        ("line\nbreak", '"line\\nbreak"'),
        ("back\\slash", '"back\\\\slash"'),
    ],
)
def test_sparql_string_literal_escapes_safely(raw: str, expected: str) -> None:
    assert sparql_string_literal(raw) == expected


# --- IRI reference gate ---------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "iri",
    [
        "https://fdp.example/catalog/x",
        "urn:fdp-shape:meta-metadata",
        "https://w3id.org/fdp/fdp-o#FAIRDataPoint",
        "https://fdp.example/a/b?c=d#e",
    ],
)
def test_is_sparql_safe_iri_accepts_legal_iris(iri: str) -> None:
    assert is_sparql_safe_iri(iri) is True


@pytest.mark.unit
@pytest.mark.parametrize(
    "iri",
    [
        "",
        "https://fdp.example/x>",  # what GET /x%3E decodes to — breaks out of <…>
        "https://fdp.example/<x",
        "https://fdp.example/a b",
        "https://fdp.example/a\tb",
        'https://fdp.example/"',
        "https://fdp.example/{x}",
        "https://fdp.example/a|b",
        "https://fdp.example/a^b",
        "https://fdp.example/a`b",
        "https://fdp.example/..\\..\\var/log",  # the scanner probe that 500'd
        "https://fdp.example/a\x00b",
        "https://fdp.example/" + "a" * 2048,
    ],
)
def test_is_sparql_safe_iri_rejects_iriref_forbidden_input(iri: str) -> None:
    assert is_sparql_safe_iri(iri) is False


@pytest.mark.unit
def test_require_sparql_safe_iri_returns_value_or_raises_given_error() -> None:
    assert require_sparql_safe_iri("https://fdp.example/x") == "https://fdp.example/x"
    with pytest.raises(BadRequest):
        require_sparql_safe_iri("https://fdp.example/x>")
    with pytest.raises(NotFound):
        require_sparql_safe_iri("https://fdp.example/x>", error=NotFound("resource not found"))


@pytest.mark.unit
def test_sparql_iri_ref_wraps_only_safe_iris() -> None:
    assert sparql_iri_ref("https://fdp.example/x") == "<https://fdp.example/x>"
    with pytest.raises(BadRequest):
        sparql_iri_ref("https://fdp.example/x> } } #")
