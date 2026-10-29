"""Admin-only backup/restore HTTP surface (ADR-0016 §5 amendment, v0.9.0).

Job-based, ``admin``-role-gated endpoints under ``/fdp-api/admin/backup`` so the
web client can offer an interactive backup/restore UI. They drive the same
``dump_store`` / ``restore_store`` code paths as ``fdp backup …`` — the HTTP layer
is only a role-gated trigger, so the server-stamped-provenance guarantee (ADR-0014)
is unchanged for ordinary API clients.

* ``POST /admin/backup/dump``            → 202 + job; dumps the store to an archive.
  ``?scope=full`` (ADR-0029) adds the Postgres state and *requires* a passphrase
  in the JSON body: a full archive carries credentials, so over HTTP it is always
  sealed. Writes are paused (``503`` + ``Retry-After``) while it runs.
* ``POST /admin/backup/restore``         → 202 + job; restores an uploaded archive
  (directory zip, or a sealed archive with the ``passphrase`` form field).
* ``GET  /admin/backup/jobs/{id}``       → job status (poll this).
* ``GET  /admin/backup/jobs/{id}/archive`` → download a finished dump's ``.zip``.
"""

from __future__ import annotations

import contextlib
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Final, Literal

import structlog
from fastapi import APIRouter, Body, Depends, File, Form, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from fdpneo_server.identity.deps import require_auth
from fdpneo_server.metadata.backup.dump import SCOPE_FULL
from fdpneo_server.metadata.backup.jobs import BackupJob, JobState
from fdpneo_server.metadata.backup.orchestrate import (
    dump_to_archive,
    open_archive,
    orchestrate_restore,
)
from fdpneo_server.metadata.backup.restore import read_manifest
from fdpneo_server.metadata.backup.seal import MIN_PASSPHRASE_LENGTH, is_sealed
from fdpneo_server.shared.context import RequestContext
from fdpneo_server.shared.errors import BadRequest, Conflict, Forbidden, NotFound

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

    from fdpneo_server.config import Settings
    from fdpneo_server.metadata.backup.jobs import BackupJobRegistry
    from fdpneo_server.metadata.backup.orchestrate import BeforeDump
    from fdpneo_server.shared.write_freeze import WriteFreeze
    from fdpneo_server.storage.triplestore import TripleStoreAdapter

log = structlog.get_logger(__name__)

_ADMIN_ROLE: Final = "admin"


class JobView(BaseModel):
    """A backup/restore job as the client polls it."""

    id: str
    kind: str
    state: str
    created_at: datetime
    finished_at: datetime | None = None
    result: dict[str, Any] | None = None
    error: str | None = None


class FullDumpRequest(BaseModel):
    """Body of a full dump: the passphrase that seals the archive (never stored)."""

    passphrase: str = Field(min_length=MIN_PASSPHRASE_LENGTH, max_length=1024)


def _view(job: BackupJob) -> JobView:
    return JobView(
        id=job.id,
        kind=job.kind,
        state=job.state.value,
        created_at=job.created_at,
        finished_at=job.finished_at,
        result=job.result,
        error=job.error,
    )


def build_backup_admin_router(
    *,
    registry: BackupJobRegistry,
    adapter: TripleStoreAdapter,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    prefix: str = "/admin/backup",
    engine: AsyncEngine | None = None,
    write_freeze: WriteFreeze | None = None,
    before_dump: BeforeDump | None = None,
) -> APIRouter:
    """Build the admin backup/restore router (all endpoints require the admin role).

    ``engine`` enables full dumps and restores (ADR-0029); ``write_freeze`` pauses
    writes while they run; ``before_dump`` (the metrics rollup) runs first.
    """

    def _frozen(reason: str) -> contextlib.AbstractAsyncContextManager[None]:
        if write_freeze is None:
            return contextlib.nullcontext()
        return write_freeze.hold(reason)

    router = APIRouter(prefix=prefix, tags=["admin"])

    def _require_admin(ctx: RequestContext) -> None:
        if _ADMIN_ROLE not in ctx.roles:
            raise Forbidden(
                "admin role required for backup/restore",
                details={"required_role": _ADMIN_ROLE},
            )

    @router.post("/dump", status_code=202, response_model=JobView, name="admin_backup_dump")
    async def start_dump(  # pyright: ignore[reportUnusedFunction]
        ctx: Annotated[RequestContext, Depends(require_auth)],
        no_audit: Annotated[
            bool, Query(description="Skip exporting the Postgres record_audit rows.")
        ] = False,
        scope: Annotated[
            Literal["store", "full"],
            Query(description="`full` adds the Postgres state and seals the archive."),
        ] = "store",
        body: Annotated[FullDumpRequest | None, Body()] = None,
    ) -> JobView:
        """Start a store dump (admin). Poll the returned job; download its archive when done."""
        _require_admin(ctx)
        full = scope == SCOPE_FULL
        if full and engine is None:
            raise BadRequest("full backups are not available on this server")
        if full and body is None:
            raise BadRequest(
                "a full backup carries credentials and is always encrypted: "
                'send {"passphrase": "…"} in the request body',
                details={"min_passphrase_length": MIN_PASSPHRASE_LENGTH},
            )
        passphrase = body.passphrase if (full and body is not None) else None
        job = registry.create("dump")

        async def runner(j: BackupJob) -> None:
            j.work_dir = Path(tempfile.mkdtemp(prefix="fdp-dump-"))
            async with _frozen("a full backup") if full else contextlib.nullcontext():
                result, archive = await dump_to_archive(
                    adapter,
                    identifier_base=settings.resolved_identifier_base,
                    work_dir=j.work_dir,
                    session_factory=session_factory,
                    include_audit=not no_audit,
                    full_engine=engine if full else None,
                    passphrase=passphrase,
                    before_dump=before_dump if full else None,
                )
            j.artifact_path = archive
            j.result = {
                "scope": result.scope,
                "graphs": result.graph_count,
                "quads": result.quad_count,
                "audit_rows": result.audit_rows,
                "data_model_version": result.data_model_version,
                "state_rows": result.state_rows,
                "encrypted": passphrase is not None,
                "archive": archive.name,
            }

        registry.launch(job, runner)
        log.info("admin_backup_dump_started", job_id=job.id, subject=ctx.subject, scope=scope)
        return _view(job)

    @router.post("/restore", status_code=202, response_model=JobView, name="admin_backup_restore")
    async def start_restore(  # pyright: ignore[reportUnusedFunction]
        ctx: Annotated[RequestContext, Depends(require_auth)],
        archive: Annotated[UploadFile, File(description="A dump archive (.zip) to restore.")],
        merge: Annotated[
            bool, Query(description="Into a non-empty store: skip existing graphs.")
        ] = False,
        overwrite: Annotated[
            bool, Query(description="Into a non-empty store: replace existing graphs.")
        ] = False,
        no_audit: Annotated[bool, Query(description="Skip inserting audit.jsonl rows.")] = False,
        dry_run: Annotated[
            bool, Query(description="Report what would change; write nothing.")
        ] = False,
        passphrase: Annotated[
            str | None, Form(description="Passphrase of an encrypted (sealed) archive.")
        ] = None,
    ) -> JobView:
        """Start a faithful restore from an uploaded dump archive (admin)."""
        _require_admin(ctx)
        if merge and overwrite:
            raise BadRequest("merge and overwrite are mutually exclusive")

        # Save the upload now (bounded by the global body-size limit) so the 202
        # returns only once the archive is safely on disk; the job does the work.
        work_dir = Path(tempfile.mkdtemp(prefix="fdp-restore-"))
        saved = work_dir / "upload.bin"
        saved.write_bytes(await archive.read())
        if passphrase is None and is_sealed(saved):
            shutil.rmtree(work_dir, ignore_errors=True)
            raise BadRequest(
                "this archive is encrypted: send its passphrase in the `passphrase` form field"
            )

        job = registry.create("restore")
        job.work_dir = work_dir

        async def runner(j: BackupJob) -> None:
            extracted = await open_archive(saved, work_dir / "extracted", passphrase=passphrase)
            full = read_manifest(extracted).get("scope") == SCOPE_FULL
            async with _frozen("a full restore") if full else contextlib.nullcontext():
                outcome = await orchestrate_restore(
                    adapter,
                    session_factory,
                    settings=settings,
                    in_dir=extracted,
                    merge=merge,
                    overwrite=overwrite,
                    include_audit=not no_audit,
                    dry_run=dry_run,
                    engine=engine,
                    allow_schema_migration=False,
                )
            j.result = {
                "scope": outcome.scope,
                "graphs_loaded": outcome.result.graphs_loaded,
                "graphs_skipped": outcome.result.graphs_skipped,
                "quads": outcome.result.quad_count,
                "dry_run": outcome.result.dry_run,
                "migrated": outcome.result.needs_migration,
                "profiles_provisioned": outcome.profiles_provisioned,
                "audit_rows": outcome.audit_rows,
                "records_indexed": outcome.records_indexed,
                "state_rows": outcome.state_rows,
            }

        registry.launch(job, runner)
        log.info("admin_backup_restore_started", job_id=job.id, subject=ctx.subject)
        return _view(job)

    @router.get("/jobs/{job_id}", response_model=JobView, name="admin_backup_job")
    async def job_status(  # pyright: ignore[reportUnusedFunction]
        job_id: str,
        ctx: Annotated[RequestContext, Depends(require_auth)],
    ) -> JobView:
        """Poll a backup/restore job's status (admin)."""
        _require_admin(ctx)
        job = registry.get(job_id)
        if job is None:
            raise NotFound(f"no such job: {job_id}")
        return _view(job)

    @router.get("/jobs/{job_id}/archive", name="admin_backup_download")
    async def download_archive(  # pyright: ignore[reportUnusedFunction]
        job_id: str,
        ctx: Annotated[RequestContext, Depends(require_auth)],
    ) -> FileResponse:
        """Download a finished dump job's archive (admin)."""
        _require_admin(ctx)
        job = registry.get(job_id)
        if job is None or job.kind != "dump":
            raise NotFound(f"no such dump job: {job_id}")
        if job.state is not JobState.SUCCEEDED or job.artifact_path is None:
            raise Conflict(
                f"dump job {job_id} is not ready (state {job.state.value})",
                details={"state": job.state.value},
            )
        sealed = job.artifact_path.suffix == ".fdpbk"
        return FileResponse(
            job.artifact_path,
            media_type="application/octet-stream" if sealed else "application/zip",
            filename=f"fdp-backup-{job_id}{'.fdpbk' if sealed else '.zip'}",
        )

    return router


__all__ = ["FullDumpRequest", "JobView", "build_backup_admin_router"]
