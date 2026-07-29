"""Integration tests for the metrics rollup pipeline against Postgres."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fdpneo_server.metrics.aggregation import roll_up_hourly_to_daily, roll_up_raw_to_hourly
from fdpneo_server.metrics.events import MetricEventType, MetricSample
from fdpneo_server.metrics.repository import HourlyAggregate, MetricsRepository

H = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


def _sample(
    *,
    bucket: datetime = H,
    visitor_hash: str | None = "aa" * 16,
    status_code: int = 200,
    latency_ms: int = 42,
    city: str = "SF",
) -> MetricSample:
    return MetricSample(
        timestamp_bucket=bucket,
        event_type=MetricEventType.VIEW,
        resource_iri="https://example.org/r1",
        country_code="US",
        region="CA",
        city=city,
        visitor_hash=visitor_hash,
        status_code=status_code,
        latency_ms=latency_ms,
    )


# --- raw → hourly ----------------------------------------------------------


@pytest.mark.integration
async def test_raw_to_hourly_aggregates_and_clears_raw(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Seed raw rows in two different hour buckets.
    async with session_factory() as s:
        repo = MetricsRepository(s)
        for vh in ("aa" * 16, "bb" * 16, "cc" * 16):
            await repo.insert_raw(_sample(visitor_hash=vh))
        await repo.insert_raw(_sample(bucket=H + timedelta(hours=1), visitor_hash="dd" * 16))
        await s.commit()

    # Now is two hours past H so both buckets are well past the 5-minute
    # watermark and both should be rolled up.
    result = await roll_up_raw_to_hourly(
        session_factory,
        now=H + timedelta(hours=2),
        aggregate_after_seconds=300,
    )

    assert result.buckets_processed == 2
    assert result.aggregates_written == 2
    assert result.source_rows_deleted == 4

    async with session_factory() as s:
        repo = MetricsRepository(s)
        assert await repo.count_raw() == 0
        assert await repo.count_hourly() == 2


@pytest.mark.integration
async def test_raw_to_hourly_leaves_recent_buckets_alone(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A bucket newer than (now - watermark) must not be rolled up."""
    async with session_factory() as s:
        repo = MetricsRepository(s)
        await repo.insert_raw(_sample(bucket=H))
        await s.commit()

    # now is only 60s past H, below the 300s watermark.
    result = await roll_up_raw_to_hourly(
        session_factory,
        now=H + timedelta(seconds=60),
        aggregate_after_seconds=300,
    )
    assert result.buckets_processed == 0
    assert result.aggregates_written == 0

    async with session_factory() as s:
        repo = MetricsRepository(s)
        assert await repo.count_raw() == 1
        assert await repo.count_hourly() == 0


@pytest.mark.integration
async def test_raw_to_hourly_rerun_does_not_double_count(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A second run on an empty raw table contributes nothing."""
    async with session_factory() as s:
        repo = MetricsRepository(s)
        await repo.insert_raw(_sample(visitor_hash="aa" * 16))
        await repo.insert_raw(_sample(visitor_hash="bb" * 16))
        await s.commit()

    later = H + timedelta(hours=1)
    await roll_up_raw_to_hourly(session_factory, now=later, aggregate_after_seconds=300)
    await roll_up_raw_to_hourly(session_factory, now=later, aggregate_after_seconds=300)

    async with session_factory() as s:
        repo = MetricsRepository(s)
        agg = await repo.aggregate_hourly_for_day(H.date())
        assert len(agg) == 1
        assert agg[0].request_count == 2
        assert agg[0].unique_visitors == 2


# --- hourly → daily --------------------------------------------------------


@pytest.mark.integration
async def test_hourly_to_daily_rolls_old_buckets_and_drops_them(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    old_day = H  # 2026-06-01
    async with session_factory() as s:
        repo = MetricsRepository(s)
        for offset in (0, 5, 11):  # three hours in the same day
            await repo.upsert_hourly(
                HourlyAggregate(
                    bucket=old_day + timedelta(hours=offset),
                    event_type=MetricEventType.VIEW.value,
                    resource_iri="https://example.org/r1",
                    country_code="US",
                    region="CA",
                    city="SF",
                    request_count=10,
                    unique_visitors=3,
                    latency_ms_sum=420,
                    status_2xx_count=10,
                    status_3xx_count=0,
                    status_4xx_count=0,
                    status_5xx_count=0,
                )
            )
        await s.commit()

    # Now is three days after old_day; with discard_after_days=2 the
    # cutoff lands on day (now - 2d) = old_day + 1d, so every hour in
    # old_day is < cutoff and should be rolled to daily.
    now = old_day + timedelta(days=3)
    result = await roll_up_hourly_to_daily(
        session_factory,
        now=now,
        discard_after_days=2,
    )
    assert result.buckets_processed == 1  # one calendar day rolled up
    assert result.aggregates_written == 1
    assert result.source_rows_deleted == 3

    async with session_factory() as s:
        repo = MetricsRepository(s)
        assert await repo.count_hourly() == 0
        assert await repo.count_daily() == 1


@pytest.mark.integration
async def test_hourly_to_daily_keeps_recent_hours(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An hourly row newer than the cutoff stays put."""
    async with session_factory() as s:
        repo = MetricsRepository(s)
        await repo.upsert_hourly(
            HourlyAggregate(
                bucket=H,
                event_type=MetricEventType.VIEW.value,
                resource_iri="https://example.org/r1",
                country_code="US",
                region="CA",
                city="SF",
                request_count=10,
                unique_visitors=3,
                latency_ms_sum=420,
                status_2xx_count=10,
                status_3xx_count=0,
                status_4xx_count=0,
                status_5xx_count=0,
            )
        )
        await s.commit()

    # Only 12 hours after H — well inside the 2-day retention.
    result = await roll_up_hourly_to_daily(
        session_factory,
        now=H + timedelta(hours=12),
        discard_after_days=2,
    )
    assert result.buckets_processed == 0
    async with session_factory() as s:
        repo = MetricsRepository(s)
        assert await repo.count_hourly() == 1
        assert await repo.count_daily() == 0
