"""PDP unit tests with an in-memory fake repository + fake resolver."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from fdpneo_server.policy.cache import ANONYMOUS_SUBJECT_KEY, compute_subject_key
from fdpneo_server.policy.model import (
    Action,
    ConstraintOperator,
    Offer,
    Outcome,
    PartyConstraint,
    Permission,
)
from fdpneo_server.policy.pdp import PDP
from fdpneo_server.shared.context import RequestContext

ALICE = "https://idp.example/realms/fdp#alice"
BOB = "https://idp.example/realms/fdp#bob"
RESOURCE = "https://example.org/records/1"
OFFER_IRI = "https://example.org/offer/1"


def _ctx(
    *,
    subject: str | None = ALICE,
    roles: frozenset[str] = frozenset(),
    groups: frozenset[str] = frozenset(),
) -> RequestContext:
    return RequestContext(
        subject=subject,
        roles=roles,
        groups=groups,
        request_timestamp=datetime(2026, 6, 1, tzinfo=UTC),
        trace_id="t-1",
    )


@dataclass
class _FakeRow:
    subject_key: str
    action: str
    graph_uri: str
    decision: str
    policy_version: str | None


def _empty_rows() -> dict[tuple[str, str, str], _FakeRow]:
    return {}


@dataclass
class FakeCache:
    """In-memory stand-in for :class:`CacheRepository`."""

    rows: dict[tuple[str, str, str], _FakeRow] = field(default_factory=_empty_rows)

    async def upsert(
        self,
        *,
        subject_key: str,
        action: str,
        graph_uri: str,
        decision: str,
        policy_version: str | None,
    ) -> None:
        self.rows[(subject_key, action, graph_uri)] = _FakeRow(
            subject_key=subject_key,
            action=action,
            graph_uri=graph_uri,
            decision=decision,
            policy_version=policy_version,
        )

    async def lookup(
        self,
        *,
        subject_key: str,
        action: str,
        graph_uri: str,
    ) -> _FakeRow | None:
        return self.rows.get((subject_key, action, graph_uri))

    async def authorized_resources(
        self,
        *,
        subject_key: str,
        action: str,
    ) -> set[str]:
        return {
            row.graph_uri
            for row in self.rows.values()
            if row.subject_key == subject_key and row.action == action and row.decision == "permit"
        }

    async def invalidate_by_resource(self, graph_uri: str) -> int:
        keys = [k for k in self.rows if k[2] == graph_uri]
        for k in keys:
            del self.rows[k]
        return len(keys)

    async def invalidate_by_subject(self, subject_key: str) -> int:
        keys = [k for k in self.rows if k[0] == subject_key]
        for k in keys:
            del self.rows[k]
        return len(keys)

    async def invalidate_all(self) -> int:
        dropped = len(self.rows)
        self.rows.clear()
        return dropped


def _empty_offer_map() -> dict[str, Offer | None]:
    return {}


def _empty_call_log() -> list[str]:
    return []


@dataclass
class FakeResolver:
    """Returns a pre-arranged Offer per resource (or ``None``)."""

    by_resource: dict[str, Offer | None] = field(default_factory=_empty_offer_map)
    calls: list[str] = field(default_factory=_empty_call_log)

    async def resolve_offer(self, resource_iri: str) -> Offer | None:
        self.calls.append(resource_iri)
        return self.by_resource.get(resource_iri)


def _permitting_offer() -> Offer:
    return Offer(
        iri=OFFER_IRI,
        permissions=(
            Permission(
                action=Action.READ,
                constraints=(PartyConstraint(operator=ConstraintOperator.EQ, party_uri=ALICE),),
            ),
        ),
    )


def _pdp(*, offers: dict[str, Offer | None] | None = None) -> tuple[PDP, FakeCache, FakeResolver]:
    cache = FakeCache()
    resolver = FakeResolver(by_resource=offers or {})
    pdp = PDP(cache=cache, offer_resolver=resolver)  # type: ignore[arg-type]
    return pdp, cache, resolver


# --- cache miss → resolve → evaluate → store -------------------------------


@pytest.mark.unit
async def test_cache_miss_resolves_evaluates_and_stores() -> None:
    pdp, cache, resolver = _pdp(offers={RESOURCE: _permitting_offer()})
    decision = await pdp.authorize(_ctx(subject=ALICE), Action.READ, RESOURCE)
    assert decision.outcome is Outcome.PERMIT
    assert resolver.calls == [RESOURCE]
    assert len(cache.rows) == 1
    [row] = cache.rows.values()
    assert row.decision == "permit"
    assert row.policy_version == OFFER_IRI


@pytest.mark.unit
async def test_cache_hit_short_circuits_resolver() -> None:
    pdp, _cache, resolver = _pdp(offers={RESOURCE: _permitting_offer()})
    await pdp.authorize(_ctx(subject=ALICE), Action.READ, RESOURCE)
    resolver.calls.clear()
    decision = await pdp.authorize(_ctx(subject=ALICE), Action.READ, RESOURCE)
    assert decision.outcome is Outcome.PERMIT
    assert decision.reason == "cached"
    assert resolver.calls == []


@pytest.mark.unit
async def test_no_offer_resolved_yields_deny() -> None:
    pdp, cache, _ = _pdp()  # resolver returns None for every resource
    decision = await pdp.authorize(_ctx(subject=ALICE), Action.READ, RESOURCE)
    assert decision.outcome is Outcome.DENY
    assert decision.reason == "no policy resolved"
    # The deny IS cached (so subsequent misses don't re-fetch).
    [row] = cache.rows.values()
    assert row.decision == "deny"
    assert row.policy_version is None


# --- subject_key ----------------------------------------------------------


@pytest.mark.unit
def test_subject_key_for_anonymous_is_constant() -> None:
    anon1 = _ctx(subject=None)
    anon2 = _ctx(subject=None, groups=frozenset({"someone"}))
    assert compute_subject_key(anon1) == ANONYMOUS_SUBJECT_KEY
    assert compute_subject_key(anon2) == ANONYMOUS_SUBJECT_KEY


@pytest.mark.unit
def test_subject_key_changes_when_roles_change() -> None:
    a = _ctx(subject=ALICE, roles=frozenset({"steward"}))
    b = _ctx(subject=ALICE, roles=frozenset({"steward", "viewer"}))
    assert compute_subject_key(a) != compute_subject_key(b)


@pytest.mark.unit
def test_subject_key_is_role_order_independent() -> None:
    a = _ctx(subject=ALICE, roles=frozenset({"a", "b", "c"}))
    b = _ctx(subject=ALICE, roles=frozenset({"c", "b", "a"}))
    assert compute_subject_key(a) == compute_subject_key(b)


# --- authorized_graphs (bulk) ---------------------------------------------


@pytest.mark.unit
async def test_authorized_graphs_returns_only_permitted_resources() -> None:
    pdp, _cache, _ = _pdp(
        offers={
            "https://example.org/r1": _permitting_offer(),
            "https://example.org/r2": None,  # → deny
        }
    )
    ctx = _ctx(subject=ALICE)
    await pdp.authorize(ctx, Action.READ, "https://example.org/r1")
    await pdp.authorize(ctx, Action.READ, "https://example.org/r2")

    permitted = await pdp.authorized_graphs(ctx, Action.READ)
    assert permitted == {"https://example.org/r1"}


@pytest.mark.unit
async def test_authorized_graphs_is_scoped_to_subject_and_action() -> None:
    pdp, _cache, _ = _pdp(offers={RESOURCE: _permitting_offer()})
    await pdp.authorize(_ctx(subject=ALICE), Action.READ, RESOURCE)

    bob_permitted = await pdp.authorized_graphs(_ctx(subject=BOB), Action.READ)
    alice_modify = await pdp.authorized_graphs(_ctx(subject=ALICE), Action.MODIFY)
    assert bob_permitted == set()
    assert alice_modify == set()


@pytest.mark.unit
async def test_authorized_graphs_excludes_internal_graphs() -> None:
    # Even when an internal graph (a resource-definition record, ADR-0009)
    # is granted READ — e.g. so the client can fetch the type catalog over
    # REST — it must never enter the SPARQL projection. The exclusion is
    # structural in authorized_graphs, independent of what the cache holds.
    rd_iri = "http://localhost:8000/fdp-api/resource-definitions/catalog"
    meta_iri = RESOURCE + "/meta"
    pdp, _cache, _ = _pdp(
        offers={
            RESOURCE: _permitting_offer(),
            rd_iri: _permitting_offer(),
            meta_iri: _permitting_offer(),
        }
    )
    ctx = _ctx(subject=ALICE)
    await pdp.authorize(ctx, Action.READ, RESOURCE)
    await pdp.authorize(ctx, Action.READ, rd_iri)
    await pdp.authorize(ctx, Action.READ, meta_iri)

    # All three are cached as PERMIT, but only the public record is projected.
    assert await pdp.authorized_graphs(ctx, Action.READ) == {RESOURCE}


# --- invalidation ---------------------------------------------------------


@pytest.mark.unit
async def test_invalidate_all_drops_cached_rows() -> None:
    pdp, cache, _ = _pdp(offers={RESOURCE: _permitting_offer()})
    await pdp.authorize(_ctx(subject=ALICE), Action.READ, RESOURCE)
    assert len(cache.rows) == 1

    dropped = await pdp.invalidate_all()
    assert dropped == 1
    assert cache.rows == {}


@pytest.mark.unit
async def test_cache_miss_after_invalidation_recomputes() -> None:
    pdp, _cache, resolver = _pdp(offers={RESOURCE: _permitting_offer()})
    ctx = _ctx(subject=ALICE)
    await pdp.authorize(ctx, Action.READ, RESOURCE)
    await pdp.invalidate_all()

    resolver.calls.clear()
    decision = await pdp.authorize(ctx, Action.READ, RESOURCE)
    assert decision.outcome is Outcome.PERMIT
    assert resolver.calls == [RESOURCE]  # resolved again
