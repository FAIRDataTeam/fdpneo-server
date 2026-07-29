"""Postgres adapter — async SQLAlchemy engine, session factory, and ORM base.

Public interface:

* :data:`Base` — the shared :class:`~sqlalchemy.orm.DeclarativeBase` every
  consuming module extends to define its tables.
* :func:`build_engine` / :func:`build_session_factory` — construct an async
  engine and session factory from :class:`~fdpneo_server.config.Settings`. The
  application's lifespan handler owns the engine's lifecycle; tests build
  their own engine pointed at an isolated database.
"""

from __future__ import annotations

from fdpneo_server.storage.postgres.engine import build_engine, build_session_factory
from fdpneo_server.storage.postgres.models import Base

__all__ = ["Base", "build_engine", "build_session_factory"]
