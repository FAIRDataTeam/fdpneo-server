"""Structural SPARQL safety gate (shared kernel).

Federation-off and no-remote-fetch are **deployment invariants**, not access
policies (security audit 2026-06-10, N-01). ``SERVICE`` and ``LOAD`` reach
outside the dataset to an arbitrary network host regardless of which graphs the
caller is authorized to project, so they are an SSRF vector on *every* SPARQL
surface. This module is the single source of truth for that gate; both the
access endpoint (``/sparql``, via :mod:`fdp.access.parser`) and the data
provider endpoint (``/data/{id}/sparql``) enforce it.

Authorization — *which* graphs a caller may read — stays per-module and
ODRL-driven (the access rewriter projects authorized named graphs; the data
provider evaluates the distribution's anonymous Offer). This gate is orthogonal
to that: it asks only "is this query form structurally safe to forward to the
store?", never "who may see what".
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from rdflib.plugins.sparql.parserutils import CompValue
from rdflib.plugins.sparql.processor import prepareQuery

from fdp.shared.errors import BadRequest

SERVICE_REJECTED_MESSAGE = "SERVICE clauses are not supported (no federation in v1)"
LOAD_REJECTED_MESSAGE = (
    "LOAD is not permitted: its source URL would be fetched by the "
    "triple store (SSRF risk)"
)


def reject_service(node: Any) -> None:
    """Raise :class:`BadRequest` if ``node`` contains a ``SERVICE`` clause anywhere.

    Operates on an already-parsed RDFLib algebra node so callers that have
    parsed for other reasons (the access parser extracts graph targets too) pay
    the parse cost only once.
    """
    for descendant in walk_compvalues(node):
        if descendant.name == "ServiceGraphPattern":
            raise BadRequest(SERVICE_REJECTED_MESSAGE)


def assert_query_safe(sparql: str) -> None:
    """Complete federation/SSRF gate for a **read-only** SPARQL surface.

    Ensures ``sparql`` is a well-formed read query (SELECT / ASK / CONSTRUCT /
    DESCRIBE) carrying no ``SERVICE`` clause, then returns ``None``. Update
    forms — including ``LOAD`` — are not queries and fail to parse here, so they
    are rejected as a side effect; this is correct for a query-only endpoint.
    Raises :class:`BadRequest` (message safe to surface) on any failure.

    The data provider's ``/data/{id}/sparql`` is query-only, so this is its
    whole gate. The access ``/sparql`` endpoint instead reuses the lower-level
    :func:`reject_service` primitive inside its richer parser, because it must
    also authorize updates and extract graph targets.
    """
    body = sparql.strip()
    if not body:
        raise BadRequest("SPARQL request body is empty")
    try:
        prepared = prepareQuery(body)
    except Exception as err:
        raise BadRequest(f"could not parse SPARQL read query: {err}") from err
    reject_service(prepared.algebra)


def walk_compvalues(node: Any) -> Iterator[CompValue]:
    """Yield ``node`` and every nested :class:`CompValue` descendant."""
    if isinstance(node, CompValue):
        yield node
        for value in node.values():  # pyright: ignore[reportUnknownVariableType]
            yield from walk_compvalues(value)
    elif isinstance(node, (list, tuple, set, frozenset)):
        for item in node:  # pyright: ignore[reportUnknownVariableType]
            yield from walk_compvalues(item)
    elif isinstance(node, dict):
        for value in node.values():  # pyright: ignore[reportUnknownVariableType]
            yield from walk_compvalues(value)


__all__ = [
    "LOAD_REJECTED_MESSAGE",
    "SERVICE_REJECTED_MESSAGE",
    "assert_query_safe",
    "reject_service",
    "walk_compvalues",
]
