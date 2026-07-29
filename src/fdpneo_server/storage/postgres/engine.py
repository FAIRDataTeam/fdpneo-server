"""Async SQLAlchemy engine and session factory.

**Responsibilities**

* Build an :class:`AsyncEngine` from :class:`~fdpneo_server.config.Settings.postgres_dsn`.
* Build an :class:`async_sessionmaker` bound to that engine.

**Non-responsibilities**

* Schema definitions. Tables live in :mod:`fdpneo_server.storage.postgres.models` and
  in the consuming bounded-context modules.
* Lifecycle. The FastAPI lifespan handler creates the engine on startup,
  passes the session factory to dependents, and disposes the engine on
  shutdown. Tests build their own engine pointing at an isolated database.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from fdpneo_server.config import Settings


def build_engine(settings: Settings, *, echo: bool = False) -> AsyncEngine:
    """Construct the async engine for the configured Postgres DSN.

    The DSN must use the asyncpg driver (``postgresql+asyncpg://...``); this
    is enforced by the type of ``Settings.postgres_dsn`` plus the runtime
    sanity check below.
    """
    dsn = str(settings.postgres_dsn)
    if "+asyncpg" not in dsn:
        raise ValueError(
            "Settings.postgres_dsn must use the asyncpg driver "
            "(postgresql+asyncpg://...). Got: " + dsn
        )
    return create_async_engine(dsn, echo=echo, future=True)


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Construct an :class:`async_sessionmaker` bound to ``engine``.

    ``expire_on_commit=False`` is intentional: it keeps attribute access
    safe after the session has committed, which simplifies request-scoped
    session patterns.
    """
    return async_sessionmaker(engine, expire_on_commit=False)


__all__ = ["build_engine", "build_session_factory"]
