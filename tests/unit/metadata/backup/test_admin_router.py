"""Unit tests for the admin backup/restore HTTP router (gating + status codes).

The job registry is faked so no background work runs; this exercises the HTTP
layer (admin gate, response shapes, download preconditions).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fdpneo_server.identity.deps import require_auth
from fdpneo_server.metadata.backup import build_backup_admin_router
from fdpneo_server.metadata.backup.jobs import BackupJob, JobState
from fdpneo_server.shared.context import RequestContext
from fdpneo_server.shared.errors import register_exception_handlers

pytestmark = pytest.mark.unit


def _ctx(*roles: str) -> RequestContext:
    return RequestContext(
        subject="https://idp/u",
        roles=frozenset(roles),
        request_timestamp=datetime(2026, 7, 6, tzinfo=UTC),
        trace_id="t",
    )


class _FakeRegistry:
    """Records jobs without running them."""

    def __init__(self) -> None:
        self.jobs: dict[str, BackupJob] = {}
        self.launched = 0
        self._n = 0

    def create(self, kind: str) -> BackupJob:
        self._n += 1
        job = BackupJob(id=f"job{self._n}", kind=kind)
        self.jobs[job.id] = job
        return job

    def get(self, job_id: str) -> BackupJob | None:
        return self.jobs.get(job_id)

    def launch(self, job: BackupJob, runner: Callable[[BackupJob], Awaitable[None]]) -> None:
        del job, runner
        self.launched += 1


def _app(registry: _FakeRegistry, ctx: RequestContext) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(
        build_backup_admin_router(
            registry=registry,  # type: ignore[arg-type]
            adapter=object(),  # type: ignore[arg-type]  # only touched inside the (faked) runner
            session_factory=object(),  # type: ignore[arg-type]
            settings=object(),  # type: ignore[arg-type]
        )
    )
    app.dependency_overrides[require_auth] = lambda: ctx
    return app


def test_dump_requires_admin_role() -> None:
    app = _app(_FakeRegistry(), _ctx("steward"))  # not admin
    with TestClient(app) as client:
        assert client.post("/admin/backup/dump").status_code == 403


def test_dump_starts_job_and_returns_202() -> None:
    registry = _FakeRegistry()
    app = _app(registry, _ctx("admin"))
    with TestClient(app) as client:
        resp = client.post("/admin/backup/dump")
    assert resp.status_code == 202
    body = resp.json()
    assert body["kind"] == "dump" and body["state"] == "QUEUED"
    assert registry.launched == 1


def test_restore_rejects_merge_with_overwrite() -> None:
    app = _app(_FakeRegistry(), _ctx("admin"))
    with TestClient(app) as client:
        resp = client.post(
            "/admin/backup/restore?merge=true&overwrite=true",
            files={"archive": ("backup.zip", b"PK\x03\x04", "application/zip")},
        )
    assert resp.status_code == 400


def test_restore_starts_job_and_returns_202() -> None:
    registry = _FakeRegistry()
    app = _app(registry, _ctx("admin"))
    with TestClient(app) as client:
        resp = client.post(
            "/admin/backup/restore",
            files={"archive": ("backup.zip", b"PK\x03\x04", "application/zip")},
        )
    assert resp.status_code == 202
    assert resp.json()["kind"] == "restore"
    assert registry.launched == 1


def test_job_status_404_when_absent() -> None:
    app = _app(_FakeRegistry(), _ctx("admin"))
    with TestClient(app) as client:
        assert client.get("/admin/backup/jobs/nope").status_code == 404


def test_download_conflict_when_dump_not_ready() -> None:
    registry = _FakeRegistry()
    job = registry.create("dump")  # still QUEUED, no artifact
    app = _app(registry, _ctx("admin"))
    with TestClient(app) as client:
        assert client.get(f"/admin/backup/jobs/{job.id}/archive").status_code == 409


def test_download_streams_archive_when_ready(tmp_path: Path) -> None:
    registry = _FakeRegistry()
    job = registry.create("dump")
    archive = tmp_path / "backup.zip"
    archive.write_bytes(b"PK\x03\x04zip")
    job.state = JobState.SUCCEEDED
    job.artifact_path = archive
    app = _app(registry, _ctx("admin"))
    with TestClient(app) as client:
        resp = client.get(f"/admin/backup/jobs/{job.id}/archive")
    assert resp.status_code == 200
    assert resp.content == b"PK\x03\x04zip"


def test_download_404_for_non_dump_job() -> None:
    registry = _FakeRegistry()
    job = registry.create("restore")
    app = _app(registry, _ctx("admin"))
    with TestClient(app) as client:
        assert client.get(f"/admin/backup/jobs/{job.id}/archive").status_code == 404


# --- full-deployment scope (ADR-0029) ---------------------------------------


def _full_app(registry: _FakeRegistry, ctx: RequestContext) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(
        build_backup_admin_router(
            registry=registry,  # type: ignore[arg-type]
            adapter=object(),  # type: ignore[arg-type]
            session_factory=object(),  # type: ignore[arg-type]
            settings=object(),  # type: ignore[arg-type]
            engine=object(),  # type: ignore[arg-type]  # only touched inside the runner
        )
    )
    app.dependency_overrides[require_auth] = lambda: ctx
    return app


def test_full_dump_requires_a_passphrase() -> None:
    registry = _FakeRegistry()
    with TestClient(_full_app(registry, _ctx("admin"))) as client:
        resp = client.post("/admin/backup/dump?scope=full")
    assert resp.status_code == 400
    assert "encrypted" in resp.json()["message"]
    assert registry.launched == 0


def test_full_dump_rejects_a_short_passphrase() -> None:
    registry = _FakeRegistry()
    with TestClient(_full_app(registry, _ctx("admin"))) as client:
        resp = client.post("/admin/backup/dump?scope=full", json={"passphrase": "short"})
    assert resp.status_code == 422
    assert registry.launched == 0


def test_full_dump_with_passphrase_starts_job() -> None:
    registry = _FakeRegistry()
    with TestClient(_full_app(registry, _ctx("admin"))) as client:
        resp = client.post(
            "/admin/backup/dump?scope=full",
            json={"passphrase": "a long enough passphrase"},
        )
    assert resp.status_code == 202
    assert registry.launched == 1


def test_full_dump_unavailable_without_engine() -> None:
    with TestClient(_app(_FakeRegistry(), _ctx("admin"))) as client:
        resp = client.post(
            "/admin/backup/dump?scope=full",
            json={"passphrase": "a long enough passphrase"},
        )
    assert resp.status_code == 400


def test_full_dump_still_requires_admin() -> None:
    with TestClient(_full_app(_FakeRegistry(), _ctx("steward"))) as client:
        resp = client.post(
            "/admin/backup/dump?scope=full",
            json={"passphrase": "a long enough passphrase"},
        )
    assert resp.status_code == 403


def test_sealed_upload_without_passphrase_is_rejected_up_front() -> None:
    registry = _FakeRegistry()
    with TestClient(_full_app(registry, _ctx("admin"))) as client:
        resp = client.post(
            "/admin/backup/restore",
            files={"archive": ("b.fdpbk", b"FDPNEO-ENC1" + b"\0" * 64, "application/octet-stream")},
        )
    assert resp.status_code == 400
    assert "passphrase" in resp.json()["message"]
    assert registry.launched == 0


def test_sealed_upload_with_passphrase_starts_job() -> None:
    registry = _FakeRegistry()
    with TestClient(_full_app(registry, _ctx("admin"))) as client:
        resp = client.post(
            "/admin/backup/restore",
            files={"archive": ("b.fdpbk", b"FDPNEO-ENC1" + b"\0" * 64, "application/octet-stream")},
            data={"passphrase": "a long enough passphrase"},
        )
    assert resp.status_code == 202
    assert registry.launched == 1


def test_download_of_a_sealed_dump_is_octet_stream(tmp_path: Path) -> None:
    registry = _FakeRegistry()
    job = registry.create("dump")
    sealed = tmp_path / "backup.fdpbk"
    sealed.write_bytes(b"FDPNEO-ENC1 sealed")
    job.state = JobState.SUCCEEDED
    job.artifact_path = sealed
    with TestClient(_full_app(registry, _ctx("admin"))) as client:
        resp = client.get(f"/admin/backup/jobs/{job.id}/archive")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/octet-stream"
    assert resp.headers["content-disposition"].endswith('.fdpbk"')
