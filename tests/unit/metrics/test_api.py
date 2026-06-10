"""Unit tests for the metrics dashboard API.

Covers:

* ``_resolve_period`` defaults, bounds checking, and the max-range cap.
* Endpoint authentication enforcement (anonymous → 401).
* Response shape for each endpoint, against a fake reader.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fdp.identity.deps import current_context
from fdp.metrics.api import (
    _DEFAULT_LOOKBACK_DAYS,
    _MAX_LOOKBACK_DAYS,
    _resolve_period,
    build_metrics_router,
)
from fdp.metrics.reader import CountryCount, DailyPoint, ResourceCount, SummaryTotals
from fdp.shared.context import RequestContext
from fdp.shared.errors import BadRequest, register_exception_handlers

# --- _resolve_period -------------------------------------------------------


def _today() -> date:
    return date(2026, 6, 1)


@pytest.mark.unit
def test_resolve_period_defaults_to_last_n_days() -> None:
    period = _resolve_period(None, None, today_provider=_today)
    assert period.until == _today()
    assert (period.until - period.since).days == _DEFAULT_LOOKBACK_DAYS


@pytest.mark.unit
def test_resolve_period_accepts_explicit_bounds() -> None:
    period = _resolve_period(date(2026, 5, 1), date(2026, 5, 7), today_provider=_today)
    assert period.since == date(2026, 5, 1)
    assert period.until == date(2026, 5, 7)


@pytest.mark.unit
def test_resolve_period_rejects_since_after_until() -> None:
    with pytest.raises(BadRequest):
        _resolve_period(date(2026, 5, 10), date(2026, 5, 1), today_provider=_today)


@pytest.mark.unit
def test_resolve_period_rejects_range_over_cap() -> None:
    with pytest.raises(BadRequest):
        _resolve_period(date(2024, 1, 1), date(2026, 1, 1), today_provider=_today)


@pytest.mark.unit
def test_resolve_period_accepts_range_at_cap() -> None:
    until = date(2026, 6, 1)
    since = date.fromordinal(until.toordinal() - _MAX_LOOKBACK_DAYS)
    period = _resolve_period(since, until, today_provider=_today)
    assert period.since == since
    assert period.until == until


# --- fake reader -----------------------------------------------------------


@dataclass
class _FakeReader:
    summary_result: SummaryTotals = field(
        default_factory=lambda: SummaryTotals(
            request_count=42,
            unique_visitors=7,
            latency_ms_avg=12.5,
            status_2xx_count=40,
            status_3xx_count=0,
            status_4xx_count=2,
            status_5xx_count=0,
        )
    )
    daily_result: list[DailyPoint] = field(
        default_factory=lambda: [
            DailyPoint(bucket=date(2026, 5, 30), request_count=10, unique_visitors=3),
            DailyPoint(bucket=date(2026, 5, 31), request_count=15, unique_visitors=5),
        ]
    )
    top_result: list[ResourceCount] = field(
        default_factory=lambda: [
            ResourceCount(
                resource_iri="https://example.org/r1",
                request_count=20,
                unique_visitors=5,
            ),
            ResourceCount(
                resource_iri="https://example.org/r2",
                request_count=10,
                unique_visitors=3,
            ),
        ]
    )
    geo_result: list[CountryCount] = field(
        default_factory=lambda: [
            CountryCount(country_code="US", request_count=30, unique_visitors=6),
            CountryCount(country_code=None, request_count=2, unique_visitors=1),
        ]
    )

    async def summary(self, **_kwargs: Any) -> SummaryTotals:
        return self.summary_result

    async def daily_series(self, **_kwargs: Any) -> list[DailyPoint]:
        return self.daily_result

    async def top_resources(self, **_kwargs: Any) -> list[ResourceCount]:
        return self.top_result

    async def geography(self, **_kwargs: Any) -> list[CountryCount]:
        return self.geo_result


# --- app builder ----------------------------------------------------------


def _ctx(*, anonymous: bool = False) -> RequestContext:
    if anonymous:
        return RequestContext.anonymous(
            trace_id="t-1",
            request_timestamp=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
        )
    return RequestContext(
        subject="https://idp.example/realms/fdp#alice",
        roles=frozenset({"steward"}),
        trace_id="t-1",
        request_timestamp=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
    )


class _UnusedSessionFactory:
    """Stand-in for the real session factory.

    The reader dep is overridden in tests so this is never invoked; the
    constructor still wants a value because the production path passes a
    real factory.
    """

    def __call__(self) -> Any:
        raise AssertionError("session factory invoked in test — reader dep not overridden")


def _build_app(
    *,
    reader: _FakeReader,
    ctx: RequestContext | None = None,
) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    router = build_metrics_router(session_factory=_UnusedSessionFactory())  # type: ignore[arg-type]
    app.include_router(router)

    async def _fake_reader_dep() -> AsyncIterator[_FakeReader]:
        yield reader

    app.dependency_overrides[router.reader_dep] = _fake_reader_dep  # type: ignore[attr-defined]
    app.dependency_overrides[current_context] = lambda: ctx or _ctx()
    return app


# --- auth enforcement -----------------------------------------------------


@pytest.mark.unit
def test_summary_rejects_anonymous() -> None:
    app = _build_app(reader=_FakeReader(), ctx=_ctx(anonymous=True))
    client = TestClient(app)
    response = client.get("/metrics/summary")
    assert response.status_code == 401
    assert response.json()["code"] == "fdp.unauthenticated"


@pytest.mark.unit
def test_daily_series_rejects_anonymous() -> None:
    app = _build_app(reader=_FakeReader(), ctx=_ctx(anonymous=True))
    client = TestClient(app)
    assert client.get("/metrics/timeseries/daily").status_code == 401


@pytest.mark.unit
def test_top_resources_rejects_anonymous() -> None:
    app = _build_app(reader=_FakeReader(), ctx=_ctx(anonymous=True))
    client = TestClient(app)
    assert client.get("/metrics/top-resources").status_code == 401


@pytest.mark.unit
def test_geography_rejects_anonymous() -> None:
    app = _build_app(reader=_FakeReader(), ctx=_ctx(anonymous=True))
    client = TestClient(app)
    assert client.get("/metrics/geography").status_code == 401


# --- response shape -------------------------------------------------------


@pytest.mark.unit
def test_summary_returns_totals_and_period() -> None:
    app = _build_app(reader=_FakeReader())
    client = TestClient(app)
    response = client.get("/metrics/summary?since=2026-05-01&until=2026-05-07")
    assert response.status_code == 200
    body = response.json()
    assert body["request_count"] == 42
    assert body["unique_visitors"] == 7
    assert body["latency_ms_avg"] == 12.5
    assert body["period"] == {"since": "2026-05-01", "until": "2026-05-07"}


@pytest.mark.unit
def test_daily_series_returns_ordered_points() -> None:
    app = _build_app(reader=_FakeReader())
    client = TestClient(app)
    response = client.get("/metrics/timeseries/daily?since=2026-05-30&until=2026-05-31")
    assert response.status_code == 200
    points = response.json()["points"]
    assert [p["bucket"] for p in points] == ["2026-05-30", "2026-05-31"]
    assert points[0]["request_count"] == 10


@pytest.mark.unit
def test_top_resources_returns_items_with_event_type_echo() -> None:
    app = _build_app(reader=_FakeReader())
    client = TestClient(app)
    response = client.get(
        "/metrics/top-resources?since=2026-05-01&until=2026-05-31&event_type=view&limit=5"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["event_type"] == "view"
    assert len(body["items"]) == 2
    assert body["items"][0]["resource_iri"] == "https://example.org/r1"


@pytest.mark.unit
def test_top_resources_limit_validation() -> None:
    app = _build_app(reader=_FakeReader())
    client = TestClient(app)
    assert (
        client.get("/metrics/top-resources?since=2026-05-01&until=2026-05-31&limit=0").status_code
        == 422
    )
    assert (
        client.get(
            "/metrics/top-resources?since=2026-05-01&until=2026-05-31&limit=9999"
        ).status_code
        == 422
    )


@pytest.mark.unit
def test_geography_preserves_null_country() -> None:
    app = _build_app(reader=_FakeReader())
    client = TestClient(app)
    response = client.get("/metrics/geography?since=2026-05-01&until=2026-05-31")
    assert response.status_code == 200
    countries = response.json()["countries"]
    assert countries[0]["country_code"] == "US"
    assert countries[1]["country_code"] is None


# --- period parsing error path -------------------------------------------


@pytest.mark.unit
def test_invalid_period_returns_bad_request() -> None:
    app = _build_app(reader=_FakeReader())
    client = TestClient(app)
    response = client.get("/metrics/summary?since=2026-05-10&until=2026-05-01")
    assert response.status_code == 400
    assert response.json()["code"] == "fdp.bad_request"
