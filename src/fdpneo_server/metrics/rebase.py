"""One-off re-keying of recorded metrics rows onto the canonical record IRI.

Before 0.17 the request observer keyed every event by ``scheme://host/path`` as
seen by the ASGI scope. Behind a TLS-terminating proxy that uvicorn was not
told to trust, that is ``http://<host>/…`` while every record's canonical IRI
is ``https://<host>/…`` (or the PID namespace on an identifier-base
deployment) — so per-record metrics never joined to records. New events are
now recorded against the canonical IRI; this module rewrites the historic rows
so their history joins too.

Driven by ``fdp metrics rebase-resources --from <old prefix> [--to <prefix>]``.
Idempotent: a second run finds no row under the old prefix. Prefix matching is
a plain string-prefix rewrite (``old + rest`` → ``new + rest``), parameterised —
no caller value is interpolated into SQL.

* ``metrics_raw`` — an in-place ``UPDATE``.
* ``metrics_hourly`` / ``metrics_daily`` — rows are keyed by a dimension tuple
  with a unique constraint, so a re-keyed row may collide with one already
  recorded under the new prefix. Colliding rows are **merged** (counts summed;
  ``unique_visitors`` summed too, the same approximation the hourly upsert
  already makes) and the old row deleted; non-colliding rows are renamed in
  place.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import delete, func, select, update

from fdpneo_server.metrics.repository import MetricsDaily, MetricsHourly, MetricsRaw

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = ["ResourceRebaseReport", "rebase_resource_iris", "rebased_prefix"]

log = structlog.get_logger(__name__)

_COUNT_COLUMNS = (
    "request_count",
    "unique_visitors",
    "latency_ms_sum",
    "status_2xx_count",
    "status_3xx_count",
    "status_4xx_count",
    "status_5xx_count",
)


@dataclass
class ResourceRebaseReport:
    """Summary of one rebase run (counts are rows touched, or would-be under dry-run)."""

    old_prefix: str
    new_prefix: str
    dry_run: bool
    raw_rows: int = 0
    hourly_rows: int = 0
    daily_rows: int = 0

    @property
    def total(self) -> int:
        return self.raw_rows + self.hourly_rows + self.daily_rows


def rebased_prefix(value: str, old: str, new: str) -> str | None:
    """``value`` with a leading ``old`` swapped for ``new``; ``None`` if unaffected."""
    if value.startswith(old):
        return new + value[len(old) :]
    return None


async def rebase_resource_iris(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    old_prefix: str,
    new_prefix: str,
    dry_run: bool = False,
) -> ResourceRebaseReport:
    """Re-key every metrics row whose ``resource_iri`` starts with ``old_prefix``."""
    old = old_prefix.rstrip("/")
    new = new_prefix.rstrip("/")
    report = ResourceRebaseReport(old_prefix=old, new_prefix=new, dry_run=dry_run)
    if old == new:
        log.info("metrics_rebase_noop_same_prefix", prefix=old)
        return report

    async with session_factory() as session:
        report.raw_rows = await _rebase_raw(session, old, new, dry_run=dry_run)
        report.hourly_rows = await _rebase_aggregates(
            session, MetricsHourly, old, new, dry_run=dry_run
        )
        report.daily_rows = await _rebase_aggregates(
            session, MetricsDaily, old, new, dry_run=dry_run
        )
        if dry_run:
            await session.rollback()
        else:
            await session.commit()

    log.info(
        "metrics_rebase_completed",
        old_prefix=old,
        new_prefix=new,
        raw_rows=report.raw_rows,
        hourly_rows=report.hourly_rows,
        daily_rows=report.daily_rows,
        dry_run=dry_run,
    )
    return report


async def _rebase_raw(session: AsyncSession, old: str, new: str, *, dry_run: bool) -> int:
    column = MetricsRaw.resource_iri
    where = column.startswith(old, autoescape=True)
    count = await session.execute(select(func.count()).select_from(MetricsRaw).where(where))
    affected = int(count.scalar_one())
    if affected and not dry_run:
        await session.execute(
            update(MetricsRaw)
            .where(where)
            .values(resource_iri=func.concat(new, func.substr(column, len(old) + 1)))
        )
    return affected


async def _rebase_aggregates(
    session: AsyncSession,
    model: type[MetricsHourly] | type[MetricsDaily],
    old: str,
    new: str,
    *,
    dry_run: bool,
) -> int:
    """Rename or merge aggregate rows under ``old`` onto ``new`` (see module doc)."""
    rows = (
        await session.execute(
            select(model).where(model.resource_iri.startswith(old, autoescape=True))
        )
    ).scalars()
    touched = 0
    for row in rows:
        touched += 1
        if dry_run:
            continue
        target_iri = rebased_prefix(row.resource_iri or "", old, new)
        existing = (
            await session.execute(
                select(model).where(
                    model.bucket == row.bucket,
                    model.event_type == row.event_type,
                    model.resource_iri == target_iri,
                    model.country_code.is_not_distinct_from(row.country_code),
                    model.region.is_not_distinct_from(row.region),
                    model.city.is_not_distinct_from(row.city),
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            row.resource_iri = target_iri
            continue
        for name in _COUNT_COLUMNS:
            setattr(existing, name, getattr(existing, name) + getattr(row, name))
        await session.execute(delete(model).where(model.id == row.id))
    return touched
