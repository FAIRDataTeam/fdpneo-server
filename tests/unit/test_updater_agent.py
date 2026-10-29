"""The host updater agent (deploy/updater, ADR-0030), with Docker and HTTP faked."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "fdpneo_updater", ROOT / "deploy" / "updater" / "fdpneo_updater.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["fdpneo_updater"] = module
    spec.loader.exec_module(module)
    return module


upd = _load()

GITHUB = "https://api.github.test"
SERVER_URL = "http://server:8000"


def _meta(version: str, component: str = "server", **extra: Any) -> dict[str, Any]:
    return {
        "schema": 1,
        "component": component,
        "version": version,
        "min_upgrade_from": "0.17.0",
        "manual_steps": False,
        "security": False,
        "requires": {},
        **extra,
    }


class FakeDocker:
    """Records commands; answers pull/inspect/compose like a healthy host."""

    def __init__(self, *, revision_after: str = "0012_updater", fail_up: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.revision = "0012_updater"
        self.revision_after = revision_after
        self.fail_up = fail_up
        self.ups = 0

    def __call__(self, cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
        cmd = list(cmd)
        self.calls.append(cmd)
        out, code = "", 0
        if cmd[:2] == ["docker", "image"]:
            ref = cmd[-1]
            repo = ref.rsplit(":", 1)[0]
            out = f'["{repo}@sha256:{"a" * 64}"]'
        elif cmd[:2] == ["docker", "compose"] and "alembic_version" in cmd[-1]:
            out = self.revision + "\n"
        elif cmd[:2] == ["docker", "compose"] and "up" in cmd:
            self.ups += 1
            if self.ups == 1:
                self.revision = self.revision_after
            if self.fail_up and self.ups == 1:
                code = 1
        elif cmd[:2] == ["sh", "-c"]:
            out = "creating backup\n/backups/fdpneo-20261029.fdpneo\n"
        return subprocess.CompletedProcess(cmd, code, out, "")


class FakeServer:
    """The FDP server's agent API and info endpoint, plus GitHub."""

    def __init__(self, *, metadata: dict[str, Any] | None, info_version: str) -> None:
        self.metadata = metadata
        self.info_version = info_version
        self.reports: list[dict[str, Any]] = []
        self.claimed: list[str] = []

    def __call__(
        self,
        method: str,
        url: str,
        *,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 15.0,
    ) -> Any:
        del timeout
        if url.endswith("/claim"):
            assert headers is not None and headers["Authorization"].startswith("Bearer fdpk_")
            self.claimed.append(url)
            return {}
        if url.endswith("/report"):
            assert body is not None
            self.reports.append(body)
            return {}
        if url.endswith("/fdp-api/info"):
            return {"version": self.info_version}
        if "/releases/tags/" in url:
            assets = (
                [
                    {
                        "name": "fdpneo-release.json",
                        "browser_download_url": "https://github.test/meta",
                    }
                ]
                if self.metadata is not None
                else []
            )
            return {"draft": False, "prerelease": False, "assets": assets}
        if url == "https://github.test/meta":
            return self.metadata
        raise AssertionError(f"unexpected {method} {url}")


def _config(tmp_path: Path) -> Any:
    (tmp_path / "compose.yaml").write_text("services: {}\n")
    (tmp_path / ".env").write_text("# stack\nPOSTGRES_USER=fdp\nSERVER_IMAGE_TAG=0.20.0\n")
    return upd.Config.from_env(
        {
            "FDP_UPDATER_KEY": "fdpk_test",
            "STACK_DIR": str(tmp_path),
            "FDP_URL": SERVER_URL,
            "GITHUB_API": GITHUB,
            "BACKUP_COMMAND": "./backup.sh",
            "READY_TIMEOUT": "30",
        }
    )


def _agent(config: Any, docker: FakeDocker, server: FakeServer, *, ready: bool = True) -> Any:
    now = [0.0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    return upd.Agent(
        config,
        stack=upd.Stack(config, runner=docker),
        http=server,
        status=lambda _url, **_kw: 200 if ready else 503,  # pyright: ignore[reportUnknownLambdaType]
        sleep=sleep,
        clock=lambda: now[0],
    )


REQUEST = {"id": "r1", "component": "server", "target_version": "0.21.0", "from_version": "0.20.0"}


# --- pure helpers --------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("metadata", "reason"),
    [
        (None, "publishes no"),
        (_meta("0.21.0", manual_steps=True), "manual upgrade steps"),
        (_meta("0.21.0", manual_steps=None), "manual upgrade steps"),
        (_meta("0.21.0", min_upgrade_from="0.20.1"), "older than min_upgrade_from"),
        (_meta("0.22.0"), "does not describe"),
        (_meta("0.21.0", component="client"), "does not describe"),
    ],
)
def test_validate_release_refusals(metadata: dict[str, Any] | None, reason: str) -> None:
    with pytest.raises(upd.RefusedError, match=reason):
        upd.validate_release(
            metadata, component="server", target="0.21.0", current="0.20.0", running_server="0.20.0"
        )


@pytest.mark.unit
def test_validate_release_checks_client_requirements() -> None:
    meta = _meta(
        "0.10.0", component="client", min_upgrade_from="0.1.0", requires={"server": ">=0.21.0"}
    )
    with pytest.raises(upd.RefusedError, match="requires server"):
        upd.validate_release(
            meta, component="client", target="0.10.0", current="0.9.0", running_server="0.20.0"
        )
    upd.validate_release(
        meta, component="client", target="0.10.0", current="0.9.0", running_server="0.21.0"
    )
    with pytest.raises(upd.RefusedError, match="not newer"):
        upd.validate_release(
            meta, component="client", target="0.10.0", current="0.10.0", running_server="0.21.0"
        )


@pytest.mark.unit
def test_env_editing_keeps_other_lines(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("# comment\nA=1\nSERVER_IMAGE_TAG=0.20.0\nB='two'\n")
    upd.set_env_value(env, "SERVER_IMAGE_TAG", "0.21.0@sha256:abc")
    assert upd.read_env_value(env, "SERVER_IMAGE_TAG") == "0.21.0@sha256:abc"
    assert upd.read_env_value(env, "B") == "two"
    upd.set_env_value(env, "CLIENT_IMAGE_TAG", "0.10.0")
    upd.set_env_value(env, "SERVER_IMAGE_TAG", None)
    text = env.read_text()
    assert "SERVER_IMAGE_TAG" not in text
    assert text.startswith("# comment\nA=1\n")
    assert text.rstrip().endswith("CLIENT_IMAGE_TAG=0.10.0")


@pytest.mark.unit
def test_config_requires_a_key_a_stack_and_a_backup(tmp_path: Path) -> None:
    (tmp_path / "compose.yaml").write_text("services: {}\n")
    base = {"FDP_UPDATER_KEY": "fdpk_x", "STACK_DIR": str(tmp_path), "BACKUP_COMMAND": "./b.sh"}
    with pytest.raises(SystemExit, match="FDP_UPDATER_KEY"):
        upd.Config.from_env({**base, "FDP_UPDATER_KEY": "eyJhbGciOi"})
    with pytest.raises(SystemExit, match="STACK_DIR"):
        upd.Config.from_env({**base, "STACK_DIR": str(tmp_path / "missing")})
    with pytest.raises(SystemExit, match="BACKUP_COMMAND"):
        upd.Config.from_env({k: v for k, v in base.items() if k != "BACKUP_COMMAND"})
    no_backup = upd.Config.from_env({**base, "UPDATER_SKIP_BACKUP": "true"})
    assert no_backup.backup_command is None


# --- whole requests ------------------------------------------------------------


@pytest.mark.unit
def test_successful_server_update_pins_the_digest(tmp_path: Path) -> None:
    config = _config(tmp_path)
    docker = FakeDocker()
    server = FakeServer(metadata=_meta("0.21.0"), info_version="0.21.0")
    outcome = _agent(config, docker, server).handle(REQUEST, "0.20.0")
    assert outcome == "succeeded"
    tag = upd.read_env_value(config.env_file, "SERVER_IMAGE_TAG")
    assert tag == f"0.21.0@sha256:{'a' * 64}"
    assert server.reports[-1]["status"] == "succeeded"
    assert server.reports[-1]["backup_location"] == "/backups/fdpneo-20261029.fdpneo"
    pulls = [c for c in docker.calls if c[:2] == ["docker", "pull"]]
    assert pulls == [["docker", "pull", "ghcr.io/fairdatateam/fdpneo-server:0.21.0"]]
    ups = [c for c in docker.calls if "up" in c]
    assert ups[0][-3:] == ["-d", "--no-deps", "server"]


@pytest.mark.unit
def test_refused_release_changes_nothing(tmp_path: Path) -> None:
    config = _config(tmp_path)
    docker = FakeDocker()
    server = FakeServer(metadata=_meta("0.21.0", manual_steps=True), info_version="0.20.0")
    assert _agent(config, docker, server).handle(REQUEST, "0.20.0") == "failed"
    assert "refused" in server.reports[-1]["message"]
    assert upd.read_env_value(config.env_file, "SERVER_IMAGE_TAG") == "0.20.0"
    assert not any(c[:2] in (["docker", "pull"], ["sh", "-c"]) for c in docker.calls)


@pytest.mark.unit
def test_unready_update_without_migration_rolls_back(tmp_path: Path) -> None:
    config = _config(tmp_path)
    docker = FakeDocker(revision_after="0012_updater")  # schema unchanged
    server = FakeServer(metadata=_meta("0.21.0"), info_version="0.20.0")
    outcome = _agent(config, docker, server, ready=False).handle(REQUEST, "0.20.0")
    assert outcome == "rolled_back"
    assert upd.read_env_value(config.env_file, "SERVER_IMAGE_TAG") == "0.20.0"
    assert docker.ups == 2  # applied, then rolled back
    assert "did not become ready" in server.reports[-1]["message"]


@pytest.mark.unit
def test_unready_update_after_a_migration_needs_attention(tmp_path: Path) -> None:
    config = _config(tmp_path)
    docker = FakeDocker(revision_after="0013_future")
    server = FakeServer(metadata=_meta("0.21.0"), info_version="0.20.0")
    outcome = _agent(config, docker, server, ready=False).handle(REQUEST, "0.20.0")
    assert outcome == "needs_attention"
    assert docker.ups == 1  # no rollback attempted onto a migrated schema
    report = server.reports[-1]
    assert "0012_updater -> 0013_future" in report["message"]
    assert report["backup_location"] == "/backups/fdpneo-20261029.fdpneo"


@pytest.mark.unit
def test_failed_backup_aborts_before_any_change(tmp_path: Path) -> None:
    config = _config(tmp_path)

    class NoBackup(FakeDocker):
        def __call__(self, cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
            if list(cmd)[:2] == ["sh", "-c"]:
                return subprocess.CompletedProcess(list(cmd), 1, "", "disk full")
            return super().__call__(cmd)

    docker = NoBackup()
    server = FakeServer(metadata=_meta("0.21.0"), info_version="0.21.0")
    assert _agent(config, docker, server).handle(REQUEST, "0.20.0") == "failed"
    assert "no update without a backup" in server.reports[-1]["message"]
    assert upd.read_env_value(config.env_file, "SERVER_IMAGE_TAG") == "0.20.0"
    assert docker.ups == 0
