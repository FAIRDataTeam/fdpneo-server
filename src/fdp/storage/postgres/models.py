"""SQLAlchemy declarative base shared by every Postgres-backed module.

Tables themselves are declared by their owning bounded contexts; this module
provides only the common :class:`Base`. The initial Alembic migration
reserves the table names (``metrics_hourly``, ``metrics_daily``,
``authz_index``, ``policy_decisions_audit``, ``job_state``,
``profile_applied``) so that subsequent migrations from each module add
columns without renegotiating naming.
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Shared SQLAlchemy declarative base for fdp Postgres tables."""


__all__ = ["Base"]
