"""In-process job registry for the admin backup/restore endpoints (ADR-0016 §5 amendment).

Dump/restore are long-running, so the HTTP endpoints start a background job and
return ``202`` + a job id; the client polls the job's status. Jobs run as asyncio
tasks in the serving process and their status is kept in memory — sufficient for
the single-worker deployment this ships for; a persistent job store is a later
scaling step (multi-worker/multi-host).

Each job owns a temp working directory (the dump archive, or the uploaded+extracted
restore archive); it is cleaned up when the job is evicted or on shutdown.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

log = structlog.get_logger(__name__)


class JobState(StrEnum):
    """Lifecycle of a backup/restore job."""

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


@dataclass
class BackupJob:
    """One dump or restore job. ``result`` is a JSON-safe summary for the client."""

    id: str
    kind: str  # "dump" | "restore"
    state: JobState = JobState.QUEUED
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    result: dict[str, object] | None = None
    error: str | None = None
    work_dir: Path | None = None
    artifact_path: Path | None = None  # the dump .zip, once produced

    @property
    def done(self) -> bool:
        return self.state in (JobState.SUCCEEDED, JobState.FAILED)


class BackupJobRegistry:
    """Creates, runs, and tracks backup jobs; bounds retention and cleans up temps."""

    def __init__(self, *, max_jobs: int = 50) -> None:
        self._jobs: dict[str, BackupJob] = {}
        self._order: deque[str] = deque()
        self._tasks: set[asyncio.Task[None]] = set()
        self._max_jobs = max_jobs

    def create(self, kind: str) -> BackupJob:
        """Register a new QUEUED job, evicting the oldest when over capacity."""
        job = BackupJob(id=uuid.uuid4().hex, kind=kind)
        self._jobs[job.id] = job
        self._order.append(job.id)
        while len(self._order) > self._max_jobs:
            self._evict(self._order.popleft())
        return job

    def get(self, job_id: str) -> BackupJob | None:
        return self._jobs.get(job_id)

    def launch(self, job: BackupJob, runner: Callable[[BackupJob], Awaitable[None]]) -> None:
        """Run ``runner(job)`` in the background, tracking state + capturing errors."""
        task = asyncio.create_task(self._run(job, runner), name=f"backup-{job.kind}-{job.id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, job: BackupJob, runner: Callable[[BackupJob], Awaitable[None]]) -> None:
        job.state = JobState.RUNNING
        try:
            await runner(job)
        except Exception as err:  # a failed job is reported, never crashes the server
            job.state = JobState.FAILED
            job.error = repr(err)
            log.warning("backup_job_failed", job_id=job.id, kind=job.kind, error=repr(err))
        else:
            job.state = JobState.SUCCEEDED
            log.info("backup_job_succeeded", job_id=job.id, kind=job.kind)
        finally:
            job.finished_at = datetime.now(UTC)

    def _evict(self, job_id: str) -> None:
        job = self._jobs.pop(job_id, None)
        if job is not None and job.work_dir is not None:
            shutil.rmtree(job.work_dir, ignore_errors=True)

    async def shutdown(self) -> None:
        """Cancel running jobs and remove every job's temp directory."""
        for task in list(self._tasks):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        for job_id in list(self._order):
            self._evict(job_id)
        self._order.clear()


__all__ = ["BackupJob", "BackupJobRegistry", "JobState"]
