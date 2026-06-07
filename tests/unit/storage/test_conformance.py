"""Unit tests for the named-graph isolation self-test (audit R-03)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

from fdp.storage.triplestore.conformance import verify_named_graph_isolation


@dataclass
class _FakeAdapter:
    count: int = 2
    fail_query: bool = False
    updates: list[str] = field(default_factory=list)

    async def update(self, sparql: str, **_kw: object) -> None:
        self.updates.append(sparql)

    async def query(
        self, sparql: str, *, named_graph_uris: tuple[str, ...] = (), **_kw: object
    ) -> bytes:
        if self.fail_query:
            raise RuntimeError("store unreachable")
        return json.dumps({"results": {"bindings": [{"c": {"value": str(self.count)}}]}}).encode(
            "utf-8"
        )


@pytest.mark.unit
async def test_conformant_store_passes_and_cleans_up() -> None:
    adapter = _FakeAdapter(count=2)
    assert await verify_named_graph_isolation(adapter) is True  # type: ignore[arg-type]
    # Probe graphs were inserted and dropped (cleanup ran in finally).
    assert sum(1 for u in adapter.updates if u.startswith("INSERT DATA")) == 3
    assert sum(1 for u in adapter.updates if "DROP SILENT GRAPH" in u) >= 3


@pytest.mark.unit
async def test_store_that_leaks_extra_graph_fails() -> None:
    # Projection of two graphs returned 3 → a third (unauthorized) graph leaked.
    assert await verify_named_graph_isolation(_FakeAdapter(count=3)) is False  # type: ignore[arg-type]


@pytest.mark.unit
async def test_store_that_drops_repeated_param_fails() -> None:
    # Only one graph counted → repeated named-graph-uri was not unioned.
    assert await verify_named_graph_isolation(_FakeAdapter(count=1)) is False  # type: ignore[arg-type]


@pytest.mark.unit
async def test_probe_error_fails_closed() -> None:
    assert await verify_named_graph_isolation(_FakeAdapter(fail_query=True)) is False  # type: ignore[arg-type]
