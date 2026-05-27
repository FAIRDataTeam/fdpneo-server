"""Integration tests for :class:`MetricsReader` against real Postgres.

Seeds ``metrics_daily`` with a handful of rows spanning multiple days,
event types, resources, and countries; then exercises every reader
method with the dimension filters the dashboard endpoints expose.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from fdp.metrics.events import MetricEventType
from fdp.metrics.reader import MetricsReader
from fdp.metrics.repository import DailyAggregate, MetricsRepository


def _agg(
    *,
    bucket: date,
    event_type: MetricEventType = MetricEventType.VIEW,
    resource_iri: str | None = "https://example.org/r1",
    country_code: str | None = "US",
    region: str | None = "CA",
    city: str | None = "SF",
    request_count: int = 10,
    unique_visitors: int = 3,
    latency_ms_sum: int = 500,
    status_2xx_count: int = 10,
    status_3xx_count: int = 0,
    status_4xx_count: int = 0,
    status_5xx_count: int = 0,
) -> DailyAggregate:
    return DailyAggregate(
        bucket=bucket,
        event_type=event_type.value,
        resource_iri=resource_iri,
        country_code=country_code,
        region=region,
        city=city,
        request_count=request_count,
        unique_visitors=unique_visitors,
        latency_ms_sum=latency_ms_sum,
        status_2xx_count=status_2xx_count,
        status_3xx_count=status_3xx_count,
        status_4xx_count=status_4xx_count,
        status_5xx_count=status_5xx_count,
    )


@pytest.fixture
async def seeded(repo: MetricsRepository, session: AsyncSession) -> None:
    """Seed daily rows spanning 2026-05-29 through 2026-05-31."""
    rows = [
        # 5/29 — r1 in US/CA/SF
        _agg(bucket=date(2026, 5, 29), request_count=5, unique_visitors=2, latency_ms_sum=100),
        # 5/30 — r1 in US/CA/SF + r2 in US/CA/SF
        _agg(bucket=date(2026, 5, 30), request_count=10, unique_visitors=3, latency_ms_sum=400),
        _agg(
            bucket=date(2026, 5, 30),
            resource_iri="https://example.org/r2",
            request_count=20,
            unique_visitors=4,
            latency_ms_sum=600,
        ),
        # 5/31 — r1 in DE/Berlin + r1 in US/CA/SF + a LOGIN (no resource)
        _agg(
            bucket=date(2026, 5, 31),
            country_code="DE",
            region="Berlin",
            city="Berlin",
            request_count=8,
            unique_visitors=4,
            latency_ms_sum=200,
        ),
        _agg(
            bucket=date(2026, 5, 31),
            request_count=12,
            unique_visitors=5,
            latency_ms_sum=300,
            status_2xx_count=10,
            status_4xx_count=2,
        ),
        _agg(
            bucket=date(2026, 5, 31),
            event_type=MetricEventType.LOGIN,
            resource_iri=None,
            request_count=4,
            unique_visitors=4,
            latency_ms_sum=80,
        ),
    ]
    for agg in rows:
        await repo.upsert_daily(agg)
    await session.commit()


@pytest.fixture
def reader(session: AsyncSession) -> MetricsReader:
    return MetricsReader(session)


# --- summary --------------------------------------------------------------


@pytest.mark.integration
async def test_summary_aggregates_full_range(
    seeded: None, reader: MetricsReader
) -> None:
    del seeded
    totals = await reader.summary(since=date(2026, 5, 29), until=date(2026, 5, 31))
    # 5 + 10 + 20 + 8 + 12 + 4
    assert totals.request_count == 59
    # 2 + 3 + 4 + 4 + 5 + 4
    assert totals.unique_visitors == 22
    assert totals.latency_ms_avg is not None
    assert totals.latency_ms_avg == pytest.approx((100 + 400 + 600 + 200 + 300 + 80) / 59)
    # status counts from the 5/31 r1 row contributed 2 4xx; everything else 2xx.
    assert totals.status_4xx_count == 2


@pytest.mark.integration
async def test_summary_respects_date_window(
    seeded: None, reader: MetricsReader
) -> None:
    del seeded
    totals = await reader.summary(since=date(2026, 5, 30), until=date(2026, 5, 30))
    assert totals.request_count == 30
    assert totals.unique_visitors == 7


@pytest.mark.integration
async def test_summary_filters_by_resource(
    seeded: None, reader: MetricsReader
) -> None:
    del seeded
    totals = await reader.summary(
        since=date(2026, 5, 29),
        until=date(2026, 5, 31),
        resource_iri="https://example.org/r2",
    )
    assert totals.request_count == 20  # only the 5/30 r2 row
    assert totals.unique_visitors == 4


@pytest.mark.integration
async def test_summary_filters_by_event_type(
    seeded: None, reader: MetricsReader
) -> None:
    del seeded
    totals = await reader.summary(
        since=date(2026, 5, 29),
        until=date(2026, 5, 31),
        event_type=MetricEventType.LOGIN.value,
    )
    assert totals.request_count == 4
    assert totals.unique_visitors == 4


@pytest.mark.integration
async def test_summary_with_no_rows_returns_zero_and_none_avg(
    reader: MetricsReader,
) -> None:
    totals = await reader.summary(since=date(2026, 1, 1), until=date(2026, 1, 31))
    assert totals.request_count == 0
    assert totals.latency_ms_avg is None


# --- daily_series ---------------------------------------------------------


@pytest.mark.integration
async def test_daily_series_orders_ascending_and_groups_per_day(
    seeded: None, reader: MetricsReader
) -> None:
    del seeded
    points = await reader.daily_series(since=date(2026, 5, 29), until=date(2026, 5, 31))
    assert [p.bucket for p in points] == [
        date(2026, 5, 29),
        date(2026, 5, 30),
        date(2026, 5, 31),
    ]
    assert points[0].request_count == 5
    assert points[1].request_count == 30  # 10 + 20
    assert points[2].request_count == 24  # 8 + 12 + 4


# --- top_resources --------------------------------------------------------


@pytest.mark.integration
async def test_top_resources_orders_descending_and_excludes_null(
    seeded: None, reader: MetricsReader
) -> None:
    del seeded
    rows = await reader.top_resources(since=date(2026, 5, 29), until=date(2026, 5, 31))
    # r1 across multiple rows + r2 single row, NULL resource (LOGIN) excluded.
    assert [r.resource_iri for r in rows] == [
        "https://example.org/r1",
        "https://example.org/r2",
    ]
    assert rows[0].request_count == 5 + 10 + 8 + 12  # r1 contributions
    assert rows[1].request_count == 20


@pytest.mark.integration
async def test_top_resources_respects_limit(
    seeded: None, reader: MetricsReader
) -> None:
    del seeded
    rows = await reader.top_resources(
        since=date(2026, 5, 29), until=date(2026, 5, 31), limit=1
    )
    assert len(rows) == 1
    assert rows[0].resource_iri == "https://example.org/r1"


# --- geography ------------------------------------------------------------


@pytest.mark.integration
async def test_geography_aggregates_per_country(
    seeded: None, reader: MetricsReader
) -> None:
    del seeded
    rows = await reader.geography(since=date(2026, 5, 29), until=date(2026, 5, 31))
    by_country = {r.country_code: r for r in rows}
    # US: 5 + 10 + 20 + 12 + 4 = 51
    assert by_country["US"].request_count == 51
    # DE: 8
    assert by_country["DE"].request_count == 8
    # Order is descending by request count.
    assert rows[0].country_code == "US"
