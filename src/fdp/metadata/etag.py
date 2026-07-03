"""Deterministic ETag computation for record graphs.

Strategy: serialize the graph as N-Triples, split on line boundaries,
sort the lines, then BLAKE2b-128 the joined result. The N-Triples format
is one statement per line and quotes literals canonically, so as long as
the triple set is the same, the lines after sort are the same.

128 bits is plenty for collision avoidance at any realistic scale and
keeps the ETag header value short (32 hex chars).

**Blank nodes**

rdflib mints blank-node labels per-graph deterministically per process but not
across processes — a graph round-tripped through serialization (as every record
is on read) gets different ``_:bN`` labels, which would make the digest unstable
and break ``If-Match``. When a graph contains blank nodes we therefore
canonically relabel them (rdflib's graph-isomorphism canonicalization) before
serializing, so the digest depends only on the triple structure. Records with no
blank nodes — the common case — skip that step, so their ETags are unchanged and
the fast path stays fast. Structured alternative identifiers (``adms:identifier``
nodes, ADR-0017) are the motivating case.
"""

from __future__ import annotations

from hashlib import blake2b

from rdflib import BNode, Graph
from rdflib.compare import to_canonical_graph


def compute_etag(graph: Graph) -> str:
    """Return a deterministic BLAKE2b-128 hex digest of ``graph``.

    Stable across serialization round-trips even when the graph carries blank
    nodes (they are canonically relabeled first; graphs without them skip it).
    """
    if any(isinstance(term, BNode) for triple in graph for term in triple):
        graph = to_canonical_graph(graph)
    nt = graph.serialize(format="nt")
    lines = sorted(line for line in nt.splitlines() if line.strip())
    payload = "\n".join(lines).encode("utf-8")
    return blake2b(payload, digest_size=16).hexdigest()


__all__ = ["compute_etag"]
