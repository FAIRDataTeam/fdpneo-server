"""Unit tests for :class:`MetadataShapeProvider`."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from rdflib import Graph

from fdp.metadata.shacl import (
    ShaclValidator,
    UnknownShapeError,
    ValidationReport,
)
from fdp.metadata.shape_provider import MetadataShapeProvider


# A trivial SHACL shape that requires dct:title on dcat:Catalog records.
CATALOG_SHAPE_TTL = """\
@prefix sh:   <http://www.w3.org/ns/shacl#> .
@prefix dcat: <http://www.w3.org/ns/dcat#> .
@prefix dct:  <http://purl.org/dc/terms/> .
@prefix xsd:  <http://www.w3.org/2001/XMLSchema#> .

<http://www.w3.org/ns/dcat#Catalog>
    a sh:NodeShape ;
    sh:targetClass dcat:Catalog ;
    sh:property [
        sh:path dct:title ;
        sh:minCount 1 ;
        sh:datatype xsd:string ;
    ] .
"""


@dataclass
class _FakeRepo:
    """In-memory repo keyed by IRI → Turtle source."""

    graphs: dict[str, str] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    async def get_graph(self, record_uri: str) -> Graph:
        self.calls.append(record_uri)
        g = Graph()
        ttl = self.graphs.get(record_uri)
        if ttl:
            g.parse(data=ttl, format="turtle")
        return g


# --- happy path ----------------------------------------------------------


@pytest.mark.unit
async def test_fetch_returns_turtle_for_a_known_shape() -> None:
    iri = "http://www.w3.org/ns/dcat#Catalog"
    repo = _FakeRepo(graphs={iri: CATALOG_SHAPE_TTL})
    provider = MetadataShapeProvider(repo)  # type: ignore[arg-type]
    ttl = await provider.fetch(iri)
    assert "sh:NodeShape" in ttl
    assert "dcat:Catalog" in ttl
    assert repo.calls == [iri]


# --- unknown shape -------------------------------------------------------


@pytest.mark.unit
async def test_fetch_raises_unknown_for_empty_graph() -> None:
    repo = _FakeRepo()  # no shapes registered
    provider = MetadataShapeProvider(repo)  # type: ignore[arg-type]
    with pytest.raises(UnknownShapeError):
        await provider.fetch("http://example.org/missing-shape")


# --- integration with ShaclValidator -------------------------------------


@pytest.mark.unit
async def test_validator_uses_provider_for_validation_decisions() -> None:
    iri = "http://www.w3.org/ns/dcat#Catalog"
    repo = _FakeRepo(graphs={iri: CATALOG_SHAPE_TTL})
    validator = ShaclValidator(MetadataShapeProvider(repo))  # type: ignore[arg-type]

    # A catalog with a title conforms.
    ok_graph = Graph()
    ok_graph.parse(
        data="""\
@prefix dcat: <http://www.w3.org/ns/dcat#> .
@prefix dct:  <http://purl.org/dc/terms/> .
<http://example.org/cat-1>
    a dcat:Catalog ;
    dct:title "Public catalog" .
""",
        format="turtle",
    )
    report: ValidationReport = await validator.validate_against(ok_graph, iri)
    assert report.conforms is True

    # A catalog without a title fails.
    bad_graph = Graph()
    bad_graph.parse(
        data="""\
@prefix dcat: <http://www.w3.org/ns/dcat#> .
<http://example.org/cat-2>
    a dcat:Catalog .
""",
        format="turtle",
    )
    report = await validator.validate_against(bad_graph, iri)
    assert report.conforms is False
    assert len(report.violations) >= 1


@pytest.mark.unit
async def test_validator_bootstrap_warms_the_cache() -> None:
    iri = "http://www.w3.org/ns/dcat#Catalog"
    repo = _FakeRepo(graphs={iri: CATALOG_SHAPE_TTL})
    validator = ShaclValidator(MetadataShapeProvider(repo))  # type: ignore[arg-type]

    assert iri not in validator.cached_shapes()
    await validator.bootstrap([iri])
    assert iri in validator.cached_shapes()
    # The provider was called once during bootstrap.
    assert repo.calls == [iri]

    # A subsequent validate hits the cache — no extra fetch.
    graph = Graph()
    graph.parse(
        data="""\
@prefix dcat: <http://www.w3.org/ns/dcat#> .
@prefix dct:  <http://purl.org/dc/terms/> .
<http://example.org/c>
    a dcat:Catalog ;
    dct:title "T" .
""",
        format="turtle",
    )
    await validator.validate_against(graph, iri)
    assert repo.calls == [iri]  # still just the bootstrap call
