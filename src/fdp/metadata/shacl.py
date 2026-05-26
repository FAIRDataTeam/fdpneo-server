"""SHACL validation for record graphs.

Wraps pySHACL with a cache over parsed shape graphs.

**Design notes**

* pySHACL exposes no stable public "compiled shape" type, but parsing
  Turtle into an :class:`rdflib.Graph` is by far the costly step. The
  cache holds the parsed shape graph keyed by its IRI; pySHACL accepts
  it as ``shacl_graph`` on every call.
* Shape resolution is pluggable through :class:`ShapeProvider`. The
  default deployment provider (wired by the LDP layer) reads shapes from
  the metadata repository; tests pass an in-memory provider.
* :meth:`ShaclValidator.bootstrap` pre-loads the deployment's declared
  schemas. Runtime requests for an unwarmed shape fall back to
  compile-on-first-use.
* Validation is CPU-bound. The async surface exists because
  :class:`ShapeProvider` is async; the actual pySHACL call runs
  in-thread. If a future record turns out heavy enough to stall the
  event loop, the caller can offload via ``asyncio.to_thread``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol, cast, runtime_checkable

import pyshacl  # type: ignore[import-untyped]
from rdflib import Graph, URIRef
from rdflib.term import Node

from fdp.shared.errors import SchemaViolation
from fdp.shared.namespaces import SH


@dataclass(frozen=True)
class Violation:
    """A single ``sh:ValidationResult`` extracted from the pySHACL report."""

    focus_node: str | None
    result_path: str | None
    source_shape: str | None
    severity: str | None
    message: str | None
    value: str | None


@dataclass(frozen=True)
class ValidationReport:
    """Outcome of validating a data graph against a shape graph."""

    conforms: bool
    shape_iri: str
    violations: tuple[Violation, ...] = ()

    def raise_if_failed(self) -> None:
        """Raise :class:`SchemaViolation` carrying the structured report on failure."""
        if self.conforms:
            return
        raise SchemaViolation(
            f"Graph failed SHACL validation against {self.shape_iri}",
            details={
                "shape": self.shape_iri,
                "violations": [
                    {
                        "focus_node": v.focus_node,
                        "result_path": v.result_path,
                        "source_shape": v.source_shape,
                        "severity": v.severity,
                        "message": v.message,
                        "value": v.value,
                    }
                    for v in self.violations
                ],
            },
        )


class UnknownShapeError(LookupError):
    """The requested SHACL shape IRI is not registered with the provider."""


@runtime_checkable
class ShapeProvider(Protocol):
    """Resolves a shape IRI to a Turtle-serialized SHACL shape graph."""

    async def fetch(self, shape_iri: str) -> str: ...


class InMemoryShapeProvider:
    """Provider backed by an in-process dict of IRI to Turtle. Tests and bootstrap."""

    def __init__(self, shapes: dict[str, str]) -> None:
        self._shapes = dict(shapes)

    def register(self, shape_iri: str, turtle: str) -> None:
        self._shapes[shape_iri] = turtle

    async def fetch(self, shape_iri: str) -> str:
        try:
            return self._shapes[shape_iri]
        except KeyError as err:
            raise UnknownShapeError(shape_iri) from err


class ShaclValidator:
    """Validate data graphs against SHACL shapes with a parsed-shape cache."""

    def __init__(self, provider: ShapeProvider) -> None:
        self._provider = provider
        self._cache: dict[str, Graph] = {}

    async def bootstrap(self, shape_iris: Iterable[str]) -> None:
        """Pre-compile the given shape IRIs into the cache."""
        for iri in shape_iris:
            await self._load(iri)

    async def validate_against(self, data_graph: Graph, shape_iri: str) -> ValidationReport:
        """Run pySHACL on ``data_graph`` against the shape identified by ``shape_iri``."""
        shape_graph = await self._load(shape_iri)
        conforms, results_graph, _ = cast(
            tuple[bool, Graph, str],
            pyshacl.validate(  # pyright: ignore[reportUnknownMemberType]
                data_graph=data_graph,
                shacl_graph=shape_graph,
                inference="none",
                advanced=False,
                meta_shacl=False,
                debug=False,
                inplace=False,
            ),
        )
        if conforms:
            return ValidationReport(conforms=True, shape_iri=shape_iri)
        return ValidationReport(
            conforms=False,
            shape_iri=shape_iri,
            violations=_extract_violations(results_graph),
        )

    def cached_shapes(self) -> frozenset[str]:
        """Diagnostic: IRIs whose shape graphs are currently cached."""
        return frozenset(self._cache)

    def invalidate(self, shape_iri: str) -> None:
        """Drop ``shape_iri`` from the cache. Next use will refetch."""
        self._cache.pop(shape_iri, None)

    async def _load(self, shape_iri: str) -> Graph:
        cached = self._cache.get(shape_iri)
        if cached is not None:
            return cached
        ttl = await self._provider.fetch(shape_iri)
        graph = Graph()
        graph.parse(data=ttl, format="turtle")
        self._cache[shape_iri] = graph
        return graph


def _extract_violations(report: Graph) -> tuple[Violation, ...]:
    """Pull each ``sh:ValidationResult`` from a pySHACL report graph."""
    rows: list[Violation] = []
    for result in report.subjects(SH.resultSeverity, None):
        rows.append(
            Violation(
                focus_node=_str_or_none(_one(report, result, SH.focusNode)),
                result_path=_str_or_none(_one(report, result, SH.resultPath)),
                source_shape=_str_or_none(_one(report, result, SH.sourceShape)),
                severity=_str_or_none(_one(report, result, SH.resultSeverity)),
                message=_str_or_none(_one(report, result, SH.resultMessage)),
                value=_str_or_none(_one(report, result, SH.value)),
            )
        )
    rows.sort(
        key=lambda v: (
            v.focus_node or "",
            v.result_path or "",
            v.source_shape or "",
            v.message or "",
        )
    )
    return tuple(rows)


def _one(graph: Graph, subject: Any, predicate: URIRef) -> Node | None:
    for value in graph.objects(subject, predicate):
        return value
    return None


def _str_or_none(node: Node | None) -> str | None:
    return None if node is None else str(node)


__all__ = [
    "InMemoryShapeProvider",
    "ShaclValidator",
    "ShapeProvider",
    "UnknownShapeError",
    "ValidationReport",
    "Violation",
]
