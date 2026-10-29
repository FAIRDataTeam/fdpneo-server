"""``RequestScopedPDP`` resolves the access-control mode per call (ADR-0027)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest

from fdpneo_server.policy import runtime as runtime_module
from fdpneo_server.policy.mode import ODRL_ACCESS, OPEN_ACCESS, AccessControlConfig, AccessMode
from fdpneo_server.policy.model import Action, Offer, Outcome
from fdpneo_server.policy.runtime import RequestScopedPDP
from fdpneo_server.shared.context import RequestContext
from tests.unit.policy.test_pdp import FakeCache

RESOURCE = "https://example.org/records/1"


class _Session:
    async def commit(self) -> None:
        return None


@pytest.fixture
def session_factory(monkeypatch: pytest.MonkeyPatch) -> Any:
    """A no-op session plus an in-memory cache standing in for ``CacheRepository``.

    The real authz_index table is Postgres-only (identity column); the wrapper's
    contract under test is session-per-call + mode resolution, not SQL.
    """
    cache = FakeCache()
    monkeypatch.setattr(runtime_module, "CacheRepository", lambda _session: cache)  # pyright: ignore[reportUnknownLambdaType]

    @asynccontextmanager
    async def factory():
        yield _Session()

    return factory


class _DenyingResolver:
    """No Offer anywhere — ODRL mode default-denies; records whether it was asked."""

    def __init__(self) -> None:
        self.calls = 0

    async def resolve_offer(self, resource_iri: str) -> Offer | None:
        del resource_iri
        self.calls += 1
        return None


def _anon() -> RequestContext:
    return RequestContext.anonymous(trace_id="t-1")


def _steward() -> RequestContext:
    return RequestContext(
        subject="https://idp/alice",
        roles=frozenset({"steward"}),
        trace_id="t-1",
        request_timestamp=datetime(2026, 10, 29, tzinfo=UTC),
    )


@pytest.mark.unit
async def test_without_provider_runs_odrl(session_factory: Any) -> None:
    resolver = _DenyingResolver()
    pdp = RequestScopedPDP(session_factory=session_factory, offer_resolver=resolver)
    assert (await pdp.access_control()) == ODRL_ACCESS
    decision = await pdp.authorize(_anon(), Action.READ, RESOURCE)
    assert decision.outcome is Outcome.DENY
    assert resolver.calls == 1


@pytest.mark.unit
async def test_provider_is_consulted_on_every_call(session_factory: Any) -> None:
    """Flipping the provider's answer flips decisions with no rebuild."""
    resolver = _DenyingResolver()
    current = {"config": OPEN_ACCESS}

    async def provider() -> AccessControlConfig:
        return current["config"]

    pdp = RequestScopedPDP(
        session_factory=session_factory,
        offer_resolver=resolver,
        access_control_provider=provider,
    )
    assert (await pdp.authorize(_anon(), Action.READ, RESOURCE)).outcome is Outcome.PERMIT
    assert (await pdp.authorize(_steward(), Action.MODIFY, RESOURCE)).outcome is Outcome.PERMIT
    assert await pdp.authorize_many(_anon(), Action.READ, [RESOURCE]) == {RESOURCE}
    assert await pdp.authorized_graphs(_anon(), Action.READ) == set()
    assert resolver.calls == 0

    current["config"] = AccessControlConfig(mode=AccessMode.ODRL)
    assert (await pdp.authorize(_anon(), Action.READ, RESOURCE)).outcome is Outcome.DENY
    assert resolver.calls == 1
