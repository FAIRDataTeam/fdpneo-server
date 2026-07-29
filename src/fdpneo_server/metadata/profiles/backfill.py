"""One-shot reconciliation: stamp Direct Container membership on old containers.

Task 15.1 made every FDP container a genuine LDP **Direct Container**: its graph
carries ``ldp:DirectContainer`` + the membership triad (``ldp:membershipResource``
= itself, one ``ldp:hasMemberRelation`` per RD child relation,
``ldp:insertedContentRelation ldp:MemberSubject``). The root seed and the runtime
LDP router (``_stamp_container_config``) now emit it. Deployments bootstrapped
*before* 15.1 still hold their containers as ``ldp:BasicContainer`` with no
membership configuration, and any container records authored before the upgrade
are likewise stale.

A ``force-apply`` would fix the seed but wipes the store (and authored records),
so this module provides a **non-destructive** backfill instead, driven by
``fdp ldp backfill-membership``:

1. Walk every non-internal record graph. Resolve each to its resource definition
   (by URL); a record whose RD declares child links is a container, and those
   links' ``relationUri`` values are its membership relations.
2. For each container missing (or mis-typed for) the Direct Container config, add
   the membership triad and strip any stale ``ldp:BasicContainer`` type, then
   re-store the record graph.

The rewrite goes through the adapter's ``replace_graph`` rather than
``repository.put_graph`` on purpose: stamping LDP affordance triples is a
structural reconciliation, not a content edit, so it must **not** bump the
record's ``owl:versionInfo`` / ``dct:modified`` in the meta graph.

Idempotent: a container already carrying the full Direct Container config (and no
``ldp:BasicContainer``) is skipped, so a second run is a no-op. Leaf records
(Distribution) and graphs that don't resolve to a known type are untouched.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog
from rdflib import URIRef
from rdflib.namespace import RDF

from fdpneo_server.metadata.profiles.applier import direct_container_config
from fdpneo_server.shared.graphs import is_internal_graph_uri
from fdpneo_server.shared.namespaces import LDP

if TYPE_CHECKING:
    from fdpneo_server.metadata.profiles.registry import ResourceDefinitionCache
    from fdpneo_server.metadata.repository import MetadataRepository
    from fdpneo_server.storage.triplestore import TripleStoreAdapter

log = structlog.get_logger(__name__)

LDP_BASIC_CONTAINER = LDP.BasicContainer


@dataclass
class MembershipBackfillReport:
    """What the backfill changed (or would have)."""

    stamped: list[str] = field(default_factory=list)
    """IRIs of container records that gained / corrected the membership config."""

    already_conformant: list[str] = field(default_factory=list)
    """Container IRIs that already carried the full Direct Container config."""

    @property
    def changed(self) -> bool:
        return bool(self.stamped)


async def backfill_direct_container_membership(
    *,
    repository: MetadataRepository,
    adapter: TripleStoreAdapter,
    cache: ResourceDefinitionCache,
) -> MembershipBackfillReport:
    """Stamp Direct Container membership on every stale container in the store."""
    report = MembershipBackfillReport()
    for graph_iri in await _record_graphs(adapter):
        relations = cache.member_relations(graph_iri)
        if not relations:
            # A leaf type, or a graph that doesn't resolve to a known RD.
            continue
        graph = await repository.get_graph(graph_iri)
        if len(graph) == 0:
            continue
        subject = URIRef(graph_iri)
        desired = direct_container_config(subject, relations)
        has_stale_basic = (subject, RDF.type, LDP_BASIC_CONTAINER) in graph
        if all(triple in graph for triple in desired) and not has_stale_basic:
            report.already_conformant.append(graph_iri)
            continue
        graph.remove((subject, RDF.type, LDP_BASIC_CONTAINER))
        for triple in desired:
            graph.add(triple)
        await adapter.replace_graph(
            graph_iri, graph.serialize(format="nt"), mime="application/n-triples"
        )
        report.stamped.append(graph_iri)
        log.info("direct_container_membership_stamped", record=graph_iri, relations=relations)
    return report


async def _record_graphs(adapter: TripleStoreAdapter) -> list[str]:
    """Enumerate every non-internal named graph (record graphs only)."""
    import json

    body = await adapter.query(
        "SELECT DISTINCT ?g WHERE { GRAPH ?g { ?s ?p ?o } }",
        accept="application/sparql-results+json",
    )
    bindings = json.loads(body).get("results", {}).get("bindings", [])
    return [
        b["g"]["value"] for b in bindings if "g" in b and not is_internal_graph_uri(b["g"]["value"])
    ]


__all__ = ["MembershipBackfillReport", "backfill_direct_container_membership"]
