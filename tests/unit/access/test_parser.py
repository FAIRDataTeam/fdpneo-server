"""Unit tests for :mod:`fdp.access.parser`."""

from __future__ import annotations

import pytest

from fdp.access.parser import (
    ParsedRead,
    ParsedUpdate,
    QueryForm,
    parse,
)
from fdp.shared.errors import BadRequest

G1 = "https://example.org/g1"
G2 = "https://example.org/g2"
SRC = "https://example.org/data.ttl"


# --- read forms -------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("body", "expected_form"),
    [
        ("SELECT * WHERE { ?s ?p ?o }", QueryForm.SELECT),
        ("ASK { ?s ?p ?o }", QueryForm.ASK),
        ("CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o }", QueryForm.CONSTRUCT),
        ("DESCRIBE <https://example.org/r1>", QueryForm.DESCRIBE),
    ],
)
def test_parse_classifies_each_read_form(body: str, expected_form: QueryForm) -> None:
    result = parse(body)
    assert isinstance(result, ParsedRead)
    assert result.form is expected_form
    assert result.explicit_default_graphs == ()
    assert result.explicit_named_graphs == ()
    assert result.inline_graph_iris == ()
    assert result.has_dataset_clause is False


@pytest.mark.unit
def test_parse_extracts_from_clauses_as_default_graphs() -> None:
    result = parse(f"SELECT * FROM <{G1}> FROM <{G2}> WHERE {{ ?s ?p ?o }}")
    assert isinstance(result, ParsedRead)
    assert result.explicit_default_graphs == (G1, G2)
    assert result.explicit_named_graphs == ()
    assert result.inline_graph_iris == ()
    assert result.has_dataset_clause is True


@pytest.mark.unit
def test_parse_extracts_from_named_clauses_as_named_graphs() -> None:
    result = parse(
        f"SELECT * FROM NAMED <{G1}> FROM NAMED <{G2}> WHERE {{ GRAPH ?g {{ ?s ?p ?o }} }}"
    )
    assert isinstance(result, ParsedRead)
    assert result.explicit_default_graphs == ()
    assert result.explicit_named_graphs == (G1, G2)
    assert result.inline_graph_iris == ()
    assert result.has_dataset_clause is True


@pytest.mark.unit
def test_parse_captures_inline_graph_iris_separately_from_dataset_clauses() -> None:
    result = parse(f"SELECT * WHERE {{ GRAPH <{G1}> {{ ?s ?p ?o }} }}")
    assert isinstance(result, ParsedRead)
    assert result.explicit_named_graphs == ()
    assert result.inline_graph_iris == (G1,)
    assert result.has_dataset_clause is False


@pytest.mark.unit
def test_parse_ignores_variable_graph_pattern() -> None:
    result = parse("SELECT * WHERE { GRAPH ?g { ?s ?p ?o } }")
    assert isinstance(result, ParsedRead)
    assert result.explicit_named_graphs == ()
    assert result.inline_graph_iris == ()


@pytest.mark.unit
def test_parse_distinguishes_from_named_from_inline_graph() -> None:
    body = f"SELECT * FROM NAMED <{G1}> WHERE {{ GRAPH <{G1}> {{ ?s ?p ?o }} }}"
    result = parse(body)
    assert isinstance(result, ParsedRead)
    # FROM NAMED stays in its own bucket; the inline ref stays in its own.
    assert result.explicit_named_graphs == (G1,)
    assert result.inline_graph_iris == (G1,)
    assert result.has_dataset_clause is True


@pytest.mark.unit
def test_parse_deduplicates_repeated_inline_graph() -> None:
    body = f"SELECT * WHERE {{ GRAPH <{G1}> {{ ?s ?p ?o }} . GRAPH <{G1}> {{ ?x ?y ?z }} }}"
    result = parse(body)
    assert isinstance(result, ParsedRead)
    assert result.inline_graph_iris == (G1,)


@pytest.mark.unit
def test_parse_handles_prologue_with_prefix_and_base() -> None:
    body = (
        "BASE <https://example.org/>\n"
        "PREFIX ex: <https://example.org/>\n"
        f"SELECT * FROM <{G1}> WHERE {{ ?s ?p ?o }}"
    )
    result = parse(body)
    assert isinstance(result, ParsedRead)
    assert result.form is QueryForm.SELECT
    assert result.explicit_default_graphs == (G1,)
    assert result.has_dataset_clause is True


@pytest.mark.unit
@pytest.mark.parametrize(
    "body",
    [
        # Single PREFIX whose IRI ends in a `#` fragment — the `#` must not be
        # mistaken for a comment when classifying the operation.
        "PREFIX dcat: <http://www.w3.org/ns/dcat#>\nSELECT ?c WHERE { ?c a dcat:Catalog }",
        "PREFIX dcat: <http://www.w3.org/ns/dcat#> SELECT ?c WHERE { ?c a dcat:Catalog }",
        # A genuine trailing comment is still stripped.
        "PREFIX dct: <http://purl.org/dc/terms/>  # my prefixes\nSELECT * WHERE { ?s ?p ?o }",
    ],
)
def test_parse_classifies_prefix_with_hash_iri(body: str) -> None:
    result = parse(body)
    assert isinstance(result, ParsedRead)
    assert result.form is QueryForm.SELECT


# --- update forms -----------------------------------------------------------


@pytest.mark.unit
def test_insert_data_with_graph_yields_target() -> None:
    result = parse(f"INSERT DATA {{ GRAPH <{G1}> {{ <a> <b> <c> }} }}")
    assert isinstance(result, ParsedUpdate)
    assert result.targets == (G1,)


@pytest.mark.unit
def test_delete_data_with_graph_yields_target() -> None:
    result = parse(f"DELETE DATA {{ GRAPH <{G1}> {{ <a> <b> <c> }} }}")
    assert isinstance(result, ParsedUpdate)
    assert result.targets == (G1,)


@pytest.mark.unit
def test_insert_data_without_graph_is_rejected() -> None:
    with pytest.raises(BadRequest, match="InsertData requires explicit graph targets"):
        parse("INSERT DATA { <a> <b> <c> }")


@pytest.mark.unit
def test_delete_where_with_graph_yields_target() -> None:
    result = parse(f"DELETE WHERE {{ GRAPH <{G1}> {{ ?s ?p ?o }} }}")
    assert isinstance(result, ParsedUpdate)
    assert result.targets == (G1,)


@pytest.mark.unit
def test_modify_with_with_clause_yields_target() -> None:
    body = f'WITH <{G1}> DELETE {{ <a> <b> ?o }} INSERT {{ <a> <b> "new" }} WHERE {{ <a> <b> ?o }}'
    result = parse(body)
    assert isinstance(result, ParsedUpdate)
    assert result.targets == (G1,)


@pytest.mark.unit
def test_modify_without_with_or_inline_graph_is_rejected() -> None:
    with pytest.raises(BadRequest, match="Modify requires explicit graph targets"):
        parse("DELETE { ?s ?p ?o } WHERE { ?s ?p ?o }")


@pytest.mark.unit
def test_modify_with_inline_graph_in_templates_yields_targets() -> None:
    body = f"INSERT {{ GRAPH <{G1}> {{ ?s ?p ?o }} }} WHERE {{ GRAPH <{G2}> {{ ?s ?p ?o }} }}"
    result = parse(body)
    assert isinstance(result, ParsedUpdate)
    assert result.targets == (G1,)


@pytest.mark.unit
def test_load_into_graph_is_rejected() -> None:
    # LOAD is rejected outright (R-03b): the source URL is an SSRF vector,
    # independent of whether the target graph is authorized.
    with pytest.raises(BadRequest, match="LOAD is not permitted"):
        parse(f"LOAD <{SRC}> INTO GRAPH <{G1}>")


@pytest.mark.unit
def test_load_without_into_graph_is_rejected() -> None:
    with pytest.raises(BadRequest, match="LOAD is not permitted"):
        parse(f"LOAD <{SRC}>")


@pytest.mark.unit
def test_clear_graph_yields_target() -> None:
    result = parse(f"CLEAR GRAPH <{G1}>")
    assert isinstance(result, ParsedUpdate)
    assert result.targets == (G1,)


@pytest.mark.unit
@pytest.mark.parametrize("scope", ["DEFAULT", "NAMED", "ALL"])
def test_clear_categorical_scopes_are_rejected(scope: str) -> None:
    with pytest.raises(BadRequest, match="Clear requires explicit graph targets"):
        parse(f"CLEAR {scope}")


@pytest.mark.unit
def test_create_graph_yields_target() -> None:
    result = parse(f"CREATE GRAPH <{G1}>")
    assert isinstance(result, ParsedUpdate)
    assert result.targets == (G1,)


@pytest.mark.unit
def test_drop_graph_yields_target() -> None:
    result = parse(f"DROP GRAPH <{G1}>")
    assert isinstance(result, ParsedUpdate)
    assert result.targets == (G1,)


@pytest.mark.unit
def test_copy_between_named_graphs_yields_both_targets() -> None:
    result = parse(f"COPY <{G1}> TO <{G2}>")
    assert isinstance(result, ParsedUpdate)
    assert result.targets == (G1, G2)


@pytest.mark.unit
def test_copy_from_default_is_rejected() -> None:
    with pytest.raises(BadRequest, match="Copy requires explicit graph targets"):
        parse(f"COPY DEFAULT TO <{G2}>")


@pytest.mark.unit
def test_multi_operation_update_aggregates_targets_in_order() -> None:
    body = (
        f"INSERT DATA {{ GRAPH <{G1}> {{ <a> <b> <c> }} }} ; "
        f"DELETE DATA {{ GRAPH <{G2}> {{ <a> <b> <c> }} }}"
    )
    result = parse(body)
    assert isinstance(result, ParsedUpdate)
    assert result.targets == (G1, G2)


@pytest.mark.unit
def test_multi_operation_update_rejects_any_ambiguous_op() -> None:
    body = f"INSERT DATA {{ GRAPH <{G1}> {{ <a> <b> <c> }} }} ; DELETE DATA {{ <a> <b> <c> }}"
    with pytest.raises(BadRequest, match="DeleteData requires explicit graph targets"):
        parse(body)


# --- SERVICE rejection ------------------------------------------------------


@pytest.mark.unit
def test_service_in_read_is_rejected() -> None:
    body = "SELECT * WHERE { SERVICE <https://other.example/sparql> { ?s ?p ?o } }"
    with pytest.raises(BadRequest, match="SERVICE clauses are not supported"):
        parse(body)


@pytest.mark.unit
def test_service_inside_update_where_is_rejected() -> None:
    body = (
        f"WITH <{G1}> "
        "DELETE { ?s ?p ?o } "
        "WHERE { SERVICE <https://other.example/sparql> { ?s ?p ?o } }"
    )
    with pytest.raises(BadRequest, match="SERVICE clauses are not supported"):
        parse(body)


# --- malformed / unknown ----------------------------------------------------


@pytest.mark.unit
def test_empty_body_is_rejected() -> None:
    with pytest.raises(BadRequest, match="empty"):
        parse("   ")


@pytest.mark.unit
def test_malformed_read_is_rejected() -> None:
    with pytest.raises(BadRequest, match="could not parse SPARQL"):
        parse("SELECT * WHERE { <oops")


@pytest.mark.unit
def test_malformed_update_is_rejected() -> None:
    with pytest.raises(BadRequest, match="could not parse SPARQL"):
        parse("INSERT DATA { <oops")


@pytest.mark.unit
def test_unknown_keyword_is_rejected() -> None:
    with pytest.raises(BadRequest, match="unrecognized SPARQL operation"):
        parse("FOO * WHERE { ?s ?p ?o }")


# --- LOAD rejection (audit R-03b) -------------------------------------------


@pytest.mark.unit
def test_load_silent_is_rejected() -> None:
    with pytest.raises(BadRequest, match="LOAD is not permitted"):
        parse(f"LOAD SILENT <https://evil.example/data.ttl> INTO GRAPH <{G1}>")
