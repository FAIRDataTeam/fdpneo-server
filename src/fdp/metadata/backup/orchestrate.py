"""Shared dump/restore orchestration (ADR-0016).

The full workflows — a dump packaged as a downloadable archive, and a restore
followed by audit insert, legacy→ADR-0019 migration, and search reindex — are
factored here so the CLI (``fdp backup …``) and the admin HTTP endpoints run the
*same* code path. Callers supply already-built collaborators (adapter, session
factory, settings); this module builds nothing process-global.
"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from fdp.metadata.backup.dump import DumpResult, dump_store
from fdp.metadata.backup.restore import RestoreResult, restore_audit, restore_store

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from fdp.config import Settings
    from fdp.storage.triplestore import TripleStoreAdapter


@dataclass
class RestoreOutcome:
    """A restore plus its follow-up steps, for the CLI/endpoint to report."""

    result: RestoreResult
    audit_rows: int
    profiles_provisioned: int
    records_indexed: int


def _system_default_offer(settings: Settings) -> str | None:
    """The profile's system-default offer IRI (anon-read baseline for reindex)."""
    if settings.profile.path is None:
        return None
    from fdp.metadata.profiles import load_profile, resolve_runtime_state

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
) -> RestoreOutcome:
    """Restore a dump, then (unless dry-run) insert audit, migrate, and reindex."""
    from fdp.metadata.prof_backfill import backfill_conformance
    from fdp.metadata.profiles import build_cache_from_repository
    from fdp.metadata.repository import MetadataRepository
    from fdp.metadata.search.reindex import reindex_all

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
    return RestoreOutcome(
        result=result,
        audit_rows=audit_rows,
        profiles_provisioned=profiles,
        records_indexed=indexed,
    )


async def dump_to_archive(
    adapter: TripleStoreAdapter,
    *,
    identifier_base: str,
    work_dir: Path,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    include_audit: bool = True,
) -> tuple[DumpResult, Path]:
    """Dump the store into ``work_dir`` and zip it; return (result, archive path).

    The archive is a single ``.zip`` of the dump directory (``records.nq`` +
    ``manifest.json`` + optional ``audit.jsonl``) — one download for the client.
    Zipping runs in a thread so it never blocks the event loop.
    """
    dump_dir = work_dir / "dump"
    result = await dump_store(
        adapter,
        dump_dir,
        identifier_base=identifier_base,
        session_factory=session_factory if include_audit else None,
        include_audit=include_audit,
    )
    archive_base = work_dir / "backup"
    path = await asyncio.to_thread(
        shutil.make_archive, str(archive_base), "zip", root_dir=str(dump_dir)
    )
    return result, Path(path)


async def extract_archive(archive: Path, dest: Path) -> None:
    """Unpack a dump ``.zip`` into ``dest`` (in a thread; never blocks the loop)."""
    await asyncio.to_thread(shutil.unpack_archive, str(archive), str(dest), "zip")


__all__ = ["RestoreOutcome", "dump_to_archive", "extract_archive", "orchestrate_restore"]
