"""Shared SQLAlchemy column types for the Postgres layer."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.types import DateTime, TypeDecorator


class AwareDateTime(TypeDecorator[datetime]):
    """``DateTime(timezone=True)`` that coerces naive results back to UTC.

    Postgres stores tz-aware timestamps; SQLite (used in unit tests) returns
    naive datetimes for the same DDL. This decorator keeps a stable, tz-aware
    shape across both backends so comparisons like ``expires_at > now`` never
    hit a naive-vs-aware ``TypeError``.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        del dialect
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


__all__ = ["AwareDateTime"]
