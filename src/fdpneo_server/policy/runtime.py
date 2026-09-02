"""Session-aware composition glue around :class:`PDP`.

:class:`PDP` holds a single :class:`CacheRepository`, which itself
holds a SQLAlchemy session — fine for a one-shot call but not for
sharing across concurrent requests. :class:`RequestScopedPDP` wraps
that with a per-call session so a single instance is safe to bind
once at app-startup time and reuse from every request handler.

The wrapper implements the same two methods the PEPs use
(:meth:`authorize`, :meth:`authorized_graphs`) so handlers can take it
where the type hint says :class:`PDP` and stay duck-compatible.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fdpneo_server.policy.cache import CacheRepository
from fdpneo_server.policy.pdp import PDP

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from fdpneo_server.policy.model import Action, Decision
    from fdpneo_server.policy.pdp import OfferResolver
    from fdpneo_server.shared.context import RequestContext


class RequestScopedPDP:
    """A PDP-compatible facade that opens a fresh session per call."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        offer_resolver: OfferResolver,
    ) -> None:
        self._session_factory = session_factory
        self._offer_resolver = offer_resolver

    async def authorize(
        self,
        ctx: RequestContext,
        action: Action,
        resource_iri: str,
    ) -> Decision:
        async with self._session_factory() as session:
            pdp = PDP(
                cache=CacheRepository(session),
                offer_resolver=self._offer_resolver,
            )
            decision = await pdp.authorize(ctx, action, resource_iri)
            await session.commit()
            return decision

    async def authorized_graphs(
        self,
        ctx: RequestContext,
        action: Action,
    ) -> set[str]:
        async with self._session_factory() as session:
            pdp = PDP(
                cache=CacheRepository(session),
                offer_resolver=self._offer_resolver,
            )
            return await pdp.authorized_graphs(ctx, action)

    async def authorize_many(
        self,
        ctx: RequestContext,
        action: Action,
        resource_iris: Iterable[str],
    ) -> set[str]:
        """Complete permit subset over ``resource_iris``; one session for the batch."""
        async with self._session_factory() as session:
            pdp = PDP(
                cache=CacheRepository(session),
                offer_resolver=self._offer_resolver,
            )
            permitted = await pdp.authorize_many(ctx, action, resource_iris)
            await session.commit()
            return permitted

    async def invalidate_all(self) -> int:
        """Clear the whole authz index — used when a managed policy changes (ADR-0012)."""
        async with self._session_factory() as session:
            pdp = PDP(
                cache=CacheRepository(session),
                offer_resolver=self._offer_resolver,
            )
            cleared = await pdp.invalidate_all()
            await session.commit()
            return cleared


__all__ = ["RequestScopedPDP"]
