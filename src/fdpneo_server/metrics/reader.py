"""Read-only queries that back the dashboard API (architecture §11.6).

Reads span all three retention tiers so the dashboard reflects *recent*
activity, not just fully-aged days:

* ``metrics_daily`` — days already rolled up (older than
  ``MetricsSettings.discard_hourly_after_days``).
* ``metrics_hourly`` — recent days not yet folded into daily.
* ``metrics_raw`` — the last few minutes not yet folded into hourly,
  aggregated on the fly.

The rollups *delete* source rows once they aggregate them
(:mod:`fdpneo_server.metrics.aggregation`), so a given (day, dimensions) tuple lives in
exactly one tier — the union never double-counts. Each tier is projected to a
common (date, dimensions, counters) shape and unioned; the endpoint queries then
sum/group over that union. ``unique_visitors`` remains an approximation (it sums
pre-aggregated per-bucket distinct counts), unchanged from the daily-only design.

The reader is a separate class from :class:`MetricsRepository` so dashboard
handlers depend only on a read-only surface — they can't accidentally write.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any

from sqlalchemy import Date, Select, Subquery, cast, desc, distinct, func, select, union_all

from fdpneo_server.metrics.repository import (
    MetricsDaily,
    MetricsHourly,
    MetricsRaw,
    _status_class_sum,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class SummaryTotals:
    """Aggregate counters over the requested period."""

    request_count: int
    unique_visitors: int
    latency_ms_avg: float | None
    status_2xx_count: int
    status_3xx_count: int
    status_4xx_count: int
    status_5xx_count: int


@dataclass(frozen=True)
class DailyPoint:
    """One bucket on the dashboard's daily time-series."""

    bucket: date
    request_count: int
    unique_visitors: int


@dataclass(frozen=True)
class ResourceCount:
    """One row in the top-resources list."""

    resource_iri: str | None
    request_count: int
    unique_visitors: int


@dataclass(frozen=True)
class CountryCount:
    """One row in the geography breakdown."""

    country_code: str | None
    request_count: int
    unique_visitors: int


def _unified_window(since: date, until: date) -> Subquery:
    """A ``UNION ALL`` of daily + hourly + raw, projected to (date, dims, counters).

    Each tier contributes the slice of ``[since, until]`` it still holds; because
    the rollups delete source rows, the slices are disjoint. Date filtering is
    pushed into each branch (the bucket column differs per tier); dimension
    filters (``resource_iri`` / ``event_type``) are applied by the caller on the
    returned subquery.
    """
    # Already day-bucketed: project straight through.
    daily = select(
        MetricsDaily.bucket.label("bucket"),
        MetricsDaily.event_type.label("event_type"),
        MetricsDaily.resource_iri.label("resource_iri"),
        MetricsDaily.country_code.label("country_code"),
        MetricsDaily.request_count.label("request_count"),
        MetricsDaily.unique_visitors.label("unique_visitors"),
        MetricsDaily.latency_ms_sum.label("latency_ms_sum"),
        MetricsDaily.status_2xx_count.label("status_2xx_count"),
        MetricsDaily.status_3xx_count.label("status_3xx_count"),
        MetricsDaily.status_4xx_count.label("status_4xx_count"),
        MetricsDaily.status_5xx_count.label("status_5xx_count"),
    ).where(MetricsDaily.bucket >= since, MetricsDaily.bucket <= until)

    # Hour-bucketed: collapse the timestamp to its date; counters are already
    # aggregated per hour, so the outer sum folds the hours into the day.
    hourly_day = cast(MetricsHourly.bucket, Date)
    hourly = select(
        hourly_day.label("bucket"),
        MetricsHourly.event_type,
        MetricsHourly.resource_iri,
        MetricsHourly.country_code,
        MetricsHourly.request_count,
        MetricsHourly.unique_visitors,
        MetricsHourly.latency_ms_sum,
        MetricsHourly.status_2xx_count,
        MetricsHourly.status_3xx_count,
        MetricsHourly.status_4xx_count,
        MetricsHourly.status_5xx_count,
    ).where(hourly_day >= since, hourly_day <= until)

    # Per-event: aggregate on the fly to (date, dims) so it unions cleanly.
    raw_day = cast(MetricsRaw.bucket, Date)
    raw = (
        select(
            raw_day.label("bucket"),
            MetricsRaw.event_type,
            MetricsRaw.resource_iri,
            MetricsRaw.country_code,
            func.count().label("request_count"),
            func.count(distinct(MetricsRaw.visitor_hash)).label("unique_visitors"),
            func.coalesce(func.sum(MetricsRaw.latency_ms), 0).label("latency_ms_sum"),
            _status_class_sum(200).label("status_2xx_count"),
            _status_class_sum(300).label("status_3xx_count"),
            _status_class_sum(400).label("status_4xx_count"),
            _status_class_sum(500).label("status_5xx_count"),
        )
        .where(raw_day >= since, raw_day <= until)
        .group_by(
            raw_day,
            MetricsRaw.event_type,
            MetricsRaw.resource_iri,
            MetricsRaw.country_code,
        )
    )

    return union_all(daily, hourly, raw).subquery("metrics_window")


class MetricsReader:
    """Read-only access to the unified metrics window for the dashboard.

    Every method takes ``since`` / ``until`` (inclusive date bounds) and an
    optional ``resource_iri`` filter. ``event_type`` is optional on most
    endpoints; ``None`` aggregates across all event types.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def summary(
        self,
        *,
        since: date,
        until: date,
        resource_iri: str | None = None,
        event_type: str | None = None,
    ) -> SummaryTotals:
        """Totals over the period for the optional dimension filters."""
        w = _unified_window(since, until)
        stmt = select(
            func.coalesce(func.sum(w.c.request_count), 0),
            func.coalesce(func.sum(w.c.unique_visitors), 0),
            func.coalesce(func.sum(w.c.latency_ms_sum), 0),
            func.coalesce(func.sum(w.c.status_2xx_count), 0),
            func.coalesce(func.sum(w.c.status_3xx_count), 0),
            func.coalesce(func.sum(w.c.status_4xx_count), 0),
            func.coalesce(func.sum(w.c.status_5xx_count), 0),
        )
        stmt = _filter(stmt, w, resource_iri, event_type)
        row = (await self._session.execute(stmt)).one()
        request_count = int(row[0])
        avg = float(row[2]) / request_count if request_count else None
        return SummaryTotals(
            request_count=request_count,
            unique_visitors=int(row[1]),
            latency_ms_avg=avg,
            status_2xx_count=int(row[3]),
            status_3xx_count=int(row[4]),
            status_4xx_count=int(row[5]),
            status_5xx_count=int(row[6]),
        )

    async def daily_series(
        self,
        *,
        since: date,
        until: date,
        resource_iri: str | None = None,
        event_type: str | None = None,
    ) -> list[DailyPoint]:
        """Per-day totals across the period, ordered by bucket ascending."""
        w = _unified_window(since, until)
        stmt = (
            select(
                w.c.bucket,
                func.coalesce(func.sum(w.c.request_count), 0),
                func.coalesce(func.sum(w.c.unique_visitors), 0),
            )
            .group_by(w.c.bucket)
            .order_by(w.c.bucket)
        )
        stmt = _filter(stmt, w, resource_iri, event_type)
        result = await self._session.execute(stmt)
        return [
            DailyPoint(bucket=row[0], request_count=int(row[1]), unique_visitors=int(row[2]))
            for row in result.all()
        ]

    async def top_resources(
        self,
        *,
        since: date,
        until: date,
        event_type: str | None = None,
        limit: int = 10,
    ) -> list[ResourceCount]:
        """The ``limit`` most-requested ``resource_iri`` values in the period.

        ``NULL`` resource rows (e.g. LOGIN events) are excluded — they would
        otherwise dominate a "top resources" list while carrying no meaning.
        """
        w = _unified_window(since, until)
        stmt = (
            select(
                w.c.resource_iri,
                func.coalesce(func.sum(w.c.request_count), 0).label("request_count"),
                func.coalesce(func.sum(w.c.unique_visitors), 0).label("unique_visitors"),
            )
            .where(w.c.resource_iri.is_not(None))
            .group_by(w.c.resource_iri)
            .order_by(desc("request_count"))
            .limit(limit)
        )
        stmt = _filter(stmt, w, None, event_type)
        result = await self._session.execute(stmt)
        return [
            ResourceCount(
                resource_iri=row[0], request_count=int(row[1]), unique_visitors=int(row[2])
            )
            for row in result.all()
        ]

    async def geography(
        self,
        *,
        since: date,
        until: date,
        resource_iri: str | None = None,
        event_type: str | None = None,
    ) -> list[CountryCount]:
        """Per-country totals, ordered by request_count descending."""
        w = _unified_window(since, until)
        stmt = (
            select(
                w.c.country_code,
                func.coalesce(func.sum(w.c.request_count), 0).label("request_count"),
                func.coalesce(func.sum(w.c.unique_visitors), 0).label("unique_visitors"),
            )
            .group_by(w.c.country_code)
            .order_by(desc("request_count"))
        )
        stmt = _filter(stmt, w, resource_iri, event_type)
        result = await self._session.execute(stmt)
        return [
            CountryCount(
                country_code=row[0], request_count=int(row[1]), unique_visitors=int(row[2])
            )
            for row in result.all()
        ]


def _filter(
    stmt: Select[Any],
    window: Subquery,
    resource_iri: str | None,
    event_type: str | None,
) -> Select[Any]:
    """Apply the optional dimension filters to a query over the unified window."""
    if resource_iri is not None:
        stmt = stmt.where(window.c.resource_iri == resource_iri)
    if event_type is not None:
        stmt = stmt.where(window.c.event_type == event_type)
    return stmt


__all__ = [
    "CountryCount",
    "DailyPoint",
    "MetricsReader",
    "ResourceCount",
    "SummaryTotals",
]
