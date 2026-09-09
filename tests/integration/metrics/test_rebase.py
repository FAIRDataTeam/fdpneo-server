"""Integration tests for ``fdp metrics rebase-resources`` against Postgres.

Rows recorded by a pre-0.17 server under ``http://<host>/…`` must be re-keyed
onto the canonical ``https://…`` prefix — raw rows in place, aggregate rows by
rename or, on a dimension collision, by merging into the existing row.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fdpneo_server.metrics.events import MetricEventType, MetricSample
from fdpneo_server.metrics.rebase import rebase_resource_iris, rebased_prefix
from fdpneo_server.metrics.repository import (
    DailyAggregate,
    HourlyAggregate,
    MetricsDaily,
    MetricsHourly,
    MetricsRaw,
    MetricsRepository,
)

pytestmark = pytest.mark.integration

OLD = "http://fdp.example"
NEW = "https://fdp.example"
BUCKET = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)


def _sample(iri: str) -> MetricSample:
    return MetricSample(
        timestamp_bucket=BUCKET,
        event_type=MetricEventType.VIEW,
        resource_iri=iri,
        country_code="NL",
        region=None,
        city=None,
        visitor_hash="v1",
        status_code=200,
        latency_ms=10,
    )


def _hourly(iri: str, requests: int) -> HourlyAggregate:
    return HourlyAggregate(
        bucket=BUCKET,
        event_type="view",
        resource_iri=iri,
        country_code="NL",
        region=None,
        city=None,
        request_count=requests,
        unique_visitors=1,
        latency_ms_sum=10 * requests,
        status_2xx_count=requests,
        status_3xx_count=0,
        status_4xx_count=0,
        status_5xx_count=0,
    )


def _daily(iri: str, requests: int) -> DailyAggregate:
    return DailyAggregate(
        bucket=date(2026, 9, 1),
        event_type="view",
        resource_iri=iri,
        country_code="NL",
        region=None,
        city=None,
        request_count=requests,
        unique_visitors=1,
        latency_ms_sum=10 * requests,
        status_2xx_count=requests,
        status_3xx_count=0,
        status_4xx_count=0,
        status_5xx_count=0,
    )


def test_rebased_prefix() -> None:
    assert rebased_prefix(f"{OLD}/catalog/x", OLD, NEW) == f"{NEW}/catalog/x"
    assert rebased_prefix("https://other/x", OLD, NEW) is None


async def test_rebase_rekeys_raw_and_renames_or_merges_aggregates(
    session_factory: async_sessionmaker[AsyncSession],
    repo: MetricsRepository,
    session: AsyncSession,
) -> None:
    await repo.insert_raw(_sample(f"{OLD}/catalog/x"))
    await repo.insert_raw(_sample(f"{OLD}/catalog/y"))
    await repo.insert_raw(_sample("https://elsewhere/z"))  # untouched
    # Hourly: /x has no counterpart under the new prefix (rename); /y collides
    # with an existing https row (merge: counts summed, old row gone).
    await repo.upsert_hourly(_hourly(f"{OLD}/catalog/x", 3))
    await repo.upsert_hourly(_hourly(f"{OLD}/catalog/y", 2))
    await repo.upsert_hourly(_hourly(f"{NEW}/catalog/y", 5))
    await repo.upsert_daily(_daily(f"{OLD}/catalog/y", 2))
    await repo.upsert_daily(_daily(f"{NEW}/catalog/y", 5))
    await session.commit()

    preview = await rebase_resource_iris(
        session_factory, old_prefix=OLD, new_prefix=NEW, dry_run=True
    )
    assert (preview.raw_rows, preview.hourly_rows, preview.daily_rows) == (2, 2, 1)

    report = await rebase_resource_iris(session_factory, old_prefix=OLD, new_prefix=NEW)
    assert (report.raw_rows, report.hourly_rows, report.daily_rows) == (2, 2, 1)

    async with session_factory() as check:
        raw = set((await check.execute(select(MetricsRaw.resource_iri))).scalars())
        assert raw == {f"{NEW}/catalog/x", f"{NEW}/catalog/y", "https://elsewhere/z"}

        hourly = {
            row.resource_iri: row.request_count
            for row in (await check.execute(select(MetricsHourly))).scalars()
        }
        assert hourly == {f"{NEW}/catalog/x": 3, f"{NEW}/catalog/y": 7}

        daily = {
            row.resource_iri: row.request_count
            for row in (await check.execute(select(MetricsDaily))).scalars()
        }
        assert daily == {f"{NEW}/catalog/y": 7}

    # Idempotent: nothing left under the old prefix.
    again = await rebase_resource_iris(session_factory, old_prefix=OLD, new_prefix=NEW)
    assert again.total == 0
