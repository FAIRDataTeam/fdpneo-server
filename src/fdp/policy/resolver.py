"""Concrete :class:`OfferResolver` backed by RDF graph fetches.

The metadata module knows how to look up triples for a resource (its
:class:`MetadataRepository`); the policy module knows how to parse an
ODRL graph into an :class:`Offer`. This resolver bridges the two
without either module importing the other — the caller passes any
object that satisfies the :class:`GraphFetcher` protocol.

**v1 scope**

Resolves only the ``dct:rights`` triple on the resource graph itself.
No inheritance walk (record → container → repository → system default)
— that lands later under architecture §8.3. A resource without an
explicit ``dct:rights`` produces ``None``; the PDP turns that into a
default DENY.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

import structlog
from rdflib import URIRef

from fdp.policy.parser import parse_offer
from fdp.shared.errors import SchemaViolation
from fdp.shared.namespaces import DCT

if TYPE_CHECKING:
    from rdflib import Graph

    from fdp.policy.model import Offer

log = structlog.get_logger(__name__)


class GraphFetcher(Protocol):
    """Anything that can return a record graph for a given IRI.

    :class:`fdp.metadata.repository.MetadataRepository.get_graph`
    satisfies this without explicit declaration.
    """

    async def get_graph(self, record_uri: str) -> Graph: ...


class GraphBackedOfferResolver:
    """Resolves the in-force Offer by reading ``dct:rights`` from the resource.

    A missing ``dct:rights``, an empty Offer graph, or a parse failure
    all produce ``None`` so the PDP can apply its default-deny rule.
    Parse failures are logged so operators can see policies that don't
    conform to the FDP profile (per architecture §8.2 such Offers
    should never have been written in the first place; this is the
    runtime backstop).
    """

    def __init__(self, fetcher: GraphFetcher) -> None:
        self._fetcher = fetcher

    async def resolve_offer(self, resource_iri: str) -> Offer | None:
        resource_graph = await self._fetcher.get_graph(resource_iri)
        rights_iri: str | None = None
        for obj in resource_graph.objects(URIRef(resource_iri), DCT.rights):
            rights_iri = str(obj)
            break
        if rights_iri is None:
            return None

        offer_graph = await self._fetcher.get_graph(rights_iri)
        if len(offer_graph) == 0:
            return None

        try:
            return parse_offer(offer_graph, URIRef(rights_iri))
        except SchemaViolation as err:
            log.warning(
                "offer_parse_failed",
                resource_iri=resource_iri,
                rights_iri=rights_iri,
                error=err.message,
            )
            return None


__all__ = ["GraphBackedOfferResolver", "GraphFetcher"]
