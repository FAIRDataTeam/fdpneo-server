"""Unit tests for the shared SPARQL federation/SSRF safety gate.

This gate is the single source of truth shared by the access ``/sparql``
endpoint and the data provider ``/data/{id}/sparql`` endpoint (security audit
2026-06-10, N-01).
"""

from __future__ import annotations

import pytest

from fdp.shared.errors import BadRequest
from fdp.shared.sparql_safety import assert_query_safe


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
