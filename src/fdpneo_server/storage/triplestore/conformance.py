"""Named-graph isolation self-test (security audit F-03 / R-03).

The SPARQL access PEP authorizes reads by projecting exactly the caller's
authorized graphs through repeated ``named-graph-uri`` protocol parameters
(:func:`fdpneo_server.access.rewriter.rewrite_read`). That is only safe if the triple
store **honors** that projection — restricting the query to the named set and
excluding every other graph. Some stores (notably Oxigraph) mishandle *repeated*
``named-graph-uri`` parameters, which would let a multi-graph read surface graphs
outside the authorized set — a cross-record/PHI leak.

This module probes that property at startup so the capability is *enforced*, not
assumed. It writes three disjoint probe graphs, runs a count restricted to two of
them, and requires the result to be exactly ``2`` — proving the third (and every
other graph in the store) is correctly excluded *and* that repeated parameters
are unioned. On failure, the deployment fails closed: multi-graph SPARQL reads
are disabled (see the SPARQL router gate).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Final

import structlog

if TYPE_CHECKING:
    from fdpneo_server.storage.triplestore.adapter import TripleStoreAdapter

log = structlog.get_logger(__name__)

_A: Final = "urn:fdp:conformance-probe:a"
_B: Final = "urn:fdp:conformance-probe:b"
_C: Final = "urn:fdp:conformance-probe:c"
_PROBES: Final = (_A, _B, _C)
_COUNT_QUERY: Final = "SELECT (COUNT(*) AS ?c) WHERE { GRAPH ?g { ?s ?p ?o } }"


async def verify_named_graph_isolation(adapter: TripleStoreAdapter) -> bool:
    """Return ``True`` iff the store correctly isolates repeated ``named-graph-uri``.

    Writes three single-triple probe graphs, counts triples projected over only
    ``_A`` + ``_B``, and requires exactly ``2`` (``_C`` and all other graphs
    excluded). Probe graphs are dropped on the way out. Any error → ``False``
    (fail closed).
    """
    try:
        for graph in _PROBES:  # start from a known-clean state
            await adapter.update(f"DROP SILENT GRAPH <{graph}>")
        for graph in _PROBES:
            await adapter.update(
                f"INSERT DATA {{ GRAPH <{graph}> {{ <urn:fdp:s> <urn:fdp:p> <{graph}> }} }}"
            )
        body = await adapter.query(_COUNT_QUERY, named_graph_uris=(_A, _B))
        count = _parse_count(body)
        ok = count == 2
        if not ok:
            log.warning(
                "named_graph_isolation_failed",
                expected=2,
                got=count,
                hint="store does not honor repeated named-graph-uri; "
                "use GraphDB/Fuseki for the SPARQL endpoint",
            )
        return ok
    except Exception as err:  # store unreachable / rejected the probe → fail closed
        log.warning("named_graph_isolation_check_errored", error=repr(err))
        return False
    finally:
        for graph in _PROBES:
            try:
                await adapter.update(f"DROP SILENT GRAPH <{graph}>")
            except Exception as drop_err:  # pragma: no cover - best-effort cleanup
                log.warning(
                    "named_graph_isolation_cleanup_failed", graph=graph, error=repr(drop_err)
                )


def _parse_count(body: bytes) -> int:
    payload = json.loads(body)
    bindings = payload.get("results", {}).get("bindings", [])
    if not bindings:
        return -1
    try:
        return int(bindings[0]["c"]["value"])
    except (KeyError, ValueError, TypeError):
        return -1


__all__ = ["verify_named_graph_isolation"]
