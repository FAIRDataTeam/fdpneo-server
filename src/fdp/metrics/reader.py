"""Read-only queries that back the dashboard API (architecture §11.6).

All reads target ``metrics_daily``. The dashboard surfaces complete days
only; today's events appear once the hourly→daily rollup has processed
them (lag bounded by ``MetricsSettings.discard_hourly_after_days``).
Reading the hourly table to merge in a partial "today" bucket is a
future enhancement, not v1 scope.

The reader is a separate class from :class:`MetricsRepository` so that
dashboard handlers depend only on a read-only surface — they can't
accidentally write to the metrics tables.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import desc, func, select

from fdp.metrics.repository import MetricsDaily

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


class MetricsReader:
    """Read-only access to ``metrics_daily`` for the dashboard.

    Every method takes ``since`` / ``until`` (inclusive date bounds) and
    an optional ``resource_iri`` filter. ``event_type`` is optional on
    most endpoints; ``None`` aggregates across all event types.
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
        stmt = select(
            func.coalesce(func.sum(MetricsDaily.request_count), 0),
            func.coalesce(func.sum(MetricsDaily.unique_visitors), 0),
            func.coalesce(func.sum(MetricsDaily.latency_ms_sum), 0),
            func.coalesce(func.sum(MetricsDaily.status_2xx_count), 0),
            func.coalesce(func.sum(MetricsDaily.status_3xx_count), 0),
            func.coalesce(func.sum(MetricsDaily.status_4xx_count), 0),
            func.coalesce(func.sum(MetricsDaily.status_5xx_count), 0),
        )
        stmt = self._apply_filters(stmt, since, until, resource_iri, event_type)
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
        stmt = (
            select(
                MetricsDaily.bucket,
                func.coalesce(func.sum(MetricsDaily.request_count), 0),
                func.coalesce(func.sum(MetricsDaily.unique_visitors), 0),
            )
            .group_by(MetricsDaily.bucket)
            .order_by(MetricsDaily.bucket)
        )
        stmt = self._apply_filters(stmt, since, until, resource_iri, event_type)
        result = await self._session.execute(stmt)
        return [
            DailyPoint(
                bucket=row[0],
                request_count=int(row[1]),
                unique_visitors=int(row[2]),
            )
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

        ``NULL`` resource rows (e.g. LOGIN events) are excluded — they
        would otherwise dominate a "top resources" list while carrying
        no meaning for it.
        """
        stmt = (
            select(
                MetricsDaily.resource_iri,
                func.coalesce(func.sum(MetricsDaily.request_count), 0).label("request_count"),
                func.coalesce(func.sum(MetricsDaily.unique_visitors), 0).label("unique_visitors"),
            )
            .where(MetricsDaily.resource_iri.is_not(None))
            .group_by(MetricsDaily.resource_iri)
            .order_by(desc("request_count"))
            .limit(limit)
        )
        stmt = self._apply_filters(stmt, since, until, None, event_type)
        result = await self._session.execute(stmt)
        return [
            ResourceCount(
                resource_iri=row[0],
                request_count=int(row[1]),
                unique_visitors=int(row[2]),
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
        stmt = (
            select(
                MetricsDaily.country_code,
                func.coalesce(func.sum(MetricsDaily.request_count), 0).label("request_count"),
                func.coalesce(func.sum(MetricsDaily.unique_visitors), 0).label("unique_visitors"),
            )
            .group_by(MetricsDaily.country_code)
            .order_by(desc("request_count"))
        )
        stmt = self._apply_filters(stmt, since, until, resource_iri, event_type)
        result = await self._session.execute(stmt)
        return [
            CountryCount(
                country_code=row[0],
                request_count=int(row[1]),
                unique_visitors=int(row[2]),
            )
            for row in result.all()
        ]

    # --- internals ---------------------------------------------------------

    @staticmethod
    def _apply_filters(
        stmt: object,
        since: date,
        until: date,
        resource_iri: str | None,
        event_type: str | None,
    ) -> object:
        """Apply the standard ``WHERE`` clauses common to every reader query."""
        # ``stmt`` is typed as object because SQLAlchemy's Select type is
        # parameterized in ways that don't compose cleanly across the various
        # callers; the runtime calls below are all valid on Select instances.
        stmt = stmt.where(  # type: ignore[union-attr]
            MetricsDaily.bucket >= since,
            MetricsDaily.bucket <= until,
        )
        if resource_iri is not None:
            stmt = stmt.where(MetricsDaily.resource_iri == resource_iri)  # type: ignore[union-attr]
        if event_type is not None:
            stmt = stmt.where(MetricsDaily.event_type == event_type)  # type: ignore[union-attr]
        return stmt


__all__ = [
    "CountryCount",
    "DailyPoint",
    "MetricsReader",
    "ResourceCount",
    "SummaryTotals",
]
