"""Unit tests for the anonymous authz-cache warmup (task 11.2 / ADR-0009).

The warmup pre-populates the PDP's cache for the anonymous subject
against a small set of high-traffic IRIs so the first anonymous read
doesn't pay a cache-miss roundtrip. Since ADR-0009 the warming is split:

* :func:`_warm_anonymous_authz_cache` warms just the FDP root (the
  universal landing page) at startup.
* Typed container prefixes are warmed by ``_publish_resource_definitions``
  (startup *and* every runtime resource-definition mutation), via
  :func:`_container_targets` + :func:`_warm_authz`.

Covered here: root warming, container-target derivation, and that the
shared :func:`_warm_authz` loop swallows per-target failures.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI

from fdpneo_server.main import _container_targets, _warm_anonymous_authz_cache, _warm_authz
from fdpneo_server.metadata.profiles.registry import (
    ResourceDefinition,
    ResourceDefinitionCache,
)
from fdpneo_server.policy.model import Action, Decision, Outcome
from fdpneo_server.shared.context import RequestContext


class _FakePDP:
    """``RequestScopedPDP`` stand-in recording every authorize call."""

    def __init__(self, *, fail_on: set[str] | None = None) -> None:
        self._fail_on = fail_on or set()
        self.calls: list[tuple[bool, Action, str]] = []

    async def authorize(self, ctx: RequestContext, action: Action, resource_iri: str) -> Decision:
        self.calls.append((ctx.is_anonymous, action, resource_iri))
        if resource_iri in self._fail_on:
            raise RuntimeError(f"backend down for {resource_iri}")
        return Decision(outcome=Outcome.PERMIT, rule=None, reason="warmup")

    async def authorized_graphs(
        self, ctx: RequestContext, action: Action
    ) -> set[str]:  # pragma: no cover — unused by the warmup
        del ctx, action
        return set()


def _build_app_with(
    *,
    pdp: _FakePDP,
    cache: ResourceDefinitionCache | None,
) -> FastAPI:
    app = FastAPI()
    app.state.pdp = pdp
    app.state.resource_definitions = cache
    return app


# --- tests ---------------------------------------------------------------


@pytest.mark.unit
async def test_warmup_calls_authorize_for_root_when_cache_is_none() -> None:
    pdp = _FakePDP()
    app = _build_app_with(pdp=pdp, cache=None)
    await _warm_anonymous_authz_cache(app)
    # Root is always warmed even without a resource-definition cache.
    assert len(pdp.calls) == 1
    is_anonymous, action, resource_iri = pdp.calls[0]
    assert is_anonymous is True
    assert action is Action.READ
    # ``record_graph_uri`` strips the trailing slash on the root.
    assert resource_iri.startswith("http://")


def _three_type_cache() -> ResourceDefinitionCache:
    return ResourceDefinitionCache(
        [
            ResourceDefinition(
                url_prefix="",
                name="Repository",
                schema_iri="https://w3id.org/fdp/o#Repository",
                children=(),
            ),
            ResourceDefinition(
                url_prefix="catalog",
                name="Catalog",
                schema_iri="http://www.w3.org/ns/dcat#Catalog",
                children=(),
            ),
            ResourceDefinition(
                url_prefix="dataset",
                name="Dataset",
                schema_iri="http://www.w3.org/ns/dcat#Dataset",
                children=(),
            ),
        ],
        base_url="http://localhost:8000",
    )


@pytest.mark.unit
def test_container_targets_lists_non_root_containers() -> None:
    targets = set(_container_targets(_three_type_cache()))
    assert targets == {
        "http://localhost:8000/catalog",
        "http://localhost:8000/dataset",
    }


@pytest.mark.unit
async def test_warm_authz_warms_each_given_target() -> None:
    pdp = _FakePDP()
    app = _build_app_with(pdp=pdp, cache=None)
    await _warm_authz(app, _container_targets(_three_type_cache()))
    iris = sorted(c[2] for c in pdp.calls)
    assert iris == ["http://localhost:8000/catalog", "http://localhost:8000/dataset"]
    assert all(is_anon and action is Action.READ for is_anon, action, _ in pdp.calls)


@pytest.mark.unit
async def test_warm_authz_continues_when_one_target_fails() -> None:
    """A single PDP failure must not abort warming the rest."""
    pdp = _FakePDP(fail_on={"http://localhost:8000/catalog"})
    app = _build_app_with(pdp=pdp, cache=None)
    # Should NOT raise.
    await _warm_authz(
        app,
        ["http://localhost:8000/catalog", "http://localhost:8000/dataset"],
    )
    # Both were attempted; the failure was caught.
    assert len(pdp.calls) == 2


@pytest.mark.unit
async def test_warmup_passes_anonymous_context() -> None:
    pdp = _FakePDP()
    app = _build_app_with(pdp=pdp, cache=None)
    await _warm_anonymous_authz_cache(app)
    is_anonymous, *_ = pdp.calls[0]
    assert is_anonymous is True


@pytest.mark.unit
async def test_warmup_uses_read_action() -> None:
    pdp = _FakePDP()
    app = _build_app_with(pdp=pdp, cache=None)
    await _warm_anonymous_authz_cache(app)
    _, action, _ = pdp.calls[0]
    assert action is Action.READ


@pytest.mark.unit
async def test_warmup_with_root_only_resource_definition() -> None:
    """A cache containing only the root RD still warms exactly one target."""
    pdp = _FakePDP()
    cache = ResourceDefinitionCache(
        [
            ResourceDefinition(
                url_prefix="",
                name="Repository",
                schema_iri="urn:r",
                children=(),
            ),
        ],
        base_url="http://localhost:8000",
    )
    app = _build_app_with(pdp=pdp, cache=cache)
    await _warm_anonymous_authz_cache(app)
    assert len(pdp.calls) == 1


@pytest.mark.unit
async def test_warmup_is_idempotent_at_pdp_call_count() -> None:
    """Calling the warmup twice doubles the authorize-call count.

    The cache itself dedups; the warmup just hits the PDP again. This
    test pins the behaviour so an accidental "don't re-call" change
    can't slip through.
    """
    pdp = _FakePDP()
    app = _build_app_with(pdp=pdp, cache=None)
    await _warm_anonymous_authz_cache(app)
    await _warm_anonymous_authz_cache(app)
    assert len(pdp.calls) == 2
