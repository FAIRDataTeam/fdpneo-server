"""Unit tests for ``fdp.metadata.graphs``."""

from __future__ import annotations

import pytest
from rdflib import URIRef

from fdp.metadata.graphs import (
    audit_graph_uri,
    is_audit_graph_uri,
    is_meta_graph_uri,
    meta_graph_uri,
    record_graph_uri,
    record_uri_from_sibling,
)

RECORD = "https://example.org/records/abc"
RECORD_URI = URIRef(RECORD)


@pytest.mark.unit
def test_record_graph_uri_returns_the_record_uri() -> None:
    assert record_graph_uri(RECORD) == RECORD_URI
    assert record_graph_uri(RECORD_URI) == RECORD_URI


@pytest.mark.unit
def test_trailing_slash_is_normalised_away() -> None:
    assert record_graph_uri(RECORD + "/") == RECORD_URI
    assert meta_graph_uri(RECORD + "/") == URIRef(RECORD + "/meta")


@pytest.mark.unit
def test_meta_and_audit_siblings_have_canonical_suffixes() -> None:
    assert meta_graph_uri(RECORD) == URIRef(RECORD + "/meta")
    assert audit_graph_uri(RECORD) == URIRef(RECORD + "/audit")


@pytest.mark.unit
def test_predicates_classify_correctly() -> None:
    assert is_meta_graph_uri(meta_graph_uri(RECORD))
    assert is_audit_graph_uri(audit_graph_uri(RECORD))
    assert not is_meta_graph_uri(RECORD_URI)
    assert not is_audit_graph_uri(RECORD_URI)


@pytest.mark.unit
def test_record_uri_from_sibling_round_trip() -> None:
    assert record_uri_from_sibling(meta_graph_uri(RECORD)) == RECORD_URI
    assert record_uri_from_sibling(audit_graph_uri(RECORD)) == RECORD_URI
    assert record_uri_from_sibling(RECORD_URI) is None
