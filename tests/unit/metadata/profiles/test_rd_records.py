"""Unit tests for :mod:`fdpneo_server.metadata.profiles.rd_records` (task #7).

Covers the RDF record layer for resource definitions: round-trip
serialization, parse-error surfacing, and that the predefined SHACL shape
accepts a well-formed record and rejects malformed ones.
"""

from __future__ import annotations

from typing import cast

import pyshacl
import pytest
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF, XSD

from fdpneo_server.metadata.profiles.rd_records import (
    RD_SHAPE_IRI,
    ChildLinkRecord,
    ResourceDefinitionParseError,
    ResourceDefinitionRecord,
    predefined_shape_graph,
    record_from_graph,
    record_to_graph,
)
from fdpneo_server.shared.namespaces import (
    FDP_NAME,
    FDP_RESOURCE_DEFINITION,
    FDP_URL_PREFIX,
    LDP,
)

CATALOG_IRI = "http://localhost:8000/resource-definitions/catalog"
ROOT_IRI = "http://localhost:8000/resource-definitions/repository"

CATALOG_RECORD = ResourceDefinitionRecord(
    url_prefix="catalog",
    name="Catalog",
    schema_iri="http://www.w3.org/ns/dcat#Catalog",
    children=(
        ChildLinkRecord(
            relation_uri="http://www.w3.org/ns/dcat#dataset",
            target_prefix="dataset",
            title="Datasets",
        ),
        ChildLinkRecord(
            relation_uri="http://www.w3.org/ns/dcat#service",
            target_prefix="data-service",
            title="Data Services",
        ),
    ),
)


def _conforms(graph: Graph) -> tuple[bool, str]:
    conforms, _, text = cast(
        tuple[bool, Graph, str],
        pyshacl.validate(  # pyright: ignore[reportUnknownMemberType]
            data_graph=graph,
            shacl_graph=predefined_shape_graph(),
            inference="none",
            advanced=False,
            meta_shacl=False,
            inplace=False,
        ),
    )
    return conforms, text


def test_round_trip_preserves_record() -> None:
    graph = record_to_graph(CATALOG_RECORD, CATALOG_IRI)
    parsed = record_from_graph(graph, CATALOG_IRI)
    assert parsed == CATALOG_RECORD


def test_round_trip_root_record() -> None:
    root = ResourceDefinitionRecord(
        url_prefix="",
        name="Repository",
        schema_iri="https://w3id.org/fdp/o#Repository",
        children=(
            ChildLinkRecord(
                relation_uri="http://www.w3.org/ns/dcat#catalog",
                target_prefix="catalog",
                title="Catalogs",
            ),
        ),
    )
    parsed = record_from_graph(record_to_graph(root, ROOT_IRI), ROOT_IRI)
    assert parsed == root
    assert parsed.is_root


def test_optional_child_fields_round_trip_when_absent() -> None:
    record = ResourceDefinitionRecord(
        url_prefix="dataset",
        name="Dataset",
        schema_iri="http://www.w3.org/ns/dcat#Dataset",
        children=(
            ChildLinkRecord(
                relation_uri="http://www.w3.org/ns/dcat#distribution",
                target_prefix="distribution",
            ),
        ),
    )
    parsed = record_from_graph(record_to_graph(record, CATALOG_IRI), CATALOG_IRI)
    assert parsed.children[0].title == ""
    assert parsed.children[0].tags_uri is None


def test_children_parsed_in_deterministic_order() -> None:
    # Serialization order is not preserved by RDF; parse must sort stably.
    parsed = record_from_graph(record_to_graph(CATALOG_RECORD, CATALOG_IRI), CATALOG_IRI)
    relations = [c.relation_uri for c in parsed.children]
    assert relations == sorted(relations)


def test_missing_url_prefix_raises() -> None:
    graph = record_to_graph(CATALOG_RECORD, CATALOG_IRI)
    graph.remove((URIRef(CATALOG_IRI), FDP_URL_PREFIX, None))
    with pytest.raises(ResourceDefinitionParseError, match="fdp:urlPrefix"):
        record_from_graph(graph, CATALOG_IRI)


def test_missing_shape_pointer_raises() -> None:
    graph = record_to_graph(CATALOG_RECORD, CATALOG_IRI)
    graph.remove((URIRef(CATALOG_IRI), LDP.constrainedBy, None))
    with pytest.raises(ResourceDefinitionParseError, match="ldp:constrainedBy"):
        record_from_graph(graph, CATALOG_IRI)


def test_predefined_shape_accepts_serialized_record() -> None:
    graph = record_to_graph(CATALOG_RECORD, CATALOG_IRI)
    graph.add((URIRef(CATALOG_IRI), RDF.type, FDP_RESOURCE_DEFINITION))
    conforms, text = _conforms(graph)
    assert conforms, text


def test_predefined_shape_rejects_missing_name() -> None:
    graph = record_to_graph(CATALOG_RECORD, CATALOG_IRI)
    graph.add((URIRef(CATALOG_IRI), RDF.type, FDP_RESOURCE_DEFINITION))
    graph.remove((URIRef(CATALOG_IRI), FDP_NAME, None))
    conforms, _ = _conforms(graph)
    assert not conforms


def test_predefined_shape_rejects_literal_shape_pointer() -> None:
    # ldp:constrainedBy must be an IRI, not a literal.
    graph = record_to_graph(CATALOG_RECORD, CATALOG_IRI)
    graph.add((URIRef(CATALOG_IRI), RDF.type, FDP_RESOURCE_DEFINITION))
    graph.remove((URIRef(CATALOG_IRI), LDP.constrainedBy, None))
    graph.add((URIRef(CATALOG_IRI), LDP.constrainedBy, Literal("not-an-iri", datatype=XSD.string)))
    conforms, _ = _conforms(graph)
    assert not conforms


def test_shape_iri_constant_matches_shape_node() -> None:
    shape = predefined_shape_graph()
    sh_node_shape = URIRef("http://www.w3.org/ns/shacl#NodeShape")
    assert (URIRef(RD_SHAPE_IRI), RDF.type, sh_node_shape) in shape
