"""Content negotiation for SPARQL endpoint responses.

SPARQL has two answer shapes:

* **Solution sequences** — ``SELECT`` and ``ASK`` queries return rows /
  a boolean serialized as SPARQL Results JSON, XML, CSV, or TSV.
* **RDF graphs** — ``CONSTRUCT`` and ``DESCRIBE`` queries return a graph
  serialized as Turtle, JSON-LD, RDF/XML, or N-Triples.

This module picks the right format per query form using the standard
Accept-header negotiation rules from
:mod:`fdpneo_server.shared.negotiation`, but with a query-form-aware
candidate set so a ``SELECT`` never accidentally gets a Turtle response.

Default media types when the client sends no Accept header — or only
``*/*`` — are the SPARQL convention:

* Solutions: ``application/sparql-results+json``
* Graphs: ``text/turtle``
"""

from __future__ import annotations

from fdpneo_server.access.parser import QueryForm
from fdpneo_server.shared.negotiation import (
    JSON_LD,
    N_TRIPLES,
    RDF_XML,
    TURTLE,
    parse_accept,
)

SPARQL_RESULTS_JSON = "application/sparql-results+json"
SPARQL_RESULTS_XML = "application/sparql-results+xml"
SPARQL_RESULTS_CSV = "text/csv"
SPARQL_RESULTS_TSV = "text/tab-separated-values"


# Server-preferred order for solution responses (SELECT / ASK).
SOLUTION_TYPES: tuple[str, ...] = (
    SPARQL_RESULTS_JSON,
    SPARQL_RESULTS_XML,
    SPARQL_RESULTS_CSV,
    SPARQL_RESULTS_TSV,
)

# Server-preferred order for RDF-graph responses (CONSTRUCT / DESCRIBE).
GRAPH_TYPES: tuple[str, ...] = (
    TURTLE,
    JSON_LD,
    RDF_XML,
    N_TRIPLES,
)


def candidates_for(form: QueryForm) -> tuple[str, ...]:
    """Return the supported media types for ``form`` in preference order."""
    if form in (QueryForm.CONSTRUCT, QueryForm.DESCRIBE):
        return GRAPH_TYPES
    return SOLUTION_TYPES


def select_result_media_type(form: QueryForm, accept: str | None) -> str | None:
    """Negotiate the best supported result media type, or ``None`` if no match.

    ``None`` lets the router respond with 406 Not Acceptable.
    """
    candidates = candidates_for(form)
    candidate_set = set(candidates)
    ranges = parse_accept(accept)
    ranked = sorted(
        (r for r in ranges if r.quality > 0.0),
        key=lambda r: r.quality,
        reverse=True,
    )
    for r in ranked:
        if r.media_type == "*/*":
            return candidates[0]
        if r.media_type.endswith("/*"):
            prefix = r.media_type[:-1]
            for supported in candidates:
                if supported.startswith(prefix):
                    return supported
            continue
        if r.media_type in candidate_set:
            return r.media_type
    return None


__all__ = [
    "GRAPH_TYPES",
    "SOLUTION_TYPES",
    "SPARQL_RESULTS_CSV",
    "SPARQL_RESULTS_JSON",
    "SPARQL_RESULTS_TSV",
    "SPARQL_RESULTS_XML",
    "candidates_for",
    "select_result_media_type",
]
