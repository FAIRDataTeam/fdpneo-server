"""Persistence for anonymized metric samples and their aggregates.

Three tables, each owned by this module (architecture §11.5, ADR-0002):

* ``metrics_raw`` — one row per anonymized :class:`MetricSample`. Short
  retention: rows are dropped once the hourly rollup has consumed them.
* ``metrics_hourly`` — counts and latency sums grouped by hour and
  dimensions (event type, resource, country, region, city). Retains 48 h.
* ``metrics_daily`` — same dimensions rolled up to a calendar day.

Two structural privacy invariants:

* No identifying field (IP, UA, subject, query text) has a column here.
  :class:`fdp.metrics.events.MetricSample` is the only shape consumed.
* ``visitor_hash`` lives only on :class:`MetricsRaw`. Hourly and daily
  rows carry a count of distinct hashes, not the hashes themselves.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
    case,
    delete,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Mapped, mapped_column

from fdp.metrics.events import MetricSample
from fdp.storage.postgres.models import Base

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


# --- aggregate value types --------------------------------------------------


@dataclass(frozen=True)
class HourlyAggregate:
    """One grouped row produced by raw → hourly aggregation."""

    bucket: datetime
    event_type: str
    resource_iri: str | None
    country_code: str | None
    region: str | None
    city: str | None
    request_count: int
    unique_visitors: int
    latency_ms_sum: int
    status_2xx_count: int
    status_3xx_count: int
    status_4xx_count: int
    status_5xx_count: int


@dataclass(frozen=True)
class DailyAggregate:
    """One grouped row produced by hourly → daily aggregation."""

    bucket: date
    event_type: str
    resource_iri: str | None
    country_code: str | None
    region: str | None
    city: str | None
    request_count: int
    unique_visitors: int
    latency_ms_sum: int
    status_2xx_count: int
    status_3xx_count: int
    status_4xx_count: int
    status_5xx_count: int


# --- ORM models -------------------------------------------------------------


class MetricsRaw(Base):
    """One anonymized request observation. Short retention."""

    __tablename__ = "metrics_raw"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    bucket: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_iri: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    country_code: Mapped[str | None] = mapped_column(String(2), nullable=True)
    region: Mapped[str | None] = mapped_column(String(128), nullable=True)
    city: Mapped[str | None] = mapped_column(String(128), nullable=True)
    visitor_hash: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )

    __table_args__ = (
        Index("ix_metrics_raw_bucket", "bucket"),
        Index("ix_metrics_raw_recorded_at", "recorded_at"),
    )


class MetricsHourly(Base):
    """Hour-bucketed aggregates grouped by dimensions."""

    __tablename__ = "metrics_hourly"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    bucket: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_iri: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    country_code: Mapped[str | None] = mapped_column(String(2), nullable=True)
    region: Mapped[str | None] = mapped_column(String(128), nullable=True)
    city: Mapped[str | None] = mapped_column(String(128), nullable=True)
    request_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    unique_visitors: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    latency_ms_sum: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    status_2xx_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    status_3xx_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    status_4xx_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    status_5xx_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint(
            "bucket",
            "event_type",
            "resource_iri",
            "country_code",
            "region",
            "city",
            name="uq_metrics_hourly_dimensions",
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_metrics_hourly_bucket", "bucket"),
    )


class MetricsDaily(Base):
    """Day-bucketed aggregates grouped by dimensions."""

    __tablename__ = "metrics_daily"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    bucket: Mapped[date] = mapped_column(Date, nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_iri: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    country_code: Mapped[str | None] = mapped_column(String(2), nullable=True)
    region: Mapped[str | None] = mapped_column(String(128), nullable=True)
    city: Mapped[str | None] = mapped_column(String(128), nullable=True)
    request_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    unique_visitors: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    latency_ms_sum: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    status_2xx_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    status_3xx_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    status_4xx_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    status_5xx_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint(
            "bucket",
            "event_type",
            "resource_iri",
            "country_code",
            "region",
            "city",
            name="uq_metrics_daily_dimensions",
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_metrics_daily_bucket", "bucket"),
    )


# --- repository -------------------------------------------------------------


class MetricsRepository:
    """Async repository over the three metrics tables.

    Sessions are passed in; the caller controls transaction scope. Methods
    that mutate state flush within the current transaction but do not
    commit; the caller commits.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- raw events --------------------------------------------------------

    async def insert_raw(self, sample: MetricSample) -> None:
        """Persist one anonymized sample to ``metrics_raw``."""
        row = MetricsRaw(
            bucket=sample.timestamp_bucket,
            event_type=sample.event_type.value,
            resource_iri=sample.resource_iri,
            country_code=sample.country_code,
            region=sample.region,
            city=sample.city,
            visitor_hash=sample.visitor_hash,
            status_code=sample.status_code,
            latency_ms=sample.latency_ms,
        )
        self._session.add(row)
        await self._session.flush()

    async def count_raw(self) -> int:
        """Total rows in ``metrics_raw`` (test helper)."""
        stmt = select(func.count()).select_from(MetricsRaw)
        result = await self._session.execute(stmt)
        return int(result.scalar_one())

    async def list_raw_buckets_through(self, *, through: datetime) -> list[datetime]:
        """Return distinct ``bucket`` values whose ``bucket <= through``."""
        stmt = (
            select(MetricsRaw.bucket)
            .where(MetricsRaw.bucket <= through)
            .distinct()
            .order_by(MetricsRaw.bucket)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def aggregate_raw_for_bucket(self, bucket: datetime) -> Sequence[HourlyAggregate]:
        """Group raw rows with ``bucket == bucket`` by dimensions.

        Returns one :class:`HourlyAggregate` per dimension tuple, with
        ``request_count``, distinct ``unique_visitors`` (ignoring NULL
        hashes), ``latency_ms_sum``, and the four status-class counts.
        """
        status_2xx = func.coalesce(
            func.sum(
                case(
                    (
                        (MetricsRaw.status_code >= 200) & (MetricsRaw.status_code < 300),
                        1,
                    ),
                    else_=0,
                )
            ),
            0,
        )
        status_3xx = func.coalesce(
            func.sum(
                case(
                    (
                        (MetricsRaw.status_code >= 300) & (MetricsRaw.status_code < 400),
                        1,
                    ),
                    else_=0,
                )
            ),
            0,
        )
        status_4xx = func.coalesce(
            func.sum(
                case(
                    (
                        (MetricsRaw.status_code >= 400) & (MetricsRaw.status_code < 500),
                        1,
                    ),
                    else_=0,
                )
            ),
            0,
        )
        status_5xx = func.coalesce(
            func.sum(
                case((MetricsRaw.status_code >= 500, 1), else_=0),
            ),
            0,
        )
        stmt = (
            select(
                MetricsRaw.event_type,
                MetricsRaw.resource_iri,
                MetricsRaw.country_code,
                MetricsRaw.region,
                MetricsRaw.city,
                func.count().label("request_count"),
                func.count(func.distinct(MetricsRaw.visitor_hash)).label("unique_visitors"),
                func.coalesce(func.sum(MetricsRaw.latency_ms), 0).label("latency_ms_sum"),
                status_2xx.label("status_2xx_count"),
                status_3xx.label("status_3xx_count"),
                status_4xx.label("status_4xx_count"),
                status_5xx.label("status_5xx_count"),
            )
            .where(MetricsRaw.bucket == bucket)
            .group_by(
                MetricsRaw.event_type,
                MetricsRaw.resource_iri,
                MetricsRaw.country_code,
                MetricsRaw.region,
                MetricsRaw.city,
            )
        )
        result = await self._session.execute(stmt)
        return [
            HourlyAggregate(
                bucket=bucket,
                event_type=row.event_type,
                resource_iri=row.resource_iri,
                country_code=row.country_code,
                region=row.region,
                city=row.city,
                request_count=int(row.request_count),
                unique_visitors=int(row.unique_visitors),
                latency_ms_sum=int(row.latency_ms_sum),
                status_2xx_count=int(row.status_2xx_count),
                status_3xx_count=int(row.status_3xx_count),
                status_4xx_count=int(row.status_4xx_count),
                status_5xx_count=int(row.status_5xx_count),
            )
            for row in result.all()
        ]

    async def delete_raw_through(self, *, through: datetime) -> int:
        """Drop ``metrics_raw`` rows whose ``bucket <= through``.

        Returns the row count removed.
        """
        stmt = delete(MetricsRaw).where(MetricsRaw.bucket <= through)
        return await self._execute_delete(stmt)

    # --- hourly aggregates -------------------------------------------------

    async def upsert_hourly(self, aggregate: HourlyAggregate) -> None:
        """Insert-or-increment one hourly aggregate row.

        On dimension-key conflict the counts add to the existing row,
        which keeps idempotency for partial reruns of a rollup that
        already wrote some rows.
        """
        stmt = pg_insert(MetricsHourly).values(
            bucket=aggregate.bucket,
            event_type=aggregate.event_type,
            resource_iri=aggregate.resource_iri,
            country_code=aggregate.country_code,
            region=aggregate.region,
            city=aggregate.city,
            request_count=aggregate.request_count,
            unique_visitors=aggregate.unique_visitors,
            latency_ms_sum=aggregate.latency_ms_sum,
            status_2xx_count=aggregate.status_2xx_count,
            status_3xx_count=aggregate.status_3xx_count,
            status_4xx_count=aggregate.status_4xx_count,
            status_5xx_count=aggregate.status_5xx_count,
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_metrics_hourly_dimensions",
            set_={
                "request_count": MetricsHourly.request_count + stmt.excluded.request_count,
                "unique_visitors": MetricsHourly.unique_visitors + stmt.excluded.unique_visitors,
                "latency_ms_sum": MetricsHourly.latency_ms_sum + stmt.excluded.latency_ms_sum,
                "status_2xx_count": MetricsHourly.status_2xx_count + stmt.excluded.status_2xx_count,
                "status_3xx_count": MetricsHourly.status_3xx_count + stmt.excluded.status_3xx_count,
                "status_4xx_count": MetricsHourly.status_4xx_count + stmt.excluded.status_4xx_count,
                "status_5xx_count": MetricsHourly.status_5xx_count + stmt.excluded.status_5xx_count,
            },
        )
        await self._session.execute(stmt)

    async def list_hourly_buckets_before(self, *, before: datetime) -> list[datetime]:
        """Distinct ``bucket`` values where ``bucket < before`` (ordered)."""
        stmt = (
            select(MetricsHourly.bucket)
            .where(MetricsHourly.bucket < before)
            .distinct()
            .order_by(MetricsHourly.bucket)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def aggregate_hourly_for_day(self, day: date) -> Sequence[DailyAggregate]:
        """Group hourly rows belonging to ``day`` by dimensions.

        Daily ``unique_visitors`` is the SUM of hourly counts — an
        intentional approximation. The exact distinct count would require
        keeping raw visitor hashes for 24 h, which conflicts with
        ADR-0002's "discard raw within minutes" retention. A visitor
        active in two different hours therefore counts twice at day
        granularity; the dashboard surfaces this caveat.
        """
        day_start = datetime.combine(day, time(0, 0, 0), tzinfo=UTC)
        day_end = datetime.combine(day, time(23, 0, 0), tzinfo=UTC)
        stmt = (
            select(
                MetricsHourly.event_type,
                MetricsHourly.resource_iri,
                MetricsHourly.country_code,
                MetricsHourly.region,
                MetricsHourly.city,
                func.coalesce(func.sum(MetricsHourly.request_count), 0).label("request_count"),
                func.coalesce(func.sum(MetricsHourly.unique_visitors), 0).label("unique_visitors"),
                func.coalesce(func.sum(MetricsHourly.latency_ms_sum), 0).label("latency_ms_sum"),
                func.coalesce(func.sum(MetricsHourly.status_2xx_count), 0).label(
                    "status_2xx_count"
                ),
                func.coalesce(func.sum(MetricsHourly.status_3xx_count), 0).label(
                    "status_3xx_count"
                ),
                func.coalesce(func.sum(MetricsHourly.status_4xx_count), 0).label(
                    "status_4xx_count"
                ),
                func.coalesce(func.sum(MetricsHourly.status_5xx_count), 0).label(
                    "status_5xx_count"
                ),
            )
            .where(MetricsHourly.bucket >= day_start, MetricsHourly.bucket <= day_end)
            .group_by(
                MetricsHourly.event_type,
                MetricsHourly.resource_iri,
                MetricsHourly.country_code,
                MetricsHourly.region,
                MetricsHourly.city,
            )
        )
        result = await self._session.execute(stmt)
        return [
            DailyAggregate(
                bucket=day,
                event_type=row.event_type,
                resource_iri=row.resource_iri,
                country_code=row.country_code,
                region=row.region,
                city=row.city,
                request_count=int(row.request_count),
                unique_visitors=int(row.unique_visitors),
                latency_ms_sum=int(row.latency_ms_sum),
                status_2xx_count=int(row.status_2xx_count),
                status_3xx_count=int(row.status_3xx_count),
                status_4xx_count=int(row.status_4xx_count),
                status_5xx_count=int(row.status_5xx_count),
            )
            for row in result.all()
        ]

    async def delete_hourly_before(self, *, before: datetime) -> int:
        """Drop ``metrics_hourly`` rows whose ``bucket < before``."""
        stmt = delete(MetricsHourly).where(MetricsHourly.bucket < before)
        return await self._execute_delete(stmt)

    # --- daily aggregates --------------------------------------------------

    async def upsert_daily(self, aggregate: DailyAggregate) -> None:
        """Insert-or-replace one daily aggregate row.

        Replaces rather than increments: the daily rollup is recomputed
        from the full hourly window for the day, so a rerun should not
        double-count.
        """
        stmt = pg_insert(MetricsDaily).values(
            bucket=aggregate.bucket,
            event_type=aggregate.event_type,
            resource_iri=aggregate.resource_iri,
            country_code=aggregate.country_code,
            region=aggregate.region,
            city=aggregate.city,
            request_count=aggregate.request_count,
            unique_visitors=aggregate.unique_visitors,
            latency_ms_sum=aggregate.latency_ms_sum,
            status_2xx_count=aggregate.status_2xx_count,
            status_3xx_count=aggregate.status_3xx_count,
            status_4xx_count=aggregate.status_4xx_count,
            status_5xx_count=aggregate.status_5xx_count,
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_metrics_daily_dimensions",
            set_={
                "request_count": stmt.excluded.request_count,
                "unique_visitors": stmt.excluded.unique_visitors,
                "latency_ms_sum": stmt.excluded.latency_ms_sum,
                "status_2xx_count": stmt.excluded.status_2xx_count,
                "status_3xx_count": stmt.excluded.status_3xx_count,
                "status_4xx_count": stmt.excluded.status_4xx_count,
                "status_5xx_count": stmt.excluded.status_5xx_count,
            },
        )
        await self._session.execute(stmt)

    async def count_daily(self) -> int:
        """Total rows in ``metrics_daily`` (test helper)."""
        stmt = select(func.count()).select_from(MetricsDaily)
        result = await self._session.execute(stmt)
        return int(result.scalar_one())

    async def count_hourly(self) -> int:
        """Total rows in ``metrics_hourly`` (test helper)."""
        stmt = select(func.count()).select_from(MetricsHourly)
        result = await self._session.execute(stmt)
        return int(result.scalar_one())

    # --- internals ---------------------------------------------------------

    async def _execute_delete(self, stmt: Any) -> int:
        """Execute a DELETE and return the affected row count.

        ``Result.rowcount`` is documented for cursor-style results but
        not present on the generic ``Result`` stubs; the cast keeps the
        call concise and pyright-clean.
        """
        result = await self._session.execute(stmt)
        return cast("int | None", getattr(result, "rowcount", None)) or 0


__all__ = [
    "DailyAggregate",
    "HourlyAggregate",
    "MetricsDaily",
    "MetricsHourly",
    "MetricsRaw",
    "MetricsRepository",
]
