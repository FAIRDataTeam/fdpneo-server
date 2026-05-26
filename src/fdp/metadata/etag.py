"""Deterministic ETag computation for record graphs.

Strategy: serialize the graph as N-Triples, split on line boundaries,
sort the lines, then BLAKE2b-128 the joined result. The N-Triples format
is one statement per line and quotes literals canonically, so as long as
the triple set is the same, the lines after sort are the same.

128 bits is plenty for collision avoidance at any realistic scale and
keeps the ETag header value short (32 hex chars).

**Blank nodes**

rdflib mints blank-node labels per-graph deterministically per process
but not across processes — a graph round-tripped through serialization
may get different ``_:b0`` labels. For ETag stability we'd need a true
graph isomorphism canonicalization (URDNA2015). That's out of scope here;
practical FDP records reference URIs, not bare blank-nodes, and meta /
audit graphs are constructed in-process so the labels stay stable.
"""

from __future__ import annotations

from hashlib import blake2b

from rdflib import Graph


def compute_etag(graph: Graph) -> str:
    """Return a deterministic BLAKE2b-128 hex digest of ``graph``."""
    nt = graph.serialize(format="nt")
    lines = sorted(line for line in nt.splitlines() if line.strip())
    payload = "\n".join(lines).encode("utf-8")
    return blake2b(payload, digest_size=16).hexdigest()


__all__ = ["compute_etag"]
