"""Forward containment-link maintenance (LDP membership, both directions).

A child record carries its own ``dct:isPartOf <parent>`` back-link (written by
the client). The *forward* links — ``parent ldp:contains child`` plus the typed
DCAT relation (e.g. ``repository dcat:catalog catalog``) — belong to the
**parent's** graph (ADR-0007: each record's triples live in its own graph), so
the server is responsible for maintaining them whenever a child is created,
moved, or deleted. Without this the LDP/DCAT graph is one-way and a standards
consumer can't traverse ``repository → dcat:catalog → catalog → …``.

This manager derives the forward links from the child's ``dct:isPartOf`` and the
resource-definition cache (which names the predicate for a given parent→child
type pair) and writes them onto the parent through the metadata repository — so
the parent's ``dct:modified`` / ETag refresh and a ``RecordModified`` event
(reindex) come for free. It returns the parent events rather than publishing
them, so the LDP router can order them after the child's own event and roll the
whole thing back on failure (the SPARQL 1.1 Protocol gives no cross-graph
transaction — ADR-0005 — so the router compensates, as the applier does).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

import structlog
from rdflib import URIRef

from fdp.metadata.events import RecordModified
from fdp.metadata.graphs import record_graph_uri
from fdp.shared.namespaces import DCT, LDP

if TYPE_CHECKING:
    from datetime import datetime

    from rdflib import Graph

    from fdp.metadata.repository import MetadataRepository

log = structlog.get_logger(__name__)


class RelationResolver(Protocol):
    """Resolves the typed forward predicate from a parent to a child IRI."""

    def containment_relation(self, parent_iri: str, child_iri: str) -> str | None: ...


class ContainmentManager:
    """Keeps a parent container's forward membership links in sync with its children.

    Stateless aside from its collaborators; safe to share across requests. Each
    ``reconcile_*`` returns the ``RecordModified`` events for the parents it
    touched (empty when the child declares no known parent), leaving publication
    and failure-compensation to the caller.
    """

    def __init__(self, *, repo: MetadataRepository, resolver: RelationResolver) -> None:
        self._repo = repo
        self._resolver = resolver

    async def reconcile_create(
        self, child_iri: str, child_graph: Graph, *, subject: str | None, timestamp: datetime
    ) -> list[RecordModified]:
        """Add forward links to the child's parent (if it declares one)."""
        parent = _parent_of(child_graph, child_iri)
        if parent is None:
            return []
        event = await self._apply(parent, child_iri, add=True, subject=subject, timestamp=timestamp)
        return [event] if event is not None else []

    async def reconcile_delete(
        self, child_iri: str, child_graph: Graph, *, subject: str | None, timestamp: datetime
    ) -> list[RecordModified]:
        """Remove forward links from the (pre-delete) child's parent."""
        parent = _parent_of(child_graph, child_iri)
        if parent is None:
            return []
        event = await self._apply(
            parent, child_iri, add=False, subject=subject, timestamp=timestamp
        )
        return [event] if event is not None else []

    async def reconcile_update(
        self,
        child_iri: str,
        old_graph: Graph,
        new_graph: Graph,
        *,
        subject: str | None,
        timestamp: datetime,
    ) -> list[RecordModified]:
        """Repoint forward links when a child's ``dct:isPartOf`` changes.

        On a plain content edit (same parent) this re-asserts the links
        idempotently — a cheap self-heal for records created before forward
        links were maintained, with no write when they are already present.
        """
        old_parent = _parent_of(old_graph, child_iri)
        new_parent = _parent_of(new_graph, child_iri)
        events: list[RecordModified | None] = []
        if old_parent == new_parent:
            if new_parent is not None:
                events.append(
                    await self._apply(
                        new_parent, child_iri, add=True, subject=subject, timestamp=timestamp
                    )
                )
        else:
            if old_parent is not None:
                events.append(
                    await self._apply(
                        old_parent, child_iri, add=False, subject=subject, timestamp=timestamp
                    )
                )
            if new_parent is not None:
                events.append(
                    await self._apply(
                        new_parent, child_iri, add=True, subject=subject, timestamp=timestamp
                    )
                )
        return [e for e in events if e is not None]

    async def _apply(
        self,
        parent_iri: str,
        child_iri: str,
        *,
        add: bool,
        subject: str | None,
        timestamp: datetime,
    ) -> RecordModified | None:
        """Add or remove the forward links on ``parent_iri``; return its event if changed."""
        parent_graph = await self._repo.get_graph(parent_iri)
        if len(parent_graph) == 0:
            # The declared parent isn't a stored record — don't fabricate a
            # container. The child keeps its dct:isPartOf back-link regardless.
            log.warning("containment_parent_missing", parent=parent_iri, child=child_iri)
            return None

        ps, cs = URIRef(parent_iri), URIRef(child_iri)
        predicates = [LDP.contains]
        relation = self._resolver.containment_relation(parent_iri, child_iri)
        if relation is not None:
            predicates.append(URIRef(relation))

        changed = False
        for pred in predicates:
            present = (ps, pred, cs) in parent_graph
            if add and not present:
                parent_graph.add((ps, pred, cs))
                changed = True
            elif not add and present:
                parent_graph.remove((ps, pred, cs))
                changed = True
        if not changed:
            return None

        etag = await self._repo.put_graph(parent_iri, parent_graph, subject=subject)
        log.info(
            "containment_links_updated",
            parent=parent_iri,
            child=child_iri,
            action="add" if add else "remove",
            relation=relation,
        )
        return RecordModified(
            record_iri=parent_iri, subject=subject, etag=etag, timestamp=timestamp
        )


def _parent_of(graph: Graph, child_iri: str) -> str | None:
    """The normalized ``dct:isPartOf`` parent IRI declared in ``graph``, if any."""
    value = graph.value(URIRef(child_iri), DCT.isPartOf)
    if not isinstance(value, URIRef):
        return None
    return str(record_graph_uri(value))


__all__ = ["ContainmentManager", "RelationResolver"]
