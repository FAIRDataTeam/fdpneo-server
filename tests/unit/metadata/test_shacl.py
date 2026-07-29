"""Unit tests for :mod:`fdpneo_server.metadata.shacl`."""

from __future__ import annotations

from typing import cast

import pytest
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF

from fdpneo_server.metadata.shacl import (
    InMemoryShapeProvider,
    ShaclValidator,
    UnknownShapeError,
)
from fdpneo_server.shared.errors import SchemaViolation

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


# --- modular composition (shape-graph closure, task 15.2a) -----------------

RESOURCE_IRI = "https://example.org/shapes/resource"
CATALOG_IRI = "https://example.org/shapes/catalog"
DCAT_CATALOG = URIRef("http://www.w3.org/ns/dcat#Catalog")

# Base shape (a mixin, no target): requires dct:title.
RESOURCE_SHAPE_TTL = """
@prefix sh:  <http://www.w3.org/ns/shacl#> .
@prefix dct: <http://purl.org/dc/terms/> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
<https://example.org/shapes/resource>
    a sh:NodeShape ;
    sh:property [ sh:path dct:title ; sh:minCount 1 ; sh:datatype xsd:string ] .
"""

# Catalog targets dcat:Catalog and composes the Resource base via sh:node.
CATALOG_SHAPE_TTL = """
@prefix sh:   <http://www.w3.org/ns/shacl#> .
@prefix dcat: <http://www.w3.org/ns/dcat#> .
<https://example.org/shapes/catalog>
    a sh:NodeShape ;
    sh:targetClass dcat:Catalog ;
    sh:node <https://example.org/shapes/resource> .
"""


class _DictCountingProvider:
    """Multi-shape provider that records each fetch."""

    def __init__(self, shapes: dict[str, str]) -> None:
        self._shapes = dict(shapes)
        self.calls: list[str] = []

    async def fetch(self, shape_iri: str) -> str:
        self.calls.append(shape_iri)
        try:
            return self._shapes[shape_iri]
        except KeyError as err:
            raise UnknownShapeError(shape_iri) from err


def _catalog(*, with_title: bool) -> Graph:
    g = Graph()
    s = URIRef(EX + "c1")
    g.add((s, RDF.type, DCAT_CATALOG))
    if with_title:
        g.add((s, DCT_TITLE, Literal("ok", datatype=URIRef(XSD_STRING))))
    return g


@pytest.mark.unit
async def test_composed_shape_enforces_inherited_constraint() -> None:
    v = _validator({RESOURCE_IRI: RESOURCE_SHAPE_TTL, CATALOG_IRI: CATALOG_SHAPE_TTL})
    # A catalog without dct:title fails the *inherited* Resource constraint.
    bad = await v.validate_against(_catalog(with_title=False), CATALOG_IRI)
    assert bad.conforms is False
    assert str(DCT_TITLE) in {viol.result_path for viol in bad.violations}
    # With the inherited property present it conforms.
    good = await v.validate_against(_catalog(with_title=True), CATALOG_IRI)
    assert good.conforms is True


@pytest.mark.unit
async def test_transitive_closure_through_sh_node_and_sh_and() -> None:
    # A -> sh:node B -> sh:and ( Resource ) ; Resource requires dct:title.
    a = """
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix dcat: <http://www.w3.org/ns/dcat#> .
<https://example.org/shapes/a> a sh:NodeShape ; sh:targetClass dcat:Catalog ;
    sh:node <https://example.org/shapes/b> .
"""
    b = """
@prefix sh: <http://www.w3.org/ns/shacl#> .
<https://example.org/shapes/b> a sh:NodeShape ;
    sh:and ( <https://example.org/shapes/resource> ) .
"""
    v = _validator(
        {
            "https://example.org/shapes/a": a,
            "https://example.org/shapes/b": b,
            RESOURCE_IRI: RESOURCE_SHAPE_TTL,
        }
    )
    # The A→B→Resource chain is enforced: the title-less catalog fails. Non-
    # conformance is itself the proof of transitivity — an *unresolved* chain
    # would pass (see test_unresolvable_reference_is_tolerated). The node-level
    # sh:and message names the Resource shape it couldn't satisfy.
    report = await v.validate_against(_catalog(with_title=False), "https://example.org/shapes/a")
    assert report.conforms is False
    assert any(RESOURCE_IRI in (viol.message or "") for viol in report.violations)


@pytest.mark.unit
async def test_closure_fetches_each_member_once_and_caches_root() -> None:
    provider = _DictCountingProvider(
        {RESOURCE_IRI: RESOURCE_SHAPE_TTL, CATALOG_IRI: CATALOG_SHAPE_TTL}
    )
    v = ShaclValidator(provider)
    await v.validate_against(_catalog(with_title=True), CATALOG_IRI)
    assert sorted(provider.calls) == [CATALOG_IRI, RESOURCE_IRI]  # both, once each
    # Cached under the root; a second validation refetches nothing.
    await v.validate_against(_catalog(with_title=True), CATALOG_IRI)
    assert sorted(provider.calls) == [CATALOG_IRI, RESOURCE_IRI]
    assert CATALOG_IRI in v.cached_shapes()


@pytest.mark.unit
async def test_invalidating_base_shape_cascades_to_composed_shape() -> None:
    provider = _DictCountingProvider(
        {RESOURCE_IRI: RESOURCE_SHAPE_TTL, CATALOG_IRI: CATALOG_SHAPE_TTL}
    )
    v = ShaclValidator(provider)
    await v.validate_against(_catalog(with_title=True), CATALOG_IRI)
    assert CATALOG_IRI in v.cached_shapes()
    # Editing the *base* must drop the composed closure that imported it.
    v.invalidate(RESOURCE_IRI)
    assert CATALOG_IRI not in v.cached_shapes()
    # Re-validating rebuilds the closure (both fetched again).
    await v.validate_against(_catalog(with_title=True), CATALOG_IRI)
    assert provider.calls.count(RESOURCE_IRI) == 2


@pytest.mark.unit
async def test_unresolvable_reference_is_tolerated() -> None:
    # Catalog references a base that the provider doesn't have; the root still
    # resolves and validates against what is available.
    v = _validator({CATALOG_IRI: CATALOG_SHAPE_TTL})  # no resource shape
    report = await v.validate_against(_catalog(with_title=False), CATALOG_IRI)
    assert report.conforms is True  # the missing constraint simply isn't applied


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
