"""SQLAlchemy declarative base shared by every Postgres-backed module.

Tables themselves are declared by their owning bounded contexts; this module
provides only the common :class:`Base`. The initial Alembic migration
reserves the table names (``metrics_hourly``, ``metrics_daily``,
``authz_index``, ``policy_decisions_audit``, ``job_state``,
``profile_applied``) so that subsequent migrations from each module add
columns without renegotiating naming.

Modules whose ORM models contribute to the schema must be imported
somewhere on the path that Alembic's ``env.py`` executes, so their
``Base.metadata`` registration runs before autogeneration / migration
checks. We import them lazily from this module to keep the dependency
graph one-directional (consumer modules import ``Base``, not the other
way around).
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Shared SQLAlchemy declarative base for fdp Postgres tables."""


def register_all_models() -> None:
    """Import every module that defines ORM models against ``Base``.

    Alembic's ``env.py`` calls this so ``Base.metadata`` is fully populated
    before migrations run; the imports are intentionally lazy to avoid
    pulling SQLAlchemy state into modules that don't need it at import
    time.
    """
    import fdp.metadata.profiles.state  # noqa: F401  # pyright: ignore[reportUnusedImport]
    import fdp.metrics.repository  # noqa: F401  # pyright: ignore[reportUnusedImport]
    import fdp.policy.cache  # noqa: F401  # pyright: ignore[reportUnusedImport]


__all__ = ["Base", "register_all_models"]
