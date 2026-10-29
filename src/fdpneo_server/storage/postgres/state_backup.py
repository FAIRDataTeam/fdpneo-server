"""Export and import of the Postgres *state* tables (ADR-0029 §2-§3).

Every table registered on :data:`~fdpneo_server.storage.postgres.models.Base`
is classified exactly once:

* **state** — travels in a full backup (settings, API keys, saved searches,
  index targets, applied profiles, subject roles, audit trail, metrics history);
* **derived** — rebuilt after a restore (authorization cache, search index,
  external-label cache);
* **excluded** — must not travel (``metrics_raw`` carries the pseudonymous
  visitor hash, ADR-0002).

``tests/unit/storage/test_state_backup_classification.py`` fails when a model's
table is missing from the classification, so a new table cannot be forgotten.

The export reads every state table inside one ``REPEATABLE READ`` transaction,
so the snapshot is consistent across tables. Rows are written as JSON Lines
using the *reflected* column types of the live database, and loaded back the
same way — the restore loads rows at the archive's own schema revision, which
may differ from the ORM models of the running code. This module is generic: it
names tables, never imports another bounded context's models.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    Interval,
    LargeBinary,
    MetaData,
    Numeric,
    Table,
    Time,
    func,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection
    from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

STATE_TABLES: Final[tuple[str, ...]] = (
    "runtime_settings",
    "api_keys",
    "search_saved_queries",
    "index_targets",
    "profile_applied",
    "subject_principal",
    "record_audit",
    "metrics_hourly",
    "metrics_daily",
    # ADR-0030: the history of requested updates and their outcomes.
    "update_request",
)
DERIVED_TABLES: Final[tuple[str, ...]] = (
    "authz_index",
    "metadata_search",
    "metadata_external_labels",
    # ADR-0028 release-check cache; the next update check rebuilds it.
    "release_check",
    # ADR-0030 agent heartbeat; the agent's next poll rewrites it.
    "updater_agent",
)
EXCLUDED_TABLES: Final[tuple[str, ...]] = ("metrics_raw",)

STATE_DIR: Final = "postgres"


@dataclass(frozen=True)
class TableDump:
    """One exported table: its file (relative to the archive root), rows, checksum."""

    file: str
    rows: int
    sha256: str


async def dump_state_tables(engine: AsyncEngine, out_dir: Path) -> dict[str, TableDump]:
    """Write every state table to ``out_dir/postgres/<table>.jsonl``.

    Tables that do not exist in this database (an older schema) are skipped.
    """
    target = out_dir / STATE_DIR
    target.mkdir(parents=True, exist_ok=True)
    dumps: dict[str, TableDump] = {}
    async with engine.connect() as base_conn:
        conn = await base_conn.execution_options(isolation_level="REPEATABLE READ")
        async with conn.begin():
            tables = await _reflect(conn, STATE_TABLES)
            for name, table in tables.items():
                order = list(table.primary_key.columns) or list(table.columns)
                result = await conn.stream(select(table).order_by(*order))
                path = target / f"{name}.jsonl"
                hasher = hashlib.sha256()
                count = 0
                with path.open("wb") as handle:
                    async for row in result.mappings():
                        line = (
                            json.dumps(
                                {key: _encode(value) for key, value in row.items()},
                                sort_keys=True,
                                ensure_ascii=False,
                            )
                            + "\n"
                        ).encode("utf-8")
                        handle.write(line)
                        hasher.update(line)
                        count += 1
                dumps[name] = TableDump(
                    file=f"{STATE_DIR}/{name}.jsonl", rows=count, sha256=hasher.hexdigest()
                )
    return dumps


async def state_row_counts(engine: AsyncEngine) -> dict[str, int]:
    """Row count per existing state table."""
    async with engine.connect() as conn:
        tables = await _reflect(conn, STATE_TABLES)
        counts: dict[str, int] = {}
        for name, table in tables.items():
            counts[name] = int(
                (await conn.execute(select(func.count()).select_from(table))).scalar_one()
            )
        return counts


async def truncate_tables(engine: AsyncEngine, names: tuple[str, ...]) -> None:
    """Empty the named tables that exist (``--overwrite`` and derived-state reset)."""
    async with engine.begin() as conn:
        tables = await _reflect(conn, names)
        for table in tables.values():
            await conn.execute(table.delete())


async def load_state_tables(
    engine: AsyncEngine, in_dir: Path, manifest_tables: dict[str, dict[str, Any]]
) -> dict[str, int]:
    """Insert the archive's table rows into the (empty) state tables.

    ``manifest_tables`` maps table name → ``{"file", "rows", "sha256"}``. Every
    file is checksum-verified before anything is written; a table the archive
    has but this schema lacks is an error (the caller migrated the schema to the
    archive's own revision first, so it cannot happen for a valid archive).
    """
    for name, entry in manifest_tables.items():
        _verify(in_dir / str(entry["file"]), str(entry["sha256"]), name)
    loaded: dict[str, int] = {}
    async with engine.begin() as conn:
        tables = await _reflect(conn, tuple(manifest_tables))
        missing = sorted(set(manifest_tables) - set(tables))
        if missing:
            raise ValueError(f"tables missing from the target schema: {', '.join(missing)}")
        for name, entry in manifest_tables.items():
            table = tables[name]
            rows = [
                {key: _decode(table, key, value) for key, value in json.loads(line).items()}
                for line in (in_dir / str(entry["file"])).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            if len(rows) != int(entry["rows"]):
                raise ValueError(f"{name}: expected {entry['rows']} rows, file has {len(rows)}")
            for start in range(0, len(rows), 1000):
                await conn.execute(table.insert(), rows[start : start + 1000])
            await _reset_sequences(conn, table)
            loaded[name] = len(rows)
    return loaded


async def _reflect(conn: AsyncConnection, names: tuple[str, ...]) -> dict[str, Table]:
    def _sync(sync_conn: Connection) -> dict[str, Table]:
        metadata = MetaData()
        existing = set(sync_conn.dialect.get_table_names(sync_conn))
        return {
            name: Table(name, metadata, autoload_with=sync_conn)
            for name in names
            if name in existing
        }

    return await conn.run_sync(_sync)


async def _reset_sequences(conn: AsyncConnection, table: Table) -> None:
    """Move serial/identity sequences past the loaded ids."""
    for column in table.primary_key.columns:
        sequence = (
            await conn.execute(
                text("SELECT pg_get_serial_sequence(:table, :column)"),
                {"table": table.name, "column": column.name},
            )
        ).scalar()
        if sequence is None:
            continue
        highest = (await conn.execute(select(func.max(column)))).scalar()
        await conn.execute(
            select(func.setval(sequence, int(highest or 0) + 1, False)),
        )


def _encode(value: object) -> object:
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes | bytearray | memoryview):
        return base64.b64encode(bytes(value)).decode("ascii")
    return value


def _decode(table: Table, key: str, value: object) -> object:
    if value is None or key not in table.columns:
        return value
    column_type = table.columns[key].type
    if isinstance(column_type, JSON | JSONB):
        return value
    if isinstance(column_type, DateTime) and isinstance(value, str):
        return datetime.fromisoformat(value)
    if isinstance(column_type, Date) and isinstance(value, str):
        return date.fromisoformat(value)
    if isinstance(column_type, Time) and isinstance(value, str):
        return time.fromisoformat(value)
    if isinstance(column_type, Interval) and isinstance(value, int | float):
        return timedelta(seconds=value)
    if isinstance(column_type, Numeric) and isinstance(value, str):
        return Decimal(value)
    if isinstance(column_type, LargeBinary) and isinstance(value, str):
        return base64.b64decode(value)
    return value


def _verify(path: Path, expected: str, name: str) -> None:
    if not path.is_file():
        raise ValueError(f"{name}: {path.name} is missing from the archive")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != expected:
        raise ValueError(f"{name}: checksum mismatch for {path.name}")


__all__ = [
    "DERIVED_TABLES",
    "EXCLUDED_TABLES",
    "STATE_DIR",
    "STATE_TABLES",
    "TableDump",
    "dump_state_tables",
    "load_state_tables",
    "state_row_counts",
    "truncate_tables",
]
