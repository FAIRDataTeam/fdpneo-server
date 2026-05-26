"""Unit tests for :mod:`fdp.metadata.shacl`."""

from __future__ import annotations

from typing import cast

import pytest
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF

from fdp.metadata.shacl import (
    InMemoryShapeProvider,
    ShaclValidator,
    UnknownShapeError,
)
from fdp.shared.errors import SchemaViolation

EX = "https://example.org/"
SHAPE_IRI = "https://example.org/shapes/dataset"
DCAT_DATASET = URIRef("http://www.w3.org/ns/dcat#Dataset")
DCT_TITLE = URIRef("http://purl.org/dc/terms/title")
XSD_STRING = "http://www.w3.org/2001/XMLSchema#string"


DATASET_SHAPE_TTL = """
@prefix sh:   <http://www.w3.org/ns/shacl#> .
@prefix dcat: <http://www.w3.org/ns/dcat#> .
@prefix dct:  <http://purl.org/dc/terms/> .
@prefix xsd:  <http://www.w3.org/2001/XMLSchema#> .

<https://example.org/shapes/dataset>
    a sh:NodeShape ;
    sh:targetClass dcat:Dataset ;
    sh:property [
        sh:path dct:title ;
        sh:minCount 1 ;
        sh:datatype xsd:string ;
    ] .
"""


def _valid_dataset() -> Graph:
    g = Graph()
    s = URIRef(EX + "d1")
    g.add((s, RDF.type, DCAT_DATASET))
    g.add((s, DCT_TITLE, Literal("ok", datatype=URIRef(XSD_STRING))))
    return g


def _dataset_without_title() -> Graph:
    g = Graph()
    s = URIRef(EX + "d2")
    g.add((s, RDF.type, DCAT_DATASET))
    return g


def _validator(shapes: dict[str, str] | None = None) -> ShaclValidator:
    return ShaclValidator(InMemoryShapeProvider(shapes or {SHAPE_IRI: DATASET_SHAPE_TTL}))


class _CountingProvider:
    """Records each fetch so the cache behavior can be asserted on."""

    def __init__(self, turtle: str) -> None:
        self._turtle = turtle
        self.calls: list[str] = []

    async def fetch(self, shape_iri: str) -> str:
        self.calls.append(shape_iri)
        return self._turtle


@pytest.mark.unit
async def test_validate_passes_on_conforming_graph() -> None:
    v = _validator()
    report = await v.validate_against(_valid_dataset(), SHAPE_IRI)
    assert report.conforms is True
    assert report.violations == ()
    assert report.shape_iri == SHAPE_IRI


@pytest.mark.unit
async def test_validate_fails_when_required_property_missing() -> None:
    v = _validator()
    report = await v.validate_against(_dataset_without_title(), SHAPE_IRI)
    assert report.conforms is False
    assert report.violations
    paths = {violation.result_path for violation in report.violations}
    assert str(DCT_TITLE) in paths


@pytest.mark.unit
async def test_raise_if_failed_emits_schema_violation_with_report() -> None:
    v = _validator()
    report = await v.validate_against(_dataset_without_title(), SHAPE_IRI)
    with pytest.raises(SchemaViolation) as excinfo:
        report.raise_if_failed()
    details = cast(dict[str, object], excinfo.value.details)
    assert details["shape"] == SHAPE_IRI
    violations = cast(list[dict[str, str | None]], details["violations"])
    assert violations
    assert violations[0]["result_path"] == str(DCT_TITLE)


@pytest.mark.unit
async def test_raise_if_failed_is_noop_when_conforms() -> None:
    v = _validator()
    report = await v.validate_against(_valid_dataset(), SHAPE_IRI)
    report.raise_if_failed()  # must not raise


@pytest.mark.unit
async def test_unknown_shape_raises_unknown_shape_error() -> None:
    v = _validator(shapes={})
    with pytest.raises(UnknownShapeError):
        await v.validate_against(_valid_dataset(), "https://example.org/shapes/unknown")


@pytest.mark.unit
async def test_shape_is_fetched_once_then_cached() -> None:
    provider = _CountingProvider(DATASET_SHAPE_TTL)
    v = ShaclValidator(provider)
    await v.validate_against(_valid_dataset(), SHAPE_IRI)
    await v.validate_against(_valid_dataset(), SHAPE_IRI)
    assert provider.calls == [SHAPE_IRI]
    assert SHAPE_IRI in v.cached_shapes()


@pytest.mark.unit
async def test_bootstrap_pre_compiles_shapes() -> None:
    provider = _CountingProvider(DATASET_SHAPE_TTL)
    v = ShaclValidator(provider)
    await v.bootstrap([SHAPE_IRI])
    assert provider.calls == [SHAPE_IRI]
    # subsequent validation does not re-fetch
    await v.validate_against(_valid_dataset(), SHAPE_IRI)
    assert provider.calls == [SHAPE_IRI]


@pytest.mark.unit
async def test_invalidate_removes_cached_shape() -> None:
    provider = _CountingProvider(DATASET_SHAPE_TTL)
    v = ShaclValidator(provider)
    await v.validate_against(_valid_dataset(), SHAPE_IRI)
    assert SHAPE_IRI in v.cached_shapes()
    v.invalidate(SHAPE_IRI)
    assert SHAPE_IRI not in v.cached_shapes()
    await v.validate_against(_valid_dataset(), SHAPE_IRI)
    assert provider.calls == [SHAPE_IRI, SHAPE_IRI]


@pytest.mark.unit
async def test_violations_are_sorted_deterministically() -> None:
    v = _validator()
    g = Graph()
    s1, s2 = URIRef(EX + "d_a"), URIRef(EX + "d_b")
    g.add((s1, RDF.type, DCAT_DATASET))
    g.add((s2, RDF.type, DCAT_DATASET))
    report = await v.validate_against(g, SHAPE_IRI)
    assert not report.conforms
    focus_nodes = [v.focus_node or "" for v in report.violations]
    assert focus_nodes == sorted(focus_nodes)
