"""Rollup logic for the metrics pipeline.

Two functions, both designed to be cron-driven (architecture §11.5,
ADR-0002):

* :func:`roll_up_raw_to_hourly` — fold every ``metrics_raw`` row whose
  bucket is at least ``aggregate_after_seconds`` old into the matching
  ``metrics_hourly`` row, then delete the raw rows. "Discarded within
  minutes" is the privacy guarantee.
* :func:`roll_up_hourly_to_daily` — fold ``metrics_hourly`` rows whose
  day is at least ``discard_after_days`` old into ``metrics_daily``,
  then delete the hourly rows.

Both are idempotent: hourly upsert increments on conflict (a partial
rerun adds the *new* increments only because the raw rows it sourced
are removed in the same transaction), and the daily upsert replaces
the full day's row from the current hourly window.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import structlog

from fdpneo_server.metrics.repository import MetricsRepository

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class RollupResult:
    """Counters returned by a rollup invocation, for logging and tests."""

    buckets_processed: int
    aggregates_written: int
    source_rows_deleted: int


async def roll_up_raw_to_hourly(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime | None = None,
    aggregate_after_seconds: int,
) -> RollupResult:
    """Aggregate raw rows older than the watermark into ``metrics_hourly``.

    The watermark is ``now - aggregate_after_seconds`` rounded down to
    the hour. Every raw bucket at or before the watermark is grouped
    by dimensions, upserted into ``metrics_hourly``, and then the
    source raw rows for those buckets are deleted in the same
    transaction. If the transaction rolls back, neither the upsert
    nor the delete takes effect.
    """
    now_ts = now or datetime.now(UTC)
    cutoff = (now_ts - timedelta(seconds=aggregate_after_seconds)).replace(
        minute=0, second=0, microsecond=0
    )

    buckets_processed = 0
    aggregates_written = 0
    deleted = 0
    async with session_factory() as session:
        repo = MetricsRepository(session)
        buckets = await repo.list_raw_buckets_through(through=cutoff)
        for bucket in buckets:
            aggregates = await repo.aggregate_raw_for_bucket(bucket)
            for agg in aggregates:
                await repo.upsert_hourly(agg)
                aggregates_written += 1
            buckets_processed += 1
        if buckets:
            # Delete every raw row whose bucket is at or before the cutoff.
            # Includes any rows inserted *between* the bucket listing above
            # and this delete — those are by definition older than the
            # watermark and would belong to a bucket we've already
            # aggregated, so dropping them is a tiny under-count we accept.
            deleted = await repo.delete_raw_through(through=cutoff)
        await session.commit()

    log.info(
        "metrics_rollup_raw_to_hourly",
        cutoff=cutoff.isoformat(),
        buckets_processed=buckets_processed,
        aggregates_written=aggregates_written,
        raw_rows_deleted=deleted,
    )
    return RollupResult(
        buckets_processed=buckets_processed,
        aggregates_written=aggregates_written,
        source_rows_deleted=deleted,
    )


async def roll_up_hourly_to_daily(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime | None = None,
    discard_after_days: int,
) -> RollupResult:
    """Aggregate hourly rows older than the watermark into ``metrics_daily``.

    The watermark is the start of the day ``now - discard_after_days``.
    Every hourly bucket strictly before that watermark is grouped per
    calendar day, upserted into ``metrics_daily`` (replace semantics),
    and the source hourly rows are deleted.

    Daily unique-visitor counts are an approximation; see
    :meth:`MetricsRepository.aggregate_hourly_for_day` for the
    rationale.
    """
    now_ts = now or datetime.now(UTC)
    cutoff_day = (now_ts - timedelta(days=discard_after_days)).date()
    cutoff_dt = datetime.combine(cutoff_day, datetime.min.time(), tzinfo=UTC)

    days_processed = 0
    aggregates_written = 0
    deleted = 0
    async with session_factory() as session:
        repo = MetricsRepository(session)
        hourly_buckets = await repo.list_hourly_buckets_before(before=cutoff_dt)
        seen_days: set[datetime] = set()
        for hb in hourly_buckets:
            day_start = hb.replace(hour=0, minute=0, second=0, microsecond=0)
            if day_start in seen_days:
                continue
            seen_days.add(day_start)
            aggregates = await repo.aggregate_hourly_for_day(day_start.date())
            for agg in aggregates:
                await repo.upsert_daily(agg)
                aggregates_written += 1
            days_processed += 1
        if hourly_buckets:
            deleted = await repo.delete_hourly_before(before=cutoff_dt)
        await session.commit()

    log.info(
        "metrics_rollup_hourly_to_daily",
        cutoff_day=cutoff_day.isoformat(),
        days_processed=days_processed,
        aggregates_written=aggregates_written,
        hourly_rows_deleted=deleted,
    )
    return RollupResult(
        buckets_processed=days_processed,
        aggregates_written=aggregates_written,
        source_rows_deleted=deleted,
    )


__all__ = [
    "RollupResult",
    "roll_up_hourly_to_daily",
    "roll_up_raw_to_hourly",
]
