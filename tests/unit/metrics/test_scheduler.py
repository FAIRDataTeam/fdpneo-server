"""Unit tests for the in-process rollup scheduler."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import Mock

import pytest

from fdp.config import MetricsSettings
from fdp.metrics import scheduler as scheduler_mod
from fdp.metrics.scheduler import MetricsRollupScheduler


def _settings(*, rollup_in_process: bool = True) -> MetricsSettings:
    return MetricsSettings(
        enabled=True,
        rollup_in_process=rollup_in_process,
        rollup_interval_seconds=1,
        aggregate_to_hourly_after_seconds=300,
        discard_hourly_after_days=2,
    )


def _patch_rollups(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    calls: dict[str, list[Any]] = {"raw": [], "daily": []}

    async def fake_raw(_factory: Any, *, aggregate_after_seconds: int) -> None:
        calls["raw"].append(aggregate_after_seconds)

    async def fake_daily(_factory: Any, *, discard_after_days: int) -> None:
        calls["daily"].append(discard_after_days)

    monkeypatch.setattr(scheduler_mod, "roll_up_raw_to_hourly", fake_raw)
    monkeypatch.setattr(scheduler_mod, "roll_up_hourly_to_daily", fake_daily)
    return calls


@pytest.mark.unit
async def test_run_once_invokes_both_rollups_with_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_rollups(monkeypatch)
    sched = MetricsRollupScheduler(session_factory=Mock(), settings=_settings())
    await sched.run_once()
    assert calls["raw"] == [300]
    assert calls["daily"] == [2]


@pytest.mark.unit
async def test_run_once_swallows_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(_factory: Any, **_kw: Any) -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr(scheduler_mod, "roll_up_raw_to_hourly", boom)
    monkeypatch.setattr(scheduler_mod, "roll_up_hourly_to_daily", boom)
    sched = MetricsRollupScheduler(session_factory=Mock(), settings=_settings())
    await sched.run_once()  # must not raise


@pytest.mark.unit
async def test_start_is_noop_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_rollups(monkeypatch)
    sched = MetricsRollupScheduler(
        session_factory=Mock(), settings=_settings(rollup_in_process=False)
    )
    sched.start()
    await asyncio.sleep(0.02)
    await sched.stop()  # idempotent even though nothing started
    assert sched.running is False


@pytest.mark.unit
async def test_start_runs_loop_then_stop_cancels(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_rollups(monkeypatch)
    sched = MetricsRollupScheduler(session_factory=Mock(), settings=_settings())
    sched.start()
    assert sched.running is True
    await asyncio.sleep(0.05)  # let at least one pass run
    await sched.stop()
    assert len(calls["raw"]) >= 1
    assert sched.running is False
