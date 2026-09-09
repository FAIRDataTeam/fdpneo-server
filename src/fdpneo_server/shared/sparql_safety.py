"""Structural SPARQL safety gate (shared kernel).

Federation-off and no-remote-fetch are **deployment invariants**, not access
policies (security audit 2026-06-10, N-01). ``SERVICE`` and ``LOAD`` reach
outside the dataset to an arbitrary network host regardless of which graphs the
caller is authorized to project, so they are an SSRF vector on *every* SPARQL
surface. This module is the single source of truth for that gate; both the
access endpoint (``/sparql``, via :mod:`fdpneo_server.access.parser`) and the data
provider endpoint (``/data/{id}/sparql``) enforce it.

Authorization — *which* graphs a caller may read — stays per-module and
ODRL-driven (the access rewriter projects authorized named graphs; the data
provider evaluates the distribution's anonymous Offer). This gate is orthogonal
to that: it asks only "is this query form structurally safe to forward to the
store?", never "who may see what".
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from rdflib.plugins.sparql.parserutils import CompValue
from rdflib.plugins.sparql.processor import prepareQuery

from fdpneo_server.shared.errors import BadRequest

SERVICE_REJECTED_MESSAGE = "SERVICE clauses are not supported (no federation in v1)"
LOAD_REJECTED_MESSAGE = (
    "LOAD is not permitted: its source URL would be fetched by the triple store (SSRF risk)"
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


def sparql_string_literal(value: str) -> str:
    """Render ``value`` as a quoted SPARQL string literal.

    JSON's string-escape rules are a safe subset of SPARQL's, so ``json.dumps``
    produces a literal the SPARQL parser accepts unchanged. This is the safe
    substitution path for caller-supplied values that must be interpolated into
    a query (e.g. an autocomplete prefix or a free-text search needle).
    """
    return json.dumps(value)


# Characters that must never appear inside a SPARQL ``<...>`` IRI reference:
# they are either syntactically meaningful or terminate the delimiter (SPARQL
# 1.1 §19.8, IRIREF). A string containing any of them is not a legal IRI, so
# rejecting it loses nothing — and a request-derived IRI that reaches a query
# unchecked is exactly how ``GET /x%3E`` used to break out of ``GRAPH <…>``.
FORBIDDEN_IRI_CHARS: frozenset[str] = frozenset(' \t\n\r<>"{}|^`\\')

_MAX_IRI_LENGTH = 2048


def is_sparql_safe_iri(iri: str) -> bool:
    """True iff ``iri`` may be inlined as ``<iri>`` in SPARQL.

    Conservative: rejects the empty string, anything over 2048 characters,
    the IRIREF-forbidden characters, and C0 control characters.
    """
    if not iri or len(iri) > _MAX_IRI_LENGTH:
        return False
    if any(c in FORBIDDEN_IRI_CHARS for c in iri):
        return False
    return not any(ord(c) < 0x20 for c in iri)


def require_sparql_safe_iri(iri: str, *, error: Exception | None = None) -> str:
    """Return ``iri`` unchanged, or raise ``error`` (default 400) if unsafe.

    The load-bearing edge gate: every request-derived IRI (an LDP record path,
    an admin-router identifier segment) passes through this before any module
    embeds it in SPARQL. Pass ``error=NotFound(...)`` where the caller wants
    the invalid identifier to be indistinguishable from a missing one.
    """
    if not is_sparql_safe_iri(iri):
        raise error if error is not None else BadRequest("not a valid IRI")
    return iri


def sparql_iri_ref(iri: str) -> str:
    """Render ``iri`` as a SPARQL IRI reference (``<iri>``), validating first.

    The adapter-level defence in depth for the two places that interpolate a
    graph URI directly (``construct_named_graph``, ``drop_graph``); the edge
    gate should already have rejected anything this refuses.
    """
    return f"<{require_sparql_safe_iri(iri)}>"


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
    "FORBIDDEN_IRI_CHARS",
    "LOAD_REJECTED_MESSAGE",
    "SERVICE_REJECTED_MESSAGE",
    "assert_query_safe",
    "is_sparql_safe_iri",
    "reject_service",
    "require_sparql_safe_iri",
    "sparql_iri_ref",
    "sparql_string_literal",
    "walk_compvalues",
]
