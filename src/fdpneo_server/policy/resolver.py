"""Concrete :class:`OfferResolver` backed by RDF graph fetches.

The metadata module knows how to look up triples for a resource (its
:class:`MetadataRepository`); the policy module knows how to parse an
ODRL graph into an :class:`Offer`. This resolver bridges the two
without either module importing the other — the caller passes any
object that satisfies the :class:`GraphFetcher` protocol.

**Inheritance walk (architecture §8.3)**

The effective Offer for a resource is resolved in this order:

1. The resource's own ``dct:rights`` triple.
2. Walking up the ``dct:isPartOf`` chain (dataset → catalog →
   repository) and using the first ``dct:rights`` encountered.
3. A deployment-level system-default Offer IRI provided through
   ``system_default_provider`` — typically the offer flagged
   ``isSystemDefault: true`` in the applied profile and seeded onto
   the Repository's ``dct:rights`` (so the walk above also lands
   there). The explicit provider catches records that don't link
   back to the Repository via ``dct:isPartOf``.

When all three sources fail, the resolver returns ``None`` and the
PDP applies its default-deny rule. Bounded walk + visited-set guard
prevent pathological cycles.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

import structlog
from rdflib import URIRef

from fdpneo_server.policy.parser import parse_offer
from fdpneo_server.shared.errors import SchemaViolation
from fdpneo_server.shared.namespaces import DCT

if TYPE_CHECKING:
    from rdflib import Graph

    from fdpneo_server.policy.model import Offer

log = structlog.get_logger(__name__)


_DEFAULT_MAX_WALK_DEPTH = 10


class GraphFetcher(Protocol):
    """Anything that can return a record graph for a given IRI.

    :class:`fdpneo_server.metadata.repository.MetadataRepository.get_graph`
    satisfies this without explicit declaration.
    """

    async def get_graph(self, record_uri: str) -> Graph: ...


class GraphBackedOfferResolver:
    """Resolves the in-force Offer with a ``dct:isPartOf`` inheritance walk.

    Parse failures and empty Offer graphs return ``None`` so the PDP
    applies its default-deny rule. Failures are logged so operators
    can see policies that don't conform to the FDP profile.

    ``system_default_provider`` is read on every call so the deployment's
    auto-bootstrap can publish the system-default Offer IRI after the
    resolver is constructed without rebuilding it.
    """

    def __init__(
        self,
        fetcher: GraphFetcher,
        *,
        system_default_provider: Callable[[], str | None] = lambda: None,
        max_walk_depth: int = _DEFAULT_MAX_WALK_DEPTH,
    ) -> None:
        self._fetcher = fetcher
        self._system_default_provider = system_default_provider
        self._max_depth = max_walk_depth

    async def resolve_offer(self, resource_iri: str) -> Offer | None:
        rights_iri = await self._resolve_rights_iri(resource_iri)
        if rights_iri is None:
            return None
        offer = await self._fetch_and_parse(rights_iri)
        if offer is None:
            # The resource named an Offer (its own dct:rights, an ancestor's, or
            # the deployment system-default) but no parseable Offer graph exists
            # there. The PDP will now default-deny every non-anonymous action on
            # this resource — surfacing as "policy denies modify on …". Logged
            # loudly because the usual cause is operational: the offer was never
            # seeded at this IRI, e.g. the deployment was upgraded across the
            # ADR-0012 offer→managed-policy IRI change without re-applying the
            # profile. Re-apply (or factory-reset) to seed it.
            log.warning(
                "offer_unresolved_default_deny",
                resource_iri=resource_iri,
                rights_iri=rights_iri,
                system_default=self._system_default_provider(),
            )
        return offer

    # --- internals --------------------------------------------------------

    async def _resolve_rights_iri(self, resource_iri: str) -> str | None:
        """Walk up ``dct:isPartOf`` looking for the first ``dct:rights``.

        Falls back to ``system_default_provider()`` only when the walk
        completes without finding any ``dct:rights`` — the chain is
        consulted first so a record's own (or an ancestor's) explicit
        Offer wins over the deployment default.
        """
        visited: set[str] = set()
        current = resource_iri
        for depth in range(self._max_depth):
            if current in visited:
                log.warning("offer_inheritance_cycle", at=current, depth=depth)
                break
            visited.add(current)

            graph = await self._fetcher.get_graph(current)

            for obj in graph.objects(URIRef(current), DCT.rights):
                return str(obj)

            parent: str | None = None
            for obj in graph.objects(URIRef(current), DCT.isPartOf):
                parent = str(obj)
                break
            if parent is None:
                break
            current = parent
        else:
            log.warning(
                "offer_inheritance_depth_exceeded",
                resource_iri=resource_iri,
                max_depth=self._max_depth,
            )

        return self._system_default_provider()

    async def _fetch_and_parse(self, rights_iri: str) -> Offer | None:
        offer_graph = await self._fetcher.get_graph(rights_iri)
        if len(offer_graph) == 0:
            return None
        try:
            return parse_offer(offer_graph, URIRef(rights_iri))
        except SchemaViolation as err:
            log.warning(
                "offer_parse_failed",
                rights_iri=rights_iri,
                error=err.message,
            )
            return None


__all__ = ["GraphBackedOfferResolver", "GraphFetcher"]
