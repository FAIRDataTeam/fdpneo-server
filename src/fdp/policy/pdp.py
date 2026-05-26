"""Policy decision point — caches lazily, evaluates on miss.

The PDP is the single entry point every PEP calls. It owns no policy
logic of its own — :mod:`fdp.policy.evaluator` is the pure function that
turns an :class:`Offer` plus a :class:`RequestContext` into a
:class:`Decision`. This module wires that function together with the
materialized authorization index (architecture §9.4).

**Resolving the effective Offer**

Looking up "which Offer is in force for this resource" is the metadata
module's responsibility — the PDP receives an :class:`OfferResolver`
constructor argument that returns an Offer (or ``None`` when no policy
applies). Ticket 1.3 deliberately does *not* implement inheritance — a
resource with no resolved Offer evaluates to DENY by default; the
inheritance walk lives in a later ticket (architecture §8.3).

**Invalidation**

* :meth:`PDP.invalidate_resource` — synchronous, called by the metadata
  module on policy / ``dct:rights`` writes (architecture §9.4 calls this
  the tightening path).
* :meth:`PDP.invalidate_subject` — fired asynchronously when a user's
  role set changes. Subjects whose role hash already changes (and whose
  new requests therefore miss the cache) get the same effect for free;
  the explicit hook also reclaims rows for the previous hash.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from fdp.policy.cache import CacheRepository, compute_subject_key
from fdp.policy.evaluator import evaluate
from fdp.policy.model import Action, Decision, Outcome

if TYPE_CHECKING:
    from fdp.policy.model import Offer
    from fdp.shared.context import RequestContext


class OfferResolver(Protocol):
    """Maps a resource IRI to the Offer in force, or ``None``."""

    async def resolve_offer(self, resource_iri: str) -> Offer | None: ...


class PDP:
    """Cached policy decision point."""

    def __init__(self, *, cache: CacheRepository, offer_resolver: OfferResolver) -> None:
        self._cache = cache
        self._resolver = offer_resolver

    async def authorize(
        self,
        ctx: RequestContext,
        action: Action,
        resource_iri: str,
    ) -> Decision:
        """Decide whether ``ctx`` may perform ``action`` on ``resource_iri``."""
        subject_key = compute_subject_key(ctx)
        cached = await self._cache.lookup(
            subject_key=subject_key, action=action.value, graph_uri=resource_iri
        )
        if cached is not None:
            outcome = Outcome(cached.decision)
            return Decision(outcome=outcome, rule=None, reason="cached")

        offer = await self._resolver.resolve_offer(resource_iri)
        if offer is None:
            decision = Decision(
                outcome=Outcome.DENY,
                rule=None,
                reason="no policy resolved",
            )
            policy_version: str | None = None
        else:
            decision = evaluate(offer, ctx, action)
            policy_version = offer.iri

        await self._cache.upsert(
            subject_key=subject_key,
            action=action.value,
            graph_uri=resource_iri,
            decision=decision.outcome.value,
            policy_version=policy_version,
        )
        return decision

    async def authorized_graphs(
        self,
        ctx: RequestContext,
        action: Action,
    ) -> set[str]:
        """Return every resource IRI cached as ``PERMIT`` for ``ctx`` + ``action``.

        This is the bulk lookup the SPARQL endpoint uses to inject
        ``FROM NAMED`` clauses. The cache must be warmed for the subject
        first — entries the PDP has never evaluated do not appear here.
        """
        subject_key = compute_subject_key(ctx)
        return await self._cache.authorized_resources(subject_key=subject_key, action=action.value)

    async def invalidate_resource(self, resource_iri: str) -> int:
        """Drop every cached decision against ``resource_iri``."""
        return await self._cache.invalidate_by_resource(resource_iri)

    async def invalidate_subject(self, ctx: RequestContext) -> int:
        """Drop every cached decision against the subject in ``ctx``.

        Computes the subject key from the *current* role set; callers
        whose role change has already produced a new key should pass the
        old context instead (or call :meth:`invalidate_subject_key`).
        """
        return await self._cache.invalidate_by_subject(compute_subject_key(ctx))

    async def invalidate_subject_key(self, subject_key: str) -> int:
        """Lower-level variant of :meth:`invalidate_subject`."""
        return await self._cache.invalidate_by_subject(subject_key)


__all__ = ["PDP", "OfferResolver"]
