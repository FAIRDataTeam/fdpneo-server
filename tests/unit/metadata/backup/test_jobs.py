"""Unit tests for the in-process backup job registry."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from fdp.metadata.backup.jobs import BackupJob, BackupJobRegistry, JobState

pytestmark = pytest.mark.unit


async def test_run_records_success_and_result() -> None:
    registry = BackupJobRegistry()
    job = registry.create("dump")
    assert job.state is JobState.QUEUED

    async def runner(j: BackupJob) -> None:
        j.result = {"graphs": 3}

    await registry._run(job, runner)
    assert job.state is JobState.SUCCEEDED
    assert job.result == {"graphs": 3}
    assert job.finished_at is not None


async def test_run_captures_failure_without_raising() -> None:
    registry = BackupJobRegistry()
    job = registry.create("restore")

    async def runner(j: BackupJob) -> None:
        raise RuntimeError("boom")

    await registry._run(job, runner)  # must not raise
    assert job.state is JobState.FAILED
    assert job.error is not None and "boom" in job.error


async def test_launch_runs_in_background() -> None:
    registry = BackupJobRegistry()
    job = registry.create("dump")
    ran = asyncio.Event()

    async def runner(j: BackupJob) -> None:
        ran.set()

    registry.launch(job, runner)
    await asyncio.wait_for(ran.wait(), timeout=2)
    for _ in range(100):
        if job.done:
            break
        await asyncio.sleep(0.01)
    assert job.state is JobState.SUCCEEDED


async def test_eviction_cleans_up_work_dir(tmp_path: Path) -> None:
    registry = BackupJobRegistry(max_jobs=1)
    first = registry.create("dump")
    first.work_dir = tmp_path / "first"
    first.work_dir.mkdir()
    registry.create("dump")  # over capacity → evicts `first`
    assert registry.get(first.id) is None
    assert not first.work_dir.exists()


async def test_shutdown_removes_work_dirs(tmp_path: Path) -> None:
    registry = BackupJobRegistry()
    job = registry.create("dump")
    job.work_dir = tmp_path / "wd"
    job.work_dir.mkdir()
    await registry.shutdown()
    assert not job.work_dir.exists()
