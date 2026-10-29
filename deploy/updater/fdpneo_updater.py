#!/usr/bin/env python3
"""FDPneo host updater agent (ADR-0030).

Optional and off by default. It runs next to a bundled compose stack, either
as the ``updater`` compose profile or as a systemd unit, and applies updates
that an administrator requested in the web client. It needs control of the
Docker daemon, which is root-equivalent on a standard Docker host.

Loop: poll the server with an ``updater``-scoped API key (which doubles as a
heartbeat), claim a pending request, then:

1. re-validate the release against GitHub on its own: published, not a
   pre-release, upgrade metadata present, ``manual_steps`` false,
   ``min_upgrade_from`` satisfied, and for a client, ``requires.server``;
2. pull the image by tag and pin the pulled digest (``X.Y.Z@sha256:…``);
3. take a full backup with the stack's backup command (ADR-0029);
4. write ``SERVER_IMAGE_TAG`` / ``CLIENT_IMAGE_TAG`` in the stack's ``.env``
   and recreate only that service;
5. wait for readiness and for the new version to report itself;
6. report the outcome. On failure it rolls back to the previous tag only when
   no database migration ran; otherwise it reports ``needs_attention`` with
   the backup's location.

It never edits compose files, never touches the database, identity provider,
triple store or proxy services, and never acts without a request.

Standard library only, so the image is the Docker CLI plus Python.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

AGENT_VERSION = "1.0.0"
RELEASE_ASSET = "fdpneo-release.json"
_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
_REQUIREMENT_RE = re.compile(r"^\s*(>=|==)?\s*v?(\d+\.\d+\.\d+)\s*$")

log = logging.getLogger("fdpneo-updater")

Version = tuple[int, int, int]


class RefusedError(Exception):
    """The release must not be applied; nothing was changed."""


# --- versions ------------------------------------------------------------------


def parse_version(value: str | None) -> Version | None:
    match = _VERSION_RE.match((value or "").strip())
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def satisfies(version: str, requirement: str | None) -> bool | None:
    if not requirement:
        return True
    have = parse_version(version)
    match = _REQUIREMENT_RE.match(requirement)
    need = parse_version(match.group(2)) if match else None
    if have is None or need is None or match is None:
        return None
    return have == need if match.group(1) == "==" else have >= need


def validate_release(
    metadata: dict[str, Any] | None,
    *,
    component: str,
    target: str,
    current: str,
    running_server: str,
) -> None:
    """Raise :class:`RefusedError` unless ADR-0030 allows applying this release unattended."""
    t, c = parse_version(target), parse_version(current)
    if t is None or c is None or t <= c:
        raise RefusedError(f"{target} is not newer than the running {component} {current}")
    if metadata is None:
        raise RefusedError(f"release {target} publishes no {RELEASE_ASSET}")
    if metadata.get("version") != target or metadata.get("component") != component:
        raise RefusedError(f"{RELEASE_ASSET} does not describe {component} {target}")
    if metadata.get("manual_steps") is not False:
        raise RefusedError(f"release {target} needs manual upgrade steps")
    minimum = metadata.get("min_upgrade_from")
    if satisfies(current, f">={minimum}" if minimum else None) is not True:
        raise RefusedError(f"{current} is older than min_upgrade_from {minimum}")
    if component == "client":
        requires = metadata.get("requires")
        needed = requires.get("server") if isinstance(requires, dict) else None
        if satisfies(running_server, needed) is not True:
            raise RefusedError(
                f"client {target} requires server {needed}; running {running_server}"
            )


# --- .env editing --------------------------------------------------------------


def read_env_value(path: Path, key: str) -> str | None:
    if not path.exists():
        return None
    value = None
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith(f"{key}="):
            value = stripped.split("=", 1)[1].strip().strip("'\"")
    return value


def set_env_value(path: Path, key: str, value: str | None) -> None:
    """Set ``key=value`` (or remove it when ``value`` is None), keeping everything else."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    out: list[str] = []
    done = False
    for line in lines:
        if line.strip().startswith(f"{key}="):
            if value is not None and not done:
                out.append(f"{key}={value}")
                done = True
            continue
        out.append(line)
    if value is not None and not done:
        out.append(f"{key}={value}")
    tmp = path.with_name(path.name + ".updater-tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    tmp.replace(path)


# --- HTTP ----------------------------------------------------------------------


def http_json(
    method: str,
    url: str,
    *,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 15.0,
) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)  # noqa: S310 (configured URLs)
    request.add_header("User-Agent", f"fdpneo-updater/{AGENT_VERSION}")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        raw = response.read()
    return json.loads(raw) if raw else None


def http_status(url: str, *, timeout: float = 5.0) -> int:
    try:
        request = urllib.request.Request(url, method="GET")  # noqa: S310
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except (urllib.error.URLError, OSError, TimeoutError):
        return 0


# --- configuration -------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    server_url: str
    api_key: str
    stack_dir: Path
    github_api: str
    server_repo: str
    client_repo: str
    client_url: str
    backup_command: str | None
    backup_dir: Path
    backup_keep: int
    poll_seconds: float
    ready_timeout: float

    @property
    def env_file(self) -> Path:
        return self.stack_dir / ".env"

    @staticmethod
    def from_env(environ: dict[str, str]) -> Config:
        key = environ.get("FDP_UPDATER_KEY", "").strip()
        key_file = environ.get("FDP_UPDATER_KEY_FILE")
        if not key and key_file:
            key = Path(key_file).read_text(encoding="utf-8").strip()
        if not key.startswith("fdpk_"):
            raise SystemExit(
                "FDP_UPDATER_KEY (or FDP_UPDATER_KEY_FILE) must hold an updater-scoped API key "
                "(fdpk_…); mint one under API keys as an administrator"
            )
        stack_dir = environ.get("STACK_DIR", "").strip()
        if not stack_dir or not (Path(stack_dir) / "compose.yaml").exists():
            raise SystemExit(
                "STACK_DIR must be the absolute path of the stack's compose.yaml directory"
            )
        backup = environ.get("BACKUP_COMMAND", "").strip()
        if environ.get("UPDATER_SKIP_BACKUP", "").lower() == "true":
            backup = ""
        elif not backup:
            raise SystemExit(
                "BACKUP_COMMAND is required (ADR-0030 takes a full backup before every update); "
                "set UPDATER_SKIP_BACKUP=true to run without one, which is not recommended"
            )
        passphrase_file = environ.get("BACKUP_PASSPHRASE_FILE", "").strip()
        if backup and not (passphrase_file and Path(passphrase_file).is_file()):
            raise SystemExit(
                "BACKUP_PASSPHRASE_FILE must name a readable file: the backup is encrypted and "
                "the agent has no terminal to prompt on (put it under FDPNEO_ROOT, mode 0600)"
            )
        return Config(
            server_url=environ.get("FDP_URL", "http://server:8000").rstrip("/"),
            api_key=key,
            stack_dir=Path(stack_dir),
            github_api=environ.get("GITHUB_API", "https://api.github.com").rstrip("/"),
            server_repo=environ.get("SERVER_REPO", "FAIRDataTeam/fdpneo-server"),
            client_repo=environ.get("CLIENT_REPO", "FAIRDataTeam/fdpneo-client"),
            client_url=environ.get("CLIENT_URL", "http://client:80").rstrip("/"),
            backup_command=backup or None,
            backup_dir=Path(environ.get("BACKUP_DIR") or f"{stack_dir}/backups"),
            backup_keep=max(1, int(environ.get("BACKUP_KEEP") or "3")),
            poll_seconds=float(environ.get("POLL_SECONDS") or "60"),
            ready_timeout=float(environ.get("READY_TIMEOUT") or "300"),
        )


# --- docker --------------------------------------------------------------------


Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def run(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
    log.info("$ %s", shlex.join(cmd))
    return subprocess.run(list(cmd), capture_output=True, text=True, check=False)  # noqa: S603


@dataclass
class Stack:
    config: Config
    runner: Runner = run
    _env_cache: dict[str, str] = field(default_factory=dict)

    def compose(self, *args: str) -> subprocess.CompletedProcess[str]:
        # Only the project directory: compose then loads compose.yaml plus any
        # compose.override.yaml and honours COMPOSE_FILE from .env, exactly as
        # the operator's own `docker compose` does in that directory.
        directory = str(self.config.stack_dir)
        return self.runner(["docker", "compose", "--project-directory", directory, *args])

    def must(self, result: subprocess.CompletedProcess[str], what: str) -> str:
        if result.returncode != 0:
            raise RuntimeError(f"{what} failed: {(result.stderr or result.stdout).strip()[-500:]}")
        return result.stdout

    def image_repo(self, component: str) -> str:
        key = "SERVER_IMAGE" if component == "server" else "CLIENT_IMAGE"
        default = f"ghcr.io/fairdatateam/fdpneo-{component}"
        return read_env_value(self.config.env_file, key) or default

    def pull_and_pin(self, component: str, version: str) -> str:
        """Pull ``repo:version`` and return ``version@sha256:…`` for that exact image."""
        ref = f"{self.image_repo(component)}:{version}"
        self.must(self.runner(["docker", "pull", ref]), f"docker pull {ref}")
        out = self.must(
            self.runner(["docker", "image", "inspect", "--format", "{{json .RepoDigests}}", ref]),
            "docker image inspect",
        )
        digests = json.loads(out.strip() or "[]")
        repo = self.image_repo(component)
        for entry in digests:
            name, _, digest = str(entry).partition("@")
            if name == repo and digest.startswith("sha256:"):
                return f"{version}@{digest}"
        raise RuntimeError(f"no registry digest recorded for {ref}")

    def migration_revision(self) -> str | None:
        user = read_env_value(self.config.env_file, "POSTGRES_USER") or "fdp"
        db = read_env_value(self.config.env_file, "POSTGRES_DB") or "fdp"
        result = self.compose(
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            user,
            "-d",
            db,
            "-Atc",
            "select version_num from alembic_version",
        )
        return result.stdout.strip() or None if result.returncode == 0 else None

    def running_client_version(self) -> str | None:
        result = self.compose("ps", "-q", "client")
        container = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
        if not container:
            return None
        label = self.runner(
            [
                "docker",
                "inspect",
                "--format",
                '{{ index .Config.Labels "org.opencontainers.image.version" }}',
                container,
            ]
        ).stdout.strip()
        return label if parse_version(label) else None

    def recreate(self, component: str) -> None:
        self.must(self.compose("up", "-d", "--no-deps", component), f"recreate {component}")


# --- the agent -----------------------------------------------------------------


class Agent:
    def __init__(
        self,
        config: Config,
        *,
        stack: Stack | None = None,
        http: Callable[..., Any] = http_json,
        status: Callable[..., int] = http_status,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.stack = stack or Stack(config)
        self.http = http
        self.status = status
        self.sleep = sleep
        self.clock = clock

    # server API

    def _api(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        return self.http(
            method,
            f"{self.config.server_url}/fdp-api/admin/updates/agent{path}",
            body=body if body is not None else {},
            headers={"Authorization": f"Bearer {self.config.api_key}"},
        )

    def poll(self) -> tuple[dict[str, Any] | None, str]:
        answer = self._api("POST", "/next", {"agent_version": AGENT_VERSION})
        return answer.get("request"), str(answer.get("server_version", ""))

    def report(
        self, request_id: str, status: str, message: str, backup_location: str | None = None
    ) -> None:
        """Report the outcome, retrying while the server is (re)starting."""
        log.info("report %s: %s %s", request_id, status, message)
        body = {"status": status, "message": message[:4000], "backup_location": backup_location}
        deadline = self.clock() + self.config.ready_timeout
        while True:
            try:
                self._api("POST", f"/requests/{request_id}/report", body)
                return
            except (urllib.error.URLError, OSError, ValueError) as exc:
                if self.clock() >= deadline:
                    log.error(
                        "could not report %s for request %s: %s (%s)",
                        status,
                        request_id,
                        exc,
                        message,
                    )
                    return
                self.sleep(5)

    # GitHub

    def release_metadata(self, component: str, version: str) -> dict[str, Any] | None:
        repo = self.config.server_repo if component == "server" else self.config.client_repo
        release = self.http(
            "GET", f"{self.config.github_api}/repos/{repo}/releases/tags/v{version}"
        )
        if not isinstance(release, dict) or release.get("draft") or release.get("prerelease"):
            raise RefusedError(f"v{version} is not a published release of {repo}")
        for asset in release.get("assets") or []:
            if isinstance(asset, dict) and asset.get("name") == RELEASE_ASSET:
                url = str(asset.get("browser_download_url", ""))
                if url.startswith("https://") or url.startswith(self.config.github_api):
                    data = self.http("GET", url)
                    return data if isinstance(data, dict) else None
        return None

    # readiness

    def wait_ready(self, component: str, version: str) -> bool:
        deadline = self.clock() + self.config.ready_timeout
        while self.clock() < deadline:
            if component == "server":
                if self.status(f"{self.config.server_url}/fdp-api/readyz") == 200:
                    try:
                        info = self.http("GET", f"{self.config.server_url}/fdp-api/info")
                    except (urllib.error.URLError, OSError, ValueError):
                        info = None
                    if isinstance(info, dict) and info.get("version") == version:
                        return True
            elif self.status(f"{self.config.client_url}/") == 200:
                running = self.stack.running_client_version()
                if running in (None, version):
                    return True
            self.sleep(5)
        return False

    def backup(self, component: str, version: str) -> str | None:
        """Run the backup command with an archive path in ``backup_dir``; return that path.

        The command runs from the stack directory with the archive path
        appended as its last argument (``backup.sh`` takes it that way).
        ``TMPDIR`` points inside ``backup_dir``: helper containers started by
        the script bind-mount its work directory, and only paths under the
        host-path mounts resolve on the host.
        """
        if self.config.backup_command is None:
            log.warning("UPDATER_SKIP_BACKUP=true: updating without a backup")
            return None
        backup_dir = self.config.backup_dir
        work = backup_dir / ".tmp"
        work.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        archive = backup_dir / f"fdpneo-before-{component}-{version}-{stamp}.fdpbk"
        command = (
            f"cd {shlex.quote(str(self.config.stack_dir))} && "
            f"TMPDIR={shlex.quote(str(work))} {self.config.backup_command} "
            f"{shlex.quote(str(archive))}"
        )
        result = self.stack.runner(["sh", "-c", command])
        if result.returncode != 0:
            raise RuntimeError(f"backup failed: {(result.stderr or result.stdout).strip()[-500:]}")
        if not archive.exists():
            raise RuntimeError(f"the backup command did not create {archive}")
        self._prune_backups()
        return str(archive)

    def _prune_backups(self) -> None:
        """Keep the newest ``backup_keep`` pre-update archives this agent made."""
        mine = sorted(
            self.config.backup_dir.glob("fdpneo-before-*.fdpbk"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for old in mine[self.config.backup_keep :]:
            log.info("removing old pre-update backup %s", old)
            old.unlink(missing_ok=True)

    # one request

    def handle(self, request: dict[str, Any], server_version: str) -> str:
        request_id = str(request["id"])
        component = str(request["component"])
        target = str(request["target_version"])
        self._api("POST", f"/requests/{request_id}/claim")
        tag_key = "SERVER_IMAGE_TAG" if component == "server" else "CLIENT_IMAGE_TAG"

        # 1-2: validate and pull; nothing has changed if these fail.
        try:
            if component == "server":
                current = server_version
            else:
                current = self.stack.running_client_version() or str(request["from_version"])
            validate_release(
                self.release_metadata(component, target),
                component=component,
                target=target,
                current=current,
                running_server=server_version,
            )
            pinned = self.stack.pull_and_pin(component, target)
        except RefusedError as exc:
            self.report(request_id, "failed", f"refused: {exc}")
            return "failed"
        except Exception as exc:  # GitHub/registry trouble: still nothing changed
            self.report(request_id, "failed", f"could not prepare the update: {exc}")
            return "failed"

        # 3: backup; abort without changes if it fails.
        try:
            backup_location = self.backup(component, target)
        except Exception as exc:
            self.report(request_id, "failed", f"no update without a backup: {exc}")
            return "failed"

        # 4-5: apply and verify.
        previous = read_env_value(self.config.env_file, tag_key)
        revision_before = self.stack.migration_revision() if component == "server" else None
        try:
            set_env_value(self.config.env_file, tag_key, pinned)
            self.stack.recreate(component)
            if self.wait_ready(component, target):
                self.report(
                    request_id,
                    "succeeded",
                    f"{component} updated to {target} ({pinned})",
                    backup_location,
                )
                return "succeeded"
            failure = (
                f"{component} {target} did not become ready within {self.config.ready_timeout:.0f}s"
            )
        except Exception as exc:
            failure = f"applying {component} {target} failed: {exc}"

        # 6: roll back only if the schema is unchanged.
        revision_after = self.stack.migration_revision() if component == "server" else None
        if component == "server" and revision_after != revision_before:
            self.report(
                request_id,
                "needs_attention",
                f"{failure}. A database migration ran ({revision_before} -> {revision_after}), so "
                f"the previous server cannot run on it; restore the backup to roll back.",
                backup_location,
            )
            return "needs_attention"
        try:
            set_env_value(self.config.env_file, tag_key, previous)
            self.stack.recreate(component)
            if component == "server":
                self.wait_ready(component, server_version)
        except Exception as exc:
            self.report(
                request_id, "needs_attention", f"{failure}; rollback failed: {exc}", backup_location
            )
            return "needs_attention"
        self.report(
            request_id,
            "rolled_back",
            f"{failure}; rolled back to {previous or 'the default tag'}",
            backup_location,
        )
        return "rolled_back"

    def run_once(self) -> str | None:
        request, server_version = self.poll()
        if request is None:
            return None
        return self.handle(request, server_version)

    def run_forever(self) -> None:
        log.info("fdpneo-updater %s watching %s", AGENT_VERSION, self.config.stack_dir)
        while True:
            try:
                self.run_once()
            except Exception:
                log.exception("poll failed")
            self.sleep(self.config.poll_seconds)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FDPneo host updater agent (ADR-0030).")
    parser.add_argument("--once", action="store_true", help="poll once and exit")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    agent = Agent(Config.from_env(dict(os.environ)))
    if args.once:
        outcome = agent.run_once()
        log.info("outcome: %s", outcome or "no pending request")
        return 0
    agent.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
