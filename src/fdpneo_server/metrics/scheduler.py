"""In-process periodic driver for the metrics rollups.

The rollup logic (:mod:`fdpneo_server.metrics.aggregation`) is plain async functions
designed to be cron-driven — ``fdp metrics rollup`` is the production path
(external cron / k8s ``CronJob``). But a single-process deployment, or the dev
stack that runs the server on the host with no external scheduler, would never
aggregate: ``metrics_raw`` fills up and the dashboard (which reads the rolled-up
tables) stays empty.

This scheduler closes that gap by running both rollups on a background asyncio
task started from the app lifespan, gated by ``MetricsSettings.rollup_in_process``
(off by default so it never double-runs alongside an external cron). It mirrors
the start/stop shape of :class:`fdpneo_server.metrics.pipeline.MetricsPipeline`.

Each pass is best-effort: a failure is logged and the loop continues, so a
transient Postgres hiccup never tears down the task. The rollups themselves are
transactional and idempotent (:mod:`fdpneo_server.metrics.aggregation`), so a missed or
retried pass is safe.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

import structlog

from fdpneo_server.metrics.aggregation import roll_up_hourly_to_daily, roll_up_raw_to_hourly

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from fdpneo_server.config import MetricsSettings

log = structlog.get_logger(__name__)


class MetricsRollupScheduler:
    """Runs raw→hourly→daily on a fixed interval until stopped.

    One instance per process. :meth:`start` launches the loop task (no-op when
    disabled); :meth:`stop` cancels and awaits it. Construction takes the
    session factory + settings by injection so tests can drive it directly.
    """

    __slots__ = ("_factory", "_settings", "_task")

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        settings: MetricsSettings,
    ) -> None:
        self._factory = session_factory
        self._settings = settings
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Launch the background loop unless disabled or already running."""
        if not (self._settings.enabled and self._settings.rollup_in_process):
            return
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._loop(), name="metrics-rollup")
        log.info(
            "metrics_rollup_scheduler_started",
            interval_seconds=self._settings.rollup_interval_seconds,
        )

    @property
    def running(self) -> bool:
        """True while the background loop task is active."""
        return self._task is not None

    async def stop(self) -> None:
        """Cancel and await the loop task. Idempotent."""
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def run_once(self) -> None:
        """Run both rollup steps once; log and swallow failures."""
        try:
            await roll_up_raw_to_hourly(
                self._factory,
                aggregate_after_seconds=self._settings.aggregate_to_hourly_after_seconds,
            )
            await roll_up_hourly_to_daily(
                self._factory,
                discard_after_days=self._settings.discard_hourly_after_days,
            )
        except Exception as err:  # never let a transient failure kill the loop
            log.warning("metrics_rollup_pass_failed", error=repr(err))

    async def _loop(self) -> None:
        interval = max(1, self._settings.rollup_interval_seconds)
        while True:
            await self.run_once()
            await asyncio.sleep(interval)


__all__ = ["MetricsRollupScheduler"]
