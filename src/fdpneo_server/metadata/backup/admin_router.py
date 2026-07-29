"""Admin-only backup/restore HTTP surface (ADR-0016 §5 amendment, v0.9.0).

Job-based, ``admin``-role-gated endpoints under ``/fdp-api/admin/backup`` so the
web client can offer an interactive backup/restore UI. They drive the same
``dump_store`` / ``restore_store`` code paths as ``fdp backup …`` — the HTTP layer
is only a role-gated trigger, so the server-stamped-provenance guarantee (ADR-0014)
is unchanged for ordinary API clients.

* ``POST /admin/backup/dump``            → 202 + job; dumps the store to an archive.
* ``POST /admin/backup/restore``         → 202 + job; restores an uploaded archive.
* ``GET  /admin/backup/jobs/{id}``       → job status (poll this).
* ``GET  /admin/backup/jobs/{id}/archive`` → download a finished dump's ``.zip``.
"""

from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Final

import structlog
from fastapi import APIRouter, Depends, File, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from fdpneo_server.identity.deps import require_auth
from fdpneo_server.metadata.backup.jobs import BackupJob, JobState
from fdpneo_server.metadata.backup.orchestrate import (
    dump_to_archive,
    extract_archive,
    orchestrate_restore,
)
from fdpneo_server.shared.context import RequestContext
from fdpneo_server.shared.errors import BadRequest, Conflict, Forbidden, NotFound

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from fdpneo_server.config import Settings
    from fdpneo_server.metadata.backup.jobs import BackupJobRegistry
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
) -> APIRouter:
    """Build the admin backup/restore router (all endpoints require the admin role)."""
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
    ) -> JobView:
        """Start a store dump (admin). Poll the returned job; download its archive when done."""
        _require_admin(ctx)
        job = registry.create("dump")

        async def runner(j: BackupJob) -> None:
            j.work_dir = Path(tempfile.mkdtemp(prefix="fdp-dump-"))
            result, archive = await dump_to_archive(
                adapter,
                identifier_base=settings.resolved_identifier_base,
                work_dir=j.work_dir,
                session_factory=session_factory,
                include_audit=not no_audit,
            )
            j.artifact_path = archive
            j.result = {
                "graphs": result.graph_count,
                "quads": result.quad_count,
                "audit_rows": result.audit_rows,
                "data_model_version": result.data_model_version,
                "archive": archive.name,
            }

        registry.launch(job, runner)
        log.info("admin_backup_dump_started", job_id=job.id, subject=ctx.subject)
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
    ) -> JobView:
        """Start a faithful restore from an uploaded dump archive (admin)."""
        _require_admin(ctx)
        if merge and overwrite:
            raise BadRequest("merge and overwrite are mutually exclusive")

        # Save the upload now (bounded by the global body-size limit) so the 202
        # returns only once the archive is safely on disk; the job does the work.
        work_dir = Path(tempfile.mkdtemp(prefix="fdp-restore-"))
        saved = work_dir / "upload.zip"
        saved.write_bytes(await archive.read())

        job = registry.create("restore")
        job.work_dir = work_dir

        async def runner(j: BackupJob) -> None:
            extracted = work_dir / "extracted"
            await extract_archive(saved, extracted)
            outcome = await orchestrate_restore(
                adapter,
                session_factory,
                settings=settings,
                in_dir=extracted,
                merge=merge,
                overwrite=overwrite,
                include_audit=not no_audit,
                dry_run=dry_run,
            )
            j.result = {
                "graphs_loaded": outcome.result.graphs_loaded,
                "graphs_skipped": outcome.result.graphs_skipped,
                "quads": outcome.result.quad_count,
                "dry_run": outcome.result.dry_run,
                "migrated": outcome.result.needs_migration,
                "profiles_provisioned": outcome.profiles_provisioned,
                "audit_rows": outcome.audit_rows,
                "records_indexed": outcome.records_indexed,
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
        return FileResponse(
            job.artifact_path,
            media_type="application/zip",
            filename=f"fdp-backup-{job_id}.zip",
        )

    return router


__all__ = ["JobView", "build_backup_admin_router"]
