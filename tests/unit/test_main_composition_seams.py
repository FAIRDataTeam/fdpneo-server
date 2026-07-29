"""Unit tests for the downstream composition seams on ``create_app`` (ADR-0023).

Two seams, both keyword-only and optional:

* ``triple_store_factory`` — replaces the internal
  ``TripleStoreAdapter.from_settings`` call, so a downstream can mediate
  every RDF read/write without monkeypatching.
* ``extension_routers`` — mounted after the reserved ``/fdp-api`` routers
  and before the LDP catch-all, so extensions win the paths they claim.
"""

from __future__ import annotations

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from fdpneo_server.config import TripleStoreSettings, get_settings
from fdpneo_server.main import create_app
from fdpneo_server.storage.triplestore.adapter import TripleStoreAdapter

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _fresh_settings() -> None:  # pyright: ignore[reportUnusedFunction]
    get_settings.cache_clear()


class _MediatingAdapter(TripleStoreAdapter):
    """Stand-in for a downstream wrapper (telemetry, driver quirks, budgets)."""


def test_default_adapter_is_used_without_a_factory() -> None:
    app = create_app()
    assert type(app.state.triplestore) is TripleStoreAdapter


def test_triple_store_factory_builds_the_shared_adapter() -> None:
    seen: list[TripleStoreSettings] = []

    def factory(ts_settings: TripleStoreSettings) -> TripleStoreAdapter:
        seen.append(ts_settings)
        return _MediatingAdapter.from_settings(ts_settings)

    app = create_app(triple_store_factory=factory)

    # The factory was called once, with the resolved triple store settings…
    assert seen == [get_settings().triplestore]
    # …and its product is the shared adapter every service composes over.
    assert isinstance(app.state.triplestore, _MediatingAdapter)
    assert app.state.metadata_repository._adapter is app.state.triplestore


def test_extension_router_wins_over_the_ldp_catch_all() -> None:
    """Behavioral precedence check, deliberately free of route-table introspection.

    If extension routers were mounted after the LDP router, its
    ``/{path:path}`` catch-all would swallow the request (and fail —
    there is no triple store in a unit test). A 200 with the extension's
    payload therefore proves the mount order.
    """
    router = APIRouter()

    @router.get("/ext/ping")
    async def ping() -> dict[str, bool]:  # pyright: ignore[reportUnusedFunction]
        return {"pong": True}

    app = create_app(extension_routers=[router])
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/ext/ping")
    assert response.status_code == 200
    assert response.json() == {"pong": True}
