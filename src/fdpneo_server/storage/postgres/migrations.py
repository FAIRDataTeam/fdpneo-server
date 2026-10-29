"""Programmatic Alembic helpers (ADR-0029 §3).

``fdp db migrate`` upgrades to head from the command line. A full-backup restore
needs finer control: read the database's current revision, check that an
archive's revision is one this server knows, and move the schema to a specific
revision (load the rows there) before migrating to head.

``migrations/env.py`` drives Alembic through ``asyncio.run``, which cannot be
nested inside a running event loop, so upgrades and downgrades run in a worker
thread. The DSN is passed through ``Config.attributes`` rather than read from
the environment, so tests and callers with their own engine work too.
"""

from __future__ import annotations

import asyncio
from importlib.resources import files

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine


def alembic_config(dsn: str) -> Config:
    """An Alembic config for the packaged migrations, bound to ``dsn``."""
    config = Config(str(files("fdpneo_server") / "alembic.ini"))
    config.attributes["dsn"] = dsn
    return config


def revision_history() -> list[str]:
    """Known revisions, oldest first (the migration history is linear)."""
    script = ScriptDirectory.from_config(Config(str(files("fdpneo_server") / "alembic.ini")))
    return [rev.revision for rev in reversed(list(script.walk_revisions()))]


def head_revision() -> str:
    """The newest revision this server ships."""
    return revision_history()[-1]


async def current_revision(engine: AsyncEngine) -> str | None:
    """The database's Alembic revision, or ``None`` for an unmigrated database."""
    async with engine.connect() as conn:
        try:
            result = await conn.execute(text("SELECT version_num FROM alembic_version"))
        except ProgrammingError:
            return None
        row = result.first()
        return None if row is None else str(row[0])


async def migrate_to(dsn: str, *, current: str | None, target: str) -> None:
    """Upgrade or downgrade the schema from ``current`` to ``target``."""
    history = revision_history()
    if target not in history:
        raise ValueError(f"unknown Alembic revision {target!r}")
    if current is not None and current not in history:
        raise ValueError(
            f"the database is at revision {current!r}, which this server does not know "
            "(it was migrated by a newer server)"
        )
    config = alembic_config(dsn)
    if current is None or history.index(current) < history.index(target):
        await asyncio.to_thread(command.upgrade, config, target)
    elif history.index(current) > history.index(target):
        await asyncio.to_thread(command.downgrade, config, target)


__all__ = [
    "alembic_config",
    "current_revision",
    "head_revision",
    "migrate_to",
    "revision_history",
]
