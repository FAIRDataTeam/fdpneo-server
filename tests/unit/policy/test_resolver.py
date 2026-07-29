"""Unit tests for :class:`GraphBackedOfferResolver` (architecture §8.3)."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from rdflib import Graph

from fdpneo_server.policy.model import Action
from fdpneo_server.policy.resolver import GraphBackedOfferResolver

OFFER_TTL = """\
@prefix odrl: <http://www.w3.org/ns/odrl/2/> .
<{iri}>
    a odrl:Offer ;
    odrl:permission [ a odrl:Permission ; odrl:action odrl:{action} ] .
"""


def _offer_ttl(iri: str, action: str = "read") -> str:
    return OFFER_TTL.format(iri=iri, action=action)


@dataclass
class _FakeFetcher:
    """In-memory graph fetcher keyed by IRI → Turtle source."""

    graphs: dict[str, str] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    async def get_graph(self, record_uri: str) -> Graph:
        self.calls.append(record_uri)
        g = Graph()
        if ttl := self.graphs.get(record_uri):
            g.parse(data=ttl, format="turtle")
        return g


# --- direct rights -------------------------------------------------------


@pytest.mark.unit
async def test_resolves_direct_rights_on_the_resource() -> None:
    rec = "https://fdp.example/dataset/d-1"
    offer = "https://fdp.example/offers/public"
    fetcher = _FakeFetcher(
        graphs={
            rec: f"""\
@prefix dct: <http://purl.org/dc/terms/> .
<{rec}> dct:rights <{offer}> .
""",
            offer: _offer_ttl(offer),
        }
    )
    resolver = GraphBackedOfferResolver(fetcher)  # type: ignore[arg-type]
    result = await resolver.resolve_offer(rec)
    assert result is not None
    assert result.iri == offer
    assert any(p.action is Action.READ for p in result.permissions)


@pytest.mark.unit
async def test_archived_policy_still_enforces_for_existing_reference() -> None:
    # ADR-0012 §4: archiving a managed policy must not break records that
    # already reference it. Offer resolution fetches the policy graph by its
    # IRI and never consults the policy's publication state (the StateGate
    # gates record *read* visibility, separately) — so an archived policy keeps
    # enforcing for its existing dct:rights dependents.
    rec = "https://fdp.example/dataset/d-1"
    policy = "https://fdp.example/policies/embargo"  # this policy is ARCHIVED
    fetcher = _FakeFetcher(
        graphs={
            rec: f"""\
@prefix dct: <http://purl.org/dc/terms/> .
<{rec}> dct:rights <{policy}> .
""",
            policy: _offer_ttl(policy, "read"),
        }
    )
    resolver = GraphBackedOfferResolver(fetcher)  # type: ignore[arg-type]

    result = await resolver.resolve_offer(rec)

    assert result is not None
    assert result.iri == policy
    assert any(p.action is Action.READ for p in result.permissions)
    # Concretely: resolution never reached for the policy's /meta state graph.
    assert f"{policy}/meta" not in fetcher.calls


# --- inheritance walk -----------------------------------------------------


@pytest.mark.unit
async def test_inherits_rights_from_parent_via_is_part_of() -> None:
    dataset = "https://fdp.example/dataset/d-1"
    catalog = "https://fdp.example/catalog/c-1"
    offer = "https://fdp.example/offers/public"
    fetcher = _FakeFetcher(
        graphs={
            dataset: f"""\
@prefix dct: <http://purl.org/dc/terms/> .
<{dataset}> dct:isPartOf <{catalog}> .
""",
            catalog: f"""\
@prefix dct: <http://purl.org/dc/terms/> .
<{catalog}> dct:rights <{offer}> .
""",
            offer: _offer_ttl(offer),
        }
    )
    resolver = GraphBackedOfferResolver(fetcher)  # type: ignore[arg-type]
    result = await resolver.resolve_offer(dataset)
    assert result is not None
    assert result.iri == offer
    # The walk visited dataset, then catalog, then the offer itself.
    assert fetcher.calls == [dataset, catalog, offer]


@pytest.mark.unit
async def test_inherits_rights_two_levels_up_to_repository() -> None:
    distribution = "https://fdp.example/distribution/d-1"
    dataset = "https://fdp.example/dataset/ds-1"
    repository = "https://fdp.example"
    offer = "https://fdp.example/offers/public"
    fetcher = _FakeFetcher(
        graphs={
            distribution: f"""\
@prefix dct: <http://purl.org/dc/terms/> .
<{distribution}> dct:isPartOf <{dataset}> .
""",
            dataset: f"""\
@prefix dct: <http://purl.org/dc/terms/> .
<{dataset}> dct:isPartOf <{repository}> .
""",
            repository: f"""\
@prefix dct: <http://purl.org/dc/terms/> .
<{repository}> dct:rights <{offer}> .
""",
            offer: _offer_ttl(offer),
        }
    )
    resolver = GraphBackedOfferResolver(fetcher)  # type: ignore[arg-type]
    result = await resolver.resolve_offer(distribution)
    assert result is not None
    assert result.iri == offer


# --- system-default fallback ---------------------------------------------


@pytest.mark.unit
async def test_falls_back_to_system_default_when_walk_finds_no_rights() -> None:
    rec = "https://fdp.example/orphan/x"
    sys_default = "https://w3id.org/fdp/profiles/default/offers/public"
    fetcher = _FakeFetcher(
        graphs={
            rec: "",  # empty graph: neither rights nor isPartOf
            sys_default: _offer_ttl(sys_default),
        }
    )
    resolver = GraphBackedOfferResolver(
        fetcher,  # type: ignore[arg-type]
        system_default_provider=lambda: sys_default,
    )
    result = await resolver.resolve_offer(rec)
    assert result is not None
    assert result.iri == sys_default


@pytest.mark.unit
async def test_no_walk_and_no_system_default_returns_none() -> None:
    fetcher = _FakeFetcher()  # empty
    resolver = GraphBackedOfferResolver(fetcher)  # type: ignore[arg-type]
    assert await resolver.resolve_offer("https://fdp.example/ghost") is None


@pytest.mark.unit
async def test_system_default_pointing_at_missing_graph_returns_none() -> None:
    # Operational failure mode (client report): the system-default IRI is set but
    # no Offer graph exists there (e.g. upgraded across the ADR-0012 offer→managed
    # -policy IRI change without re-applying). resolve_offer returns None so the
    # PDP default-denies — the resolver logs `offer_unresolved_default_deny`.
    rec = "https://fdp.example/catalog/c-1"
    missing_default = "https://fdp.example/policies/system-default"
    fetcher = _FakeFetcher(graphs={rec: ""})  # record empty; default graph absent
    resolver = GraphBackedOfferResolver(
        fetcher,  # type: ignore[arg-type]
        system_default_provider=lambda: missing_default,
    )
    assert await resolver.resolve_offer(rec) is None


@pytest.mark.unit
async def test_resource_rights_win_over_system_default() -> None:
    """A record's own ``dct:rights`` wins over the deployment default."""
    rec = "https://fdp.example/dataset/d-1"
    explicit = "https://fdp.example/offers/explicit"
    sys_default = "https://fdp.example/offers/default"
    fetcher = _FakeFetcher(
        graphs={
            rec: f"""\
@prefix dct: <http://purl.org/dc/terms/> .
<{rec}> dct:rights <{explicit}> .
""",
            explicit: _offer_ttl(explicit),
            sys_default: _offer_ttl(sys_default),
        }
    )
    resolver = GraphBackedOfferResolver(
        fetcher,  # type: ignore[arg-type]
        system_default_provider=lambda: sys_default,
    )
    result = await resolver.resolve_offer(rec)
    assert result is not None
    assert result.iri == explicit


# --- safety nets ---------------------------------------------------------


@pytest.mark.unit
async def test_cycle_is_broken_by_visited_set() -> None:
    """A → B → A loop must not spin forever."""
    a = "https://fdp.example/a"
    b = "https://fdp.example/b"
    fetcher = _FakeFetcher(
        graphs={
            a: f"""\
@prefix dct: <http://purl.org/dc/terms/> .
<{a}> dct:isPartOf <{b}> .
""",
            b: f"""\
@prefix dct: <http://purl.org/dc/terms/> .
<{b}> dct:isPartOf <{a}> .
""",
        }
    )
    resolver = GraphBackedOfferResolver(fetcher)  # type: ignore[arg-type]
    assert await resolver.resolve_offer(a) is None
    # Each node fetched once; the second visit hits the visited guard.
    assert fetcher.calls == [a, b]


@pytest.mark.unit
async def test_max_walk_depth_caps_the_chain() -> None:
    """A 100-level isPartOf chain is bounded by the configured max depth."""
    graphs: dict[str, str] = {}
    for i in range(100):
        cur = f"https://fdp.example/level/{i}"
        nxt = f"https://fdp.example/level/{i + 1}"
        graphs[cur] = (
            f"@prefix dct: <http://purl.org/dc/terms/> .\n<{cur}> dct:isPartOf <{nxt}> .\n"
        )
    fetcher = _FakeFetcher(graphs=graphs)
    resolver = GraphBackedOfferResolver(
        fetcher,  # type: ignore[arg-type]
        max_walk_depth=5,
    )
    assert await resolver.resolve_offer("https://fdp.example/level/0") is None
    # 5 fetches max — the bound holds.
    assert len(fetcher.calls) == 5


@pytest.mark.unit
async def test_malformed_offer_yields_none_so_pdp_defaults_to_deny() -> None:
    """An Offer that fails to parse logs a warning and yields None."""
    rec = "https://fdp.example/r"
    offer = "https://fdp.example/offers/broken"
    fetcher = _FakeFetcher(
        graphs={
            rec: f"""\
@prefix dct: <http://purl.org/dc/terms/> .
<{rec}> dct:rights <{offer}> .
""",
            # Use an unsupported odrl:action so the parser raises.
            offer: f"""\
@prefix odrl: <http://www.w3.org/ns/odrl/2/> .
<{offer}>
    a odrl:Offer ;
    odrl:permission [ a odrl:Permission ; odrl:action <http://example.org/unknown> ] .
""",
        }
    )
    resolver = GraphBackedOfferResolver(fetcher)  # type: ignore[arg-type]
    assert await resolver.resolve_offer(rec) is None


# --- lazy provider read --------------------------------------------------


@pytest.mark.unit
async def test_system_default_provider_is_read_per_call() -> None:
    """Late-published defaults take effect without rebuilding the resolver."""
    rec = "https://fdp.example/r"
    offer = "https://fdp.example/offers/later"
    fetcher = _FakeFetcher(graphs={offer: _offer_ttl(offer)})

    current: dict[str, str | None] = {"value": None}
    resolver = GraphBackedOfferResolver(
        fetcher,  # type: ignore[arg-type]
        system_default_provider=lambda: current["value"],
    )

    # No default → no Offer.
    assert await resolver.resolve_offer(rec) is None

    # The deployment publishes a default later (e.g. after auto-bootstrap).
    current["value"] = offer
    result = await resolver.resolve_offer(rec)
    assert result is not None
    assert result.iri == offer
