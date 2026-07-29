"""Local SPARQL Update simulation for LDP PATCH.

The LDP PATCH pipeline (architecture §10.3, ADR-0008) must validate the
*post-update* state against SHACL before anything touches the triple store.
Doing that requires simulating the update in-process against a copy of the
current record graph, validating the result, and only then committing.

This module is the simulation step. It is intentionally pure:

* No I/O — the caller fetches the current graph and commits the new one.
* No SHACL — the LDP layer calls the validator on the returned graph.
* No event emission — the LDP layer publishes ``RecordModified`` after the
  storage commit succeeds.

**Security posture**

* ``SERVICE`` is rejected outright. A successful LDP PATCH must not be a
  side-channel for outbound HTTP requests from the server.
* ``BASE <iri>`` is prepended so the ``<>`` empty IRI in the user's body
  resolves to the resource being patched, exactly as the architecture
  example shows. Any explicit ``BASE`` directive the user includes overrides
  ours — which is fine; rdflib uses the *last* base declaration.
* The pure-function contract means a SHACL failure later in the pipeline
  drops the simulated graph on the floor; the live store is untouched.

Future ticket 3.x (the SPARQL endpoint) will introduce algebra-level
classification of updates (read vs. update, referenced graphs, SERVICE
detection at the parser). For PATCH the rules are simpler — the target
graph is fixed by the URL — so a focused text-level guard plus rdflib's
own parser is sufficient here.
"""

from __future__ import annotations

import re

from rdflib import Graph

from fdpneo_server.shared.errors import BadRequest

_SERVICE_RE = re.compile(r"\bservice\b", re.IGNORECASE)


def simulate_update(current: Graph, sparql_update: str, resource_iri: str) -> Graph:
    """Apply ``sparql_update`` to a copy of ``current``, return the result.

    Args:
        current: The record graph as it currently lives in the triple store.
        sparql_update: The user's SPARQL Update body.
        resource_iri: The resource being patched; used as the BASE so ``<>``
            resolves correctly in ``INSERT DATA { <> ... }`` style bodies.

    Returns:
        A fresh :class:`Graph` reflecting the post-update state. ``current``
        is never mutated.

    Raises:
        BadRequest: The body is empty, references ``SERVICE``, or cannot be
            parsed by rdflib's SPARQL Update processor.
    """
    body = sparql_update.strip()
    if not body:
        raise BadRequest("SPARQL Update body is empty")
    if _SERVICE_RE.search(body):
        raise BadRequest("SERVICE clauses are not permitted in PATCH bodies")

    new_graph = _copy(current)
    augmented = f"BASE <{resource_iri}>\n{body}"
    try:
        new_graph.update(augmented)
    except Exception as err:  # rdflib raises a heterogeneous tree of errors
        raise BadRequest(f"could not parse SPARQL Update: {err}") from err
    return new_graph


def _copy(graph: Graph) -> Graph:
    """Return a fresh :class:`Graph` carrying the same triples as ``graph``.

    Avoids ``Graph.__iadd__`` / ``+`` to keep blank-node identifiers stable.
    """
    copy = Graph()
    for triple in graph:
        copy.add(triple)
    return copy


__all__ = ["simulate_update"]
