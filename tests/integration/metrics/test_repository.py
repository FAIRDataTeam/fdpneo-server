"""Integration tests for :class:`MetricsRepository` against real Postgres.

Exercises insert, aggregation grouping, upsert merging on dimension
key, and retention deletes.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fdpneo_server.metrics.events import MetricEventType, MetricSample
from fdpneo_server.metrics.repository import (
    DailyAggregate,
    HourlyAggregate,
    MetricsHourly,
    MetricsRepository,
)

H0 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
H1 = datetime(2026, 6, 1, 13, 0, 0, tzinfo=UTC)


def _sample(
    *,
    bucket: datetime = H0,
    event_type: MetricEventType = MetricEventType.VIEW,
    resource_iri: str | None = "https://example.org/r1",
    country_code: str | None = "US",
    region: str | None = "CA",
    city: str | None = "SF",
    visitor_hash: str | None = "deadbeef" * 4,
    status_code: int = 200,
    latency_ms: int = 42,
) -> MetricSample:
    return MetricSample(
        timestamp_bucket=bucket,
        event_type=event_type,
        resource_iri=resource_iri,
        country_code=country_code,
        region=region,
        city=city,
        visitor_hash=visitor_hash,
        status_code=status_code,
        latency_ms=latency_ms,
    )


# --- insert_raw -------------------------------------------------------------


@pytest.mark.integration
async def test_insert_raw_persists_one_row(repo: MetricsRepository, session: AsyncSession) -> None:
    await repo.insert_raw(_sample())
    await session.commit()
    assert await repo.count_raw() == 1


@pytest.mark.integration
async def test_insert_raw_with_null_dimensions(
    repo: MetricsRepository, session: AsyncSession
) -> None:
    """LOGIN events have no resource; geo lookup can return all-None."""
    await repo.insert_raw(
        _sample(
            event_type=MetricEventType.LOGIN,
            resource_iri=None,
            country_code=None,
            region=None,
            city=None,
            visitor_hash=None,
        )
    )
    await session.commit()
    assert await repo.count_raw() == 1


# --- aggregate_raw_for_bucket ----------------------------------------------


@pytest.mark.integration
async def test_aggregate_raw_groups_by_dimensions(
    repo: MetricsRepository, session: AsyncSession
) -> None:
    # Two rows same dimensions; one row different city.
    await repo.insert_raw(_sample(visitor_hash="aa" * 16))
    await repo.insert_raw(_sample(visitor_hash="bb" * 16, latency_ms=58))
    await repo.insert_raw(_sample(city="Oakland", visitor_hash="cc" * 16))
    await session.commit()

    aggregates = await repo.aggregate_raw_for_bucket(H0)
    by_city = {a.city: a for a in aggregates}
    assert by_city["SF"].request_count == 2
    assert by_city["SF"].unique_visitors == 2
    assert by_city["SF"].latency_ms_sum == 42 + 58
    assert by_city["SF"].status_2xx_count == 2
    assert by_city["Oakland"].request_count == 1


@pytest.mark.integration
async def test_aggregate_raw_distinct_ignores_null_visitor_hash(
    repo: MetricsRepository, session: AsyncSession
) -> None:
    """COUNT(DISTINCT hash) skips NULLs — that's the standard SQL behavior we want."""
    await repo.insert_raw(_sample(visitor_hash=None))
    await repo.insert_raw(_sample(visitor_hash=None))
    await repo.insert_raw(_sample(visitor_hash="aa" * 16))
    await session.commit()

    aggregates = await repo.aggregate_raw_for_bucket(H0)
    assert len(aggregates) == 1
    assert aggregates[0].request_count == 3
    assert aggregates[0].unique_visitors == 1


@pytest.mark.integration
async def test_aggregate_raw_status_classes(repo: MetricsRepository, session: AsyncSession) -> None:
    await repo.insert_raw(_sample(status_code=200))
    await repo.insert_raw(_sample(status_code=204))
    await repo.insert_raw(_sample(status_code=301))
    await repo.insert_raw(_sample(status_code=404))
    await repo.insert_raw(_sample(status_code=500))
    await session.commit()

    aggregates = await repo.aggregate_raw_for_bucket(H0)
    assert len(aggregates) == 1
    a = aggregates[0]
    assert a.status_2xx_count == 2
    assert a.status_3xx_count == 1
    assert a.status_4xx_count == 1
    assert a.status_5xx_count == 1


# --- upsert_hourly ---------------------------------------------------------


def _hourly(**overrides: object) -> HourlyAggregate:
    base: dict[str, object] = {
        "bucket": H0,
        "event_type": MetricEventType.VIEW.value,
        "resource_iri": "https://example.org/r1",
        "country_code": "US",
        "region": "CA",
        "city": "SF",
        "request_count": 10,
        "unique_visitors": 4,
        "latency_ms_sum": 1000,
        "status_2xx_count": 9,
        "status_3xx_count": 0,
        "status_4xx_count": 1,
        "status_5xx_count": 0,
    }
    base.update(overrides)
    return HourlyAggregate(**base)  # type: ignore[arg-type]


@pytest.mark.integration
async def test_upsert_hourly_inserts_then_increments(
    repo: MetricsRepository, session: AsyncSession
) -> None:
    await repo.upsert_hourly(_hourly())
    await repo.upsert_hourly(
        _hourly(request_count=5, unique_visitors=2, latency_ms_sum=500, status_2xx_count=5)
    )
    await session.commit()

    row = (
        await session.execute(select(MetricsHourly).where(MetricsHourly.bucket == H0))
    ).scalar_one()
    assert row.request_count == 15
    assert row.unique_visitors == 6
    assert row.latency_ms_sum == 1500
    assert row.status_2xx_count == 14


@pytest.mark.integration
async def test_upsert_hourly_treats_null_dimensions_as_same_row(
    repo: MetricsRepository, session: AsyncSession
) -> None:
    """The unique constraint uses NULLS NOT DISTINCT, so two all-NULL rows merge."""
    null_dims = _hourly(resource_iri=None, country_code=None, region=None, city=None)
    await repo.upsert_hourly(null_dims)
    await repo.upsert_hourly(null_dims)
    await session.commit()
    assert await repo.count_hourly() == 1


# --- delete_raw_through ----------------------------------------------------


@pytest.mark.integration
async def test_delete_raw_through_drops_only_up_to_cutoff(
    repo: MetricsRepository, session: AsyncSession
) -> None:
    await repo.insert_raw(_sample(bucket=H0))
    await repo.insert_raw(_sample(bucket=H1))
    await session.commit()

    deleted = await repo.delete_raw_through(through=H0)
    await session.commit()
    assert deleted == 1
    assert await repo.count_raw() == 1


# --- daily upsert / aggregation -------------------------------------------


def _daily(**overrides: object) -> DailyAggregate:
    base: dict[str, object] = {
        "bucket": date(2026, 6, 1),
        "event_type": MetricEventType.VIEW.value,
        "resource_iri": "https://example.org/r1",
        "country_code": "US",
        "region": "CA",
        "city": "SF",
        "request_count": 100,
        "unique_visitors": 30,
        "latency_ms_sum": 4500,
        "status_2xx_count": 95,
        "status_3xx_count": 0,
        "status_4xx_count": 4,
        "status_5xx_count": 1,
    }
    base.update(overrides)
    return DailyAggregate(**base)  # type: ignore[arg-type]


@pytest.mark.integration
async def test_upsert_daily_replaces_existing_row(
    repo: MetricsRepository, session: AsyncSession
) -> None:
    await repo.upsert_daily(_daily())
    await repo.upsert_daily(_daily(request_count=200, unique_visitors=60, latency_ms_sum=9000))
    await session.commit()
    assert await repo.count_daily() == 1


@pytest.mark.integration
async def test_aggregate_hourly_for_day_sums_across_hours(
    repo: MetricsRepository, session: AsyncSession
) -> None:
    await repo.upsert_hourly(_hourly(bucket=H0))
    await repo.upsert_hourly(_hourly(bucket=H1))
    await session.commit()

    aggregates = await repo.aggregate_hourly_for_day(date(2026, 6, 1))
    assert len(aggregates) == 1
    a = aggregates[0]
    assert a.request_count == 20  # 10 + 10
    assert a.unique_visitors == 8  # approximate sum, documented
    assert a.latency_ms_sum == 2000
    assert a.status_2xx_count == 18


# --- delete_hourly_before --------------------------------------------------


@pytest.mark.integration
async def test_delete_hourly_before_is_strict_inequality(
    repo: MetricsRepository, session: AsyncSession
) -> None:
    await repo.upsert_hourly(_hourly(bucket=H0))
    await repo.upsert_hourly(_hourly(bucket=H1))
    await session.commit()

    # Cutoff equals H1 — only H0 should be removed (strict <).
    deleted = await repo.delete_hourly_before(before=H1)
    await session.commit()
    assert deleted == 1
    assert await repo.count_hourly() == 1
