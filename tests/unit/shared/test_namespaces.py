"""Tests for ``fdp.shared.namespaces``."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import HttpUrl
from rdflib import Graph, Namespace, URIRef

from fdp.shared.namespaces import (
    DCAT,
    DCT,
    FDP_DEFAULT,
    FOAF,
    LDP,
    ODRL,
    OWL,
    PREFIXES,
    PROV,
    SH,
    XSD,
    bind_all,
    fdp_namespace,
)


@pytest.mark.unit
def test_standard_namespaces_have_expected_iris() -> None:
    assert str(DCAT) == "http://www.w3.org/ns/dcat#"
    assert str(DCT) == "http://purl.org/dc/terms/"
    assert str(FOAF) == "http://xmlns.com/foaf/0.1/"
    assert str(LDP) == "http://www.w3.org/ns/ldp#"
    assert str(ODRL) == "http://www.w3.org/ns/odrl/2/"
    assert str(OWL) == "http://www.w3.org/2002/07/owl#"
    assert str(PROV) == "http://www.w3.org/ns/prov#"
    assert str(SH) == "http://www.w3.org/ns/shacl#"
    assert str(XSD) == "http://www.w3.org/2001/XMLSchema#"


@pytest.mark.unit
def test_namespace_resolves_terms() -> None:
    assert DCAT.Dataset == URIRef("http://www.w3.org/ns/dcat#Dataset")
    assert SH.NodeShape == URIRef("http://www.w3.org/ns/shacl#NodeShape")


@pytest.mark.unit
def test_prefixes_mapping_is_lowercase_and_immutable() -> None:
    assert set(PREFIXES) == {
        "adms",
        "dcat",
        "dct",
        "fdp-o",
        "foaf",
        "ldp",
        "odrl",
        "owl",
        "prof",
        "prov",
        "rdfs",
        "role",
        "sh",
        "skos",
        "spdx",
        "xsd",
    }
    with pytest.raises(TypeError):
        PREFIXES["xx"] = Namespace("http://example.org/xx#")  # type: ignore[index]


@pytest.mark.unit
def test_fdp_default_is_w3id_namespace() -> None:
    assert str(FDP_DEFAULT) == "https://w3id.org/fdp/o#"


@pytest.mark.unit
def test_fdp_namespace_reads_from_explicit_settings(make_settings: Any) -> None:
    custom = "https://example.org/fdp-ns#"
    settings = make_settings(fdp_namespace=HttpUrl(custom))
    ns = fdp_namespace(settings)
    assert str(ns) == custom
    assert ns.MetadataIdentifier == URIRef(custom + "MetadataIdentifier")


@pytest.mark.unit
def test_fdp_namespace_default_value_from_settings(make_settings: Any) -> None:
    settings = make_settings()
    assert str(fdp_namespace(settings)) == "https://w3id.org/fdp/o#"


@pytest.mark.unit
def test_bind_all_registers_every_prefix(make_settings: Any) -> None:
    graph = Graph()
    bind_all(graph, settings=make_settings(fdp_namespace=HttpUrl("https://example.org/fdp#")))

    bound = {prefix: str(namespace) for prefix, namespace in graph.namespace_manager.namespaces()}
    for prefix, namespace in PREFIXES.items():
        assert bound[prefix] == str(namespace)
    assert bound["fdp"] == "https://example.org/fdp#"
