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
import structlog
from rdflib import Graph, URIRef
from rdflib.term import Node

from fdpneo_server.shared.errors import SchemaViolation
from fdpneo_server.shared.namespaces import SH

log = structlog.get_logger(__name__)


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
    """Validate data graphs against SHACL shapes with a parsed-shape cache.

    Shapes may be **modular**: a shape can compose others by IRI via ``sh:node``,
    ``sh:and``/``sh:or``/``sh:xone`` lists, ``sh:qualifiedValueShape`` or
    ``sh:not``. pySHACL needs every referenced shape present in the one
    ``shacl_graph`` it is handed, so :meth:`_load` assembles the **closure** —
    the requested shape plus every shape it transitively references, resolved
    through the :class:`ShapeProvider` and merged into a single graph. The
    closure is cached under the requested (root) IRI; :attr:`_members` records
    which shape IRIs each closure pulled in so :meth:`invalidate` can cascade
    (editing a base shape drops every composed shape that imports it).
    """

    def __init__(self, provider: ShapeProvider) -> None:
        self._provider = provider
        self._cache: dict[str, Graph] = {}
        self._members: dict[str, set[str]] = {}

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

    async def shape_closure(self, shape_iri: str) -> Graph:
        """The merged shape-graph closure rooted at ``shape_iri``.

        The requested shape plus every shape it composes (assembled by
        :meth:`_load`), so a single response carries all inherited property
        shapes — what the client's DASH form renderer needs for a composed type.
        Returns the cached graph; callers must treat it as read-only.
        """
        return await self._load(shape_iri)

    def cached_shapes(self) -> frozenset[str]:
        """Diagnostic: IRIs whose shape graphs are currently cached."""
        return frozenset(self._cache)

    def invalidate(self, shape_iri: str) -> None:
        """Drop every cached closure that includes ``shape_iri``; next use refetches.

        Cascades: a composed shape's closure is dropped when *any* shape it
        imports — including itself — is invalidated, so editing a base shape
        (``ResourceShape``) rebuilds every type shape that references it.
        """
        for root in [r for r, members in self._members.items() if shape_iri in members]:
            self._cache.pop(root, None)
            self._members.pop(root, None)

    async def _load(self, shape_iri: str) -> Graph:
        """Return the cached/assembled shape-graph closure rooted at ``shape_iri``."""
        cached = self._cache.get(shape_iri)
        if cached is not None:
            return cached
        closure = Graph()
        members: set[str] = set()
        pending = [shape_iri]
        while pending:
            iri = pending.pop()
            if iri in members:
                continue
            try:
                ttl = await self._provider.fetch(iri)
            except UnknownShapeError:
                if iri == shape_iri:
                    raise  # the requested shape itself must resolve
                # A referenced shape we can't resolve: mark visited (so a later
                # publish of it invalidates this closure) and skip — the rest of
                # the composition still validates.
                members.add(iri)
                log.warning("shacl_referenced_shape_unresolved", root=shape_iri, missing=iri)
                continue
            members.add(iri)
            fragment = Graph()
            fragment.parse(data=ttl, format="turtle")
            for triple in fragment:
                closure.add(triple)
            pending.extend(ref for ref in _referenced_shape_iris(fragment) if ref not in members)
        self._cache[shape_iri] = closure
        self._members[shape_iri] = members
        return closure


def _referenced_shape_iris(graph: Graph) -> set[str]:
    """IRIs of shapes ``graph`` composes — to pull into the validation closure.

    Follows the SHACL constraint components that take a shape as their value:
    ``sh:node``, ``sh:qualifiedValueShape`` and ``sh:not`` (a single shape), and
    ``sh:and``/``sh:or``/``sh:xone`` (an RDF list of shapes). Only IRI references
    are returned — blank-node shapes are already inline in ``graph``.
    """
    refs: set[str] = set()
    for pred in (SH.node, SH.qualifiedValueShape, SH["not"]):
        refs.update(str(o) for o in graph.objects(None, pred) if isinstance(o, URIRef))
    for pred in (SH["and"], SH["or"], SH.xone):
        for list_node in graph.objects(None, pred):
            try:
                members = list(graph.items(list_node))
            except (ValueError, TypeError):  # malformed list — ignore
                continue
            refs.update(str(m) for m in members if isinstance(m, URIRef))
    return refs


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
