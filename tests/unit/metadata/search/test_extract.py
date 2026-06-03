"""Unit tests for the pure search-field extractor (Phase 7.1)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF

from fdp.metadata.search.extract import extract, is_indexable
from fdp.metadata.states import MetadataState
from fdp.shared.graphs import meta_graph_uri
from fdp.shared.namespaces import DCAT, DCT, FDP_METADATA_STATE, LDP, ODRL, SH

REC = "http://localhost:8000/catalog/c1"
NOW = datetime(2026, 6, 2, 12, 0, tzinfo=UTC)


def _catalog_graph() -> Graph:
    g = Graph()
    s = URIRef(REC)
    g.add((s, RDF.type, DCAT.Catalog))
    g.add((s, RDF.type, LDP.BasicContainer))  # structural, not the primary type
    g.add((s, DCT.title, Literal("Genomics Catalog")))
    g.add((s, DCT.description, Literal("Open genomics datasets")))
    g.add((s, DCT.license, URIRef("https://creativecommons.org/licenses/by/4.0/")))
    g.add((s, DCAT.keyword, Literal("genomics")))
    g.add((s, DCAT.keyword, Literal("dna")))
    return g


def _meta_graph(state: MetadataState = MetadataState.PUBLISHED) -> Graph:
    g = Graph()
    s = URIRef(REC)
    g.add((s, FDP_METADATA_STATE, Literal(state.value)))
    g.add((s, DCT.modified, Literal(NOW)))
    return g


@pytest.mark.unit
def test_extract_pulls_all_fields() -> None:
    rec = extract(REC, _catalog_graph(), _meta_graph())
    assert rec.type_iri == str(DCAT.Catalog)  # not the ldp:BasicContainer
    assert rec.title == "Genomics Catalog"
    assert rec.description == "Open genomics datasets"
    assert rec.license == "https://creativecommons.org/licenses/by/4.0/"
    assert "genomics" in rec.keywords and "dna" in rec.keywords  # type: ignore[operator]
    assert rec.state is MetadataState.PUBLISHED
    assert rec.updated_at == NOW
    assert "Genomics Catalog" in rec.search_source
    assert "dna" in rec.search_source


@pytest.mark.unit
def test_extract_handles_missing_fields() -> None:
    g = Graph()
    g.add((URIRef(REC), RDF.type, DCAT.Catalog))
    rec = extract(REC, g, Graph())
    assert rec.title is None
    assert rec.description is None
    assert rec.state is None
    assert rec.updated_at is None
    assert rec.search_source == ""


@pytest.mark.unit
def test_is_indexable_true_for_content() -> None:
    assert is_indexable(REC, _catalog_graph()) is True


@pytest.mark.unit
def test_is_indexable_false_for_config_records() -> None:
    shape = Graph()
    shape.add((URIRef(REC), RDF.type, SH.NodeShape))
    assert is_indexable(REC, shape) is False

    offer = Graph()
    offer.add((URIRef(REC), RDF.type, ODRL.Offer))
    assert is_indexable(REC, offer) is False


@pytest.mark.unit
def test_is_indexable_false_for_internal_graph() -> None:
    meta_iri = str(meta_graph_uri(REC))
    assert is_indexable(meta_iri, _catalog_graph()) is False
