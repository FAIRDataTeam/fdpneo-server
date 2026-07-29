"""Dashboard API (architecture §11.6).

Read-only endpoints that the client renders into charts. Every endpoint
requires an authenticated request — the metrics pipeline records every
request anonymously, but reading aggregated reports is gated to
authenticated subjects.

Stewards-vs-administrators scoping is the one piece §11.6 promises that
this v1 router does not yet implement: it depends on a record-ownership
lookup which the metadata module does not expose, and on the IdP
role-to-FDP-role mapping that CLAUDE.md and architecture §15 flag as a
v1.x open question. Callers may pass ``resource_iri`` to scope by a
specific record; without that filter, authenticated users see system-wide
aggregates. When the policy module gains a "resources this subject may
view" enumeration, that becomes the input to a server-side filter and
this docstring shrinks accordingly.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Annotated

import structlog
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from fdpneo_server.identity.deps import require_auth
from fdpneo_server.metrics.reader import MetricsReader
from fdpneo_server.shared.context import RequestContext
from fdpneo_server.shared.errors import BadRequest

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)


_DEFAULT_LOOKBACK_DAYS = 30
_MAX_LOOKBACK_DAYS = 365
_DEFAULT_TOP_LIMIT = 10
_MAX_TOP_LIMIT = 100


# --- response models --------------------------------------------------------


class PeriodInfo(BaseModel):
    """Resolved period bounds echoed on every response."""

    since: date
    until: date


class SummaryResponse(BaseModel):
    period: PeriodInfo
    request_count: int
    unique_visitors: int
    latency_ms_avg: float | None
    status_2xx_count: int
    status_3xx_count: int
    status_4xx_count: int
    status_5xx_count: int


class DailyPointModel(BaseModel):
    bucket: date
    request_count: int
    unique_visitors: int


class DailySeriesResponse(BaseModel):
    period: PeriodInfo
    points: list[DailyPointModel]


class TopResourceModel(BaseModel):
    resource_iri: str
    request_count: int
    unique_visitors: int


class TopResourcesResponse(BaseModel):
    period: PeriodInfo
    event_type: str | None = None
    items: list[TopResourceModel]


class CountryModel(BaseModel):
    country_code: str | None = Field(
        default=None,
        description="ISO 3166-1 alpha-2. NULL when GeoLite2 produced no answer.",
    )
    request_count: int
    unique_visitors: int


class GeographyResponse(BaseModel):
    period: PeriodInfo
    countries: list[CountryModel]


# --- period parsing ---------------------------------------------------------


def _resolve_period(
    since: date | None,
    until: date | None,
    *,
    today_provider: Callable[[], date] = lambda: datetime.now(UTC).date(),
) -> PeriodInfo:
    """Apply defaults and validate the requested date range.

    Defaults: ``until`` is today, ``since`` is 30 days earlier. The
    accepted range is at most ``_MAX_LOOKBACK_DAYS``, and ``since`` must
    not be after ``until``.
    """
    today = today_provider()
    resolved_until = until or today
    resolved_since = since or (resolved_until - timedelta(days=_DEFAULT_LOOKBACK_DAYS))
    if resolved_since > resolved_until:
        raise BadRequest(
            "`since` must be on or before `until`",
            details={"since": resolved_since.isoformat(), "until": resolved_until.isoformat()},
        )
    if (resolved_until - resolved_since).days > _MAX_LOOKBACK_DAYS:
        raise BadRequest(
            f"date range exceeds the maximum of {_MAX_LOOKBACK_DAYS} days",
            details={
                "since": resolved_since.isoformat(),
                "until": resolved_until.isoformat(),
                "max_days": _MAX_LOOKBACK_DAYS,
            },
        )
    return PeriodInfo(since=resolved_since, until=resolved_until)


# --- router -----------------------------------------------------------------


def build_metrics_router(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    prefix: str = "/metrics",
) -> APIRouter:
    """Construct the dashboard router bound to ``session_factory``.

    The reader dependency is attached as ``router.reader_dep`` so tests
    can swap it via ``app.dependency_overrides[router.reader_dep] = ...``
    without having to walk the route tree.
    """
    router = APIRouter(prefix=prefix, tags=["metrics"])

    async def reader_dep() -> AsyncIterator[MetricsReader]:
        async with session_factory() as session:
            yield MetricsReader(session)

    router.reader_dep = reader_dep  # type: ignore[attr-defined]

    @router.get("/summary", response_model=SummaryResponse, name="metrics_summary")
    async def summary(  # pyright: ignore[reportUnusedFunction]
        ctx: Annotated[RequestContext, Depends(require_auth)],
        since: Annotated[date | None, Query(description="Inclusive start date.")] = None,
        until: Annotated[date | None, Query(description="Inclusive end date.")] = None,
        resource_iri: Annotated[str | None, Query()] = None,
        event_type: Annotated[str | None, Query()] = None,
        reader: MetricsReader = Depends(reader_dep),  # noqa: B008
    ) -> SummaryResponse:
        """Period totals: request count, unique visitors, status mix, mean latency."""
        del ctx  # presence enforces auth; not used in this query
        period = _resolve_period(since, until)
        totals = await reader.summary(
            since=period.since,
            until=period.until,
            resource_iri=resource_iri,
            event_type=event_type,
        )
        return SummaryResponse(
            period=period,
            request_count=totals.request_count,
            unique_visitors=totals.unique_visitors,
            latency_ms_avg=totals.latency_ms_avg,
            status_2xx_count=totals.status_2xx_count,
            status_3xx_count=totals.status_3xx_count,
            status_4xx_count=totals.status_4xx_count,
            status_5xx_count=totals.status_5xx_count,
        )

    @router.get(
        "/timeseries/daily",
        response_model=DailySeriesResponse,
        name="metrics_daily_series",
    )
    async def daily_series(  # pyright: ignore[reportUnusedFunction]
        ctx: Annotated[RequestContext, Depends(require_auth)],
        since: Annotated[date | None, Query()] = None,
        until: Annotated[date | None, Query()] = None,
        resource_iri: Annotated[str | None, Query()] = None,
        event_type: Annotated[str | None, Query()] = None,
        reader: MetricsReader = Depends(reader_dep),  # noqa: B008
    ) -> DailySeriesResponse:
        """One ``(bucket, request_count, unique_visitors)`` point per day."""
        del ctx
        period = _resolve_period(since, until)
        points = await reader.daily_series(
            since=period.since,
            until=period.until,
            resource_iri=resource_iri,
            event_type=event_type,
        )
        return DailySeriesResponse(
            period=period,
            points=[
                DailyPointModel(
                    bucket=p.bucket,
                    request_count=p.request_count,
                    unique_visitors=p.unique_visitors,
                )
                for p in points
            ],
        )

    @router.get(
        "/top-resources",
        response_model=TopResourcesResponse,
        name="metrics_top_resources",
    )
    async def top_resources(  # pyright: ignore[reportUnusedFunction]
        ctx: Annotated[RequestContext, Depends(require_auth)],
        since: Annotated[date | None, Query()] = None,
        until: Annotated[date | None, Query()] = None,
        event_type: Annotated[str | None, Query()] = None,
        limit: Annotated[
            int,
            Query(ge=1, le=_MAX_TOP_LIMIT),
        ] = _DEFAULT_TOP_LIMIT,
        reader: MetricsReader = Depends(reader_dep),  # noqa: B008
    ) -> TopResourcesResponse:
        """The ``limit`` most-requested resources, ordered by request count."""
        del ctx
        period = _resolve_period(since, until)
        rows = await reader.top_resources(
            since=period.since,
            until=period.until,
            event_type=event_type,
            limit=limit,
        )
        return TopResourcesResponse(
            period=period,
            event_type=event_type,
            items=[
                # Reader excludes NULL resources so the cast is safe.
                TopResourceModel(
                    resource_iri=row.resource_iri or "",
                    request_count=row.request_count,
                    unique_visitors=row.unique_visitors,
                )
                for row in rows
            ],
        )

    @router.get(
        "/geography",
        response_model=GeographyResponse,
        name="metrics_geography",
    )
    async def geography(  # pyright: ignore[reportUnusedFunction]
        ctx: Annotated[RequestContext, Depends(require_auth)],
        since: Annotated[date | None, Query()] = None,
        until: Annotated[date | None, Query()] = None,
        resource_iri: Annotated[str | None, Query()] = None,
        event_type: Annotated[str | None, Query()] = None,
        reader: MetricsReader = Depends(reader_dep),  # noqa: B008
    ) -> GeographyResponse:
        """Per-country counts, descending by request count."""
        del ctx
        period = _resolve_period(since, until)
        rows = await reader.geography(
            since=period.since,
            until=period.until,
            resource_iri=resource_iri,
            event_type=event_type,
        )
        return GeographyResponse(
            period=period,
            countries=[
                CountryModel(
                    country_code=row.country_code,
                    request_count=row.request_count,
                    unique_visitors=row.unique_visitors,
                )
                for row in rows
            ],
        )

    return router


__all__ = [
    "CountryModel",
    "DailyPointModel",
    "DailySeriesResponse",
    "GeographyResponse",
    "PeriodInfo",
    "SummaryResponse",
    "TopResourceModel",
    "TopResourcesResponse",
    "build_metrics_router",
]
