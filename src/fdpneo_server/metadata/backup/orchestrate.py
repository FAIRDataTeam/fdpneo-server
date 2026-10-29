"""Shared dump/restore orchestration (ADR-0016, ADR-0029).

The full workflows — a dump packaged as a downloadable archive, and a restore
followed by audit insert, legacy→ADR-0019 migration, and search reindex — are
factored here so the CLI (``fdp backup …``) and the admin HTTP endpoints run the
*same* code path. Callers supply already-built collaborators (adapter, session
factory, engine, settings); this module builds nothing process-global.

A *full* archive (ADR-0029, dump format 2) also carries the Postgres state
tables. Restoring one:

1. checks both stores' preconditions before writing anything (identifier base,
   empty triple store, empty state tables — or ``--overwrite``);
2. moves the schema to the archive's own Alembic revision, loads the rows there,
   then migrates to head, so rows always meet the schema they were written for;
3. clears the derived tables, restores the triple store, and reindexes search.

The metrics rollup that must precede a full dump lives in another bounded
context, so callers pass it in as ``before_dump`` rather than this module
importing it.
"""
# ruff: noqa: ASYNC240 - admin tooling; the few cheap pathlib calls are intentional.

from __future__ import annotations

import asyncio
import shutil
import tarfile
import zipfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fdpneo_server.metadata.backup.dump import SCOPE_FULL, DumpResult, dump_store
from fdpneo_server.metadata.backup.restore import (
    RestoreError,
    RestoreResult,
    existing_graph_count,
    read_manifest,
    restore_audit,
    restore_store,
)
from fdpneo_server.metadata.backup.seal import is_sealed, seal_file, unseal_file
from fdpneo_server.storage.postgres.migrations import (
    current_revision,
    head_revision,
    migrate_to,
    revision_history,
)
from fdpneo_server.storage.postgres.state_backup import (
    DERIVED_TABLES,
    STATE_TABLES,
    load_state_tables,
    state_row_counts,
    truncate_tables,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

    from fdpneo_server.config import Settings
    from fdpneo_server.metadata.backup.seal import KdfParams
    from fdpneo_server.storage.triplestore import TripleStoreAdapter

BeforeDump = Callable[[], Awaitable[None]]


@dataclass
class RestoreOutcome:
    """A restore plus its follow-up steps, for the CLI/endpoint to report."""

    result: RestoreResult
    audit_rows: int
    profiles_provisioned: int
    records_indexed: int
    scope: str = "store"
    state_rows: dict[str, int] = field(default_factory=dict[str, int])
    schema_revision: str | None = None


def _system_default_offer(settings: Settings) -> str | None:
    """The profile's system-default offer IRI (anon-read baseline for reindex)."""
    if settings.profile.path is None:
        return None
    from fdpneo_server.metadata.profiles import load_profile, resolve_runtime_state

    system_default, _ = resolve_runtime_state(
        load_profile(settings.profile.path), settings=settings
    )
    return system_default


async def orchestrate_restore(
    adapter: TripleStoreAdapter,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    settings: Settings,
    in_dir: Path | str,
    merge: bool = False,
    overwrite: bool = False,
    include_audit: bool = True,
    dry_run: bool = False,
    rebase: bool = False,
    engine: AsyncEngine | None = None,
    dsn: str | None = None,
    allow_schema_migration: bool = True,
) -> RestoreOutcome:
    """Restore a dump, then (unless dry-run) insert audit, migrate, and reindex.

    ``engine`` (and ``dsn`` for schema moves, default ``settings.postgres_dsn``)
    is required for a full archive. ``allow_schema_migration=False`` refuses a
    full archive whose revision differs from the database's — the HTTP path sets
    it, because the schema must not move under a running server.
    """
    manifest = read_manifest(in_dir)
    if manifest.get("scope") == SCOPE_FULL:
        if engine is None:
            raise RestoreError("a full archive needs a database engine to restore into")
        return await _restore_full(
            adapter,
            session_factory,
            engine,
            manifest,
            settings=settings,
            in_dir=Path(in_dir),
            merge=merge,
            overwrite=overwrite,
            dry_run=dry_run,
            rebase=rebase,
            dsn=dsn or str(settings.postgres_dsn),
            allow_schema_migration=allow_schema_migration,
        )

    result = await restore_store(
        adapter,
        in_dir,
        target_identifier_base=settings.resolved_identifier_base,
        merge=merge,
        overwrite=overwrite,
        dry_run=dry_run,
        rebase=rebase,
    )
    if result.dry_run:
        return RestoreOutcome(
            result=result, audit_rows=0, profiles_provisioned=0, records_indexed=0
        )

    audit_rows = await restore_audit(session_factory, in_dir) if include_audit else 0
    profiles, indexed = await _after_store_restore(adapter, session_factory, settings, result)
    return RestoreOutcome(
        result=result,
        audit_rows=audit_rows,
        profiles_provisioned=profiles,
        records_indexed=indexed,
    )


async def _restore_full(
    adapter: TripleStoreAdapter,
    session_factory: async_sessionmaker[AsyncSession],
    engine: AsyncEngine,
    manifest: dict[str, Any],
    *,
    settings: Settings,
    in_dir: Path,
    merge: bool,
    overwrite: bool,
    dry_run: bool,
    rebase: bool,
    dsn: str,
    allow_schema_migration: bool,
) -> RestoreOutcome:
    if merge:
        raise RestoreError(
            "--merge is not supported for a full archive: Postgres state cannot be merged "
            "row by row. Restore into an empty deployment, or pass --overwrite."
        )
    archive_rev = str(manifest.get("alembic_revision") or "")
    history = revision_history()
    if archive_rev not in history:
        raise RestoreError(
            f"the archive was taken at schema revision {archive_rev!r}, which this server "
            "does not know. Restore it with the same or a newer server version."
        )
    tables: dict[str, dict[str, Any]] = dict(manifest.get("postgres") or {})
    unknown = sorted(set(tables) - set(STATE_TABLES))
    if unknown:
        raise RestoreError(f"the archive carries unexpected tables: {', '.join(unknown)}")

    db_rev = await current_revision(engine)
    if db_rev is not None and db_rev not in history:
        raise RestoreError(
            f"the database is at schema revision {db_rev!r}, newer than this server knows"
        )
    if db_rev != archive_rev and not allow_schema_migration:
        raise RestoreError(
            f"the archive's schema revision ({archive_rev}) differs from this database's "
            f"({db_rev}). Restore it with `fdp backup restore` while the server is stopped."
        )
    occupied = {
        name: rows
        for name, rows in (await state_row_counts(engine) if db_rev else {}).items()
        if rows
    }
    if occupied and not overwrite:
        listing = ", ".join(f"{name} ({rows})" for name, rows in sorted(occupied.items()))
        raise RestoreError(
            f"the database already holds state: {listing}. Restore into an empty "
            "deployment, or pass --overwrite to replace it."
        )

    # Triple-store preconditions are checked before Postgres is touched: the
    # store must be empty (or --overwrite), and a dry run parses the archive and
    # raises on an identifier-base mismatch.
    graphs = await existing_graph_count(adapter)
    if graphs and not overwrite and not dry_run:
        raise RestoreError(
            f"target store is not empty ({graphs} graph(s)). Restore into an empty "
            "deployment, or pass --overwrite to replace it."
        )
    preview = await restore_store(
        adapter,
        in_dir,
        target_identifier_base=settings.resolved_identifier_base,
        overwrite=overwrite,
        dry_run=True,
        rebase=rebase,
    )
    if dry_run:
        return RestoreOutcome(
            result=preview,
            audit_rows=0,
            profiles_provisioned=0,
            records_indexed=0,
            scope=SCOPE_FULL,
            state_rows={name: int(entry.get("rows", 0)) for name, entry in tables.items()},
            schema_revision=archive_rev,
        )

    if occupied:
        await truncate_tables(engine, STATE_TABLES)
    if db_rev != archive_rev:
        await migrate_to(dsn, current=db_rev, target=archive_rev)
    loaded = await load_state_tables(engine, in_dir, tables)
    head = head_revision()
    if archive_rev != head:
        await migrate_to(dsn, current=archive_rev, target=head)
    await truncate_tables(engine, DERIVED_TABLES)

    result = await restore_store(
        adapter,
        in_dir,
        target_identifier_base=settings.resolved_identifier_base,
        overwrite=overwrite,
        rebase=rebase,
    )
    profiles, indexed = await _after_store_restore(adapter, session_factory, settings, result)
    return RestoreOutcome(
        result=result,
        audit_rows=loaded.get("record_audit", 0),
        profiles_provisioned=profiles,
        records_indexed=indexed,
        scope=SCOPE_FULL,
        state_rows=loaded,
        schema_revision=head,
    )


async def _after_store_restore(
    adapter: TripleStoreAdapter,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    result: RestoreResult,
) -> tuple[int, int]:
    """Migrate a legacy dump forward and reindex search; return (profiles, indexed)."""
    from fdpneo_server.metadata.prof_backfill import backfill_conformance
    from fdpneo_server.metadata.profiles import build_cache_from_repository
    from fdpneo_server.metadata.repository import MetadataRepository
    from fdpneo_server.metadata.search.reindex import reindex_all

    profiles = 0
    if result.needs_migration:
        repository = MetadataRepository(adapter)
        cache = await build_cache_from_repository(
            adapter, base_url=settings.resolved_identifier_base
        )
        report = await backfill_conformance(adapter=adapter, repository=repository, cache=cache)
        profiles = len(report.profiles_provisioned)
    indexed = await reindex_all(
        adapter,
        session_factory,
        language=settings.search.default_language,
        system_default_offer_iri=_system_default_offer(settings),
    )
    return profiles, indexed


async def dump_to_archive(
    adapter: TripleStoreAdapter,
    *,
    identifier_base: str,
    work_dir: Path,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    include_audit: bool = True,
    full_engine: AsyncEngine | None = None,
    passphrase: str | None = None,
    before_dump: BeforeDump | None = None,
    kdf: KdfParams | None = None,
) -> tuple[DumpResult, Path]:
    """Dump the store into ``work_dir`` and zip it; return (result, archive path).

    The archive is a single ``.zip`` of the dump directory (``records.nq`` +
    ``manifest.json`` + optional ``audit.jsonl``, plus ``postgres/`` for a full
    dump) — one download for the client. With ``passphrase`` the zip is sealed
    (ADR-0029 §5) and the returned path is the ``.fdpbk`` file. Zipping and
    sealing run in a thread so they never block the event loop.
    """
    if before_dump is not None:
        await before_dump()
    dump_dir = work_dir / "dump"
    result = await dump_store(
        adapter,
        dump_dir,
        identifier_base=identifier_base,
        session_factory=session_factory if include_audit else None,
        include_audit=include_audit,
        full_engine=full_engine,
    )
    archive_base = work_dir / "backup"
    path = Path(
        await asyncio.to_thread(
            shutil.make_archive, str(archive_base), "zip", root_dir=str(dump_dir)
        )
    )
    shutil.rmtree(dump_dir, ignore_errors=True)
    if passphrase is not None:
        sealed = work_dir / "backup.fdpbk"
        await asyncio.to_thread(seal_file, path, sealed, passphrase, kdf=kdf)
        path.unlink(missing_ok=True)
        path = sealed
    return result, path


async def open_archive(archive: Path, dest: Path, *, passphrase: str | None = None) -> Path:
    """Turn a dump directory, ``.zip`` or sealed file into an extracted directory.

    Returns the directory to restore from: ``archive`` itself when it already is
    a directory, otherwise ``dest`` after unsealing (``passphrase`` required) and
    unzipping. Runs in threads; never blocks the event loop.
    """
    if archive.is_dir():
        return archive
    dest.mkdir(parents=True, exist_ok=True)
    source = archive
    if await asyncio.to_thread(is_sealed, archive):
        if passphrase is None:
            raise RestoreError("this archive is encrypted: a passphrase is required")
        source = dest.parent / f"{dest.name}.zip"
        await asyncio.to_thread(unseal_file, archive, source, passphrase)
    if await asyncio.to_thread(tarfile.is_tarfile, source):
        raise RestoreError(
            "this is a stack bundle, not an application archive: restore it with "
            "deploy/stack/restore.sh (or unpack it and restore its app.zip)"
        )
    if not await asyncio.to_thread(zipfile.is_zipfile, source):
        raise RestoreError("not a backup archive (expected a directory, a .zip or a sealed file)")
    await extract_archive(source, dest)
    if source != archive:
        source.unlink(missing_ok=True)
    return dest


async def extract_archive(archive: Path, dest: Path) -> None:
    """Unpack a dump ``.zip`` into ``dest`` (in a thread; never blocks the loop)."""
    await asyncio.to_thread(shutil.unpack_archive, str(archive), str(dest), "zip")


__all__ = [
    "BeforeDump",
    "RestoreOutcome",
    "dump_to_archive",
    "extract_archive",
    "open_archive",
    "orchestrate_restore",
]
