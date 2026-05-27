"""arq-compatible job wrappers for the metrics rollups.

The rollup logic lives in :mod:`fdp.metrics.aggregation` as plain async
functions; this module wraps them in arq job signatures and exposes a
:class:`WorkerSettings`-compatible class that a worker process can
import.

CLAUDE.md notes that the project's intent is a Postgres-backed arq
deployment. Upstream arq is Redis-only; until that adapter exists, the
worker can either run against a Redis instance or these functions can
be invoked from any other scheduler (cron, k8s CronJob calling
``fdp metrics rollup``). The functions themselves only depend on the
``session_factory`` and ``settings`` carried in the arq ``ctx`` dict,
so they remain reusable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from fdp.metrics.aggregation import (
    RollupResult,
    roll_up_hourly_to_daily,
    roll_up_raw_to_hourly,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from fdp.config import MetricsSettings

log = structlog.get_logger(__name__)


# --- arq context keys -------------------------------------------------------

_CTX_SESSION_FACTORY = "session_factory"
_CTX_METRICS_SETTINGS = "metrics_settings"


def _factory(ctx: dict[str, Any]) -> async_sessionmaker[AsyncSession]:
    return ctx[_CTX_SESSION_FACTORY]


def _settings(ctx: dict[str, Any]) -> MetricsSettings:
    return ctx[_CTX_METRICS_SETTINGS]


# --- job functions ----------------------------------------------------------


async def rollup_raw_to_hourly_job(ctx: dict[str, Any]) -> RollupResult:
    """Fold raw rows older than the configured watermark into hourly."""
    settings = _settings(ctx)
    return await roll_up_raw_to_hourly(
        _factory(ctx),
        aggregate_after_seconds=settings.aggregate_to_hourly_after_seconds,
    )


async def rollup_hourly_to_daily_job(ctx: dict[str, Any]) -> RollupResult:
    """Fold hourly rows older than the configured retention into daily."""
    settings = _settings(ctx)
    return await roll_up_hourly_to_daily(
        _factory(ctx),
        discard_after_days=settings.discard_hourly_after_days,
    )


# --- worker settings --------------------------------------------------------


def build_cron_jobs() -> list[Any]:
    """Construct the cron job list for :class:`WorkerSettings`.

    Pulled out so a caller can compose it with other modules' cron
    entries; arq's :class:`WorkerSettings` accepts a single ``cron_jobs``
    list per worker process.

    Schedule:

    * Raw → hourly: every 5 minutes (matches the default
      ``aggregate_to_hourly_after_seconds=300``).
    * Hourly → daily: hourly at minute 7, off the top of the hour to
      avoid colliding with the rollup that produced the hourly rows.
    """
    from arq import cron

    return [
        cron(
            rollup_raw_to_hourly_job,
            minute=set(range(0, 60, 5)),
            run_at_startup=False,
        ),
        cron(
            rollup_hourly_to_daily_job,
            minute={7},
            run_at_startup=False,
        ),
    ]


__all__ = [
    "build_cron_jobs",
    "rollup_hourly_to_daily_job",
    "rollup_raw_to_hourly_job",
]
