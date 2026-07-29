"""GitHub automation for the W3ID redirect PR (v0.3.0, ADR-0014).

Opt-in operator tooling: fork ``perma-id/w3id.org``, commit the deployment's
redirect ``.htaccess`` (+ README) to a branch, and open a pull request — or, when
the deployment moves, update the same branch so the existing PR carries the new
target. Reusable on purpose; the identifier base never changes, only the target.

Posture mirrors schema sync: it does nothing without an explicit token, and every
request host is checked against an allow-list (``api.github.com`` by default).
The GitHub Contents API is used (base64 file commits) so no git tree/blob
plumbing is needed.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
import structlog

from fdpneo_server.metadata.pid.w3id import W3IDConfig
from fdpneo_server.shared.errors import BadRequest

__all__ = ["PublishResult", "W3IDPublisher"]

log = structlog.get_logger(__name__)

API_BASE = "https://api.github.com"
UPSTREAM_OWNER = "perma-id"
UPSTREAM_REPO = "w3id.org"


@dataclass(frozen=True)
class PublishResult:
    """Outcome of a publish/update run."""

    pull_request_url: str
    branch: str
    created_pr: bool
    files: list[str]


class W3IDPublisher:
    """Drives the fork → commit → PR flow against the w3id.org repository.

    ``http_client`` is injected so tests mock it with respx. ``token`` is the
    GitHub credential; ``allowed_hosts`` is the structural outbound boundary.
    """

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient,
        token: str,
        allowed_hosts: list[str],
        fork_owner: str | None = None,
    ) -> None:
        self._http = http_client
        self._token = token
        self._allowed = set(allowed_hosts)
        self._fork_owner = fork_owner

    async def publish(self, config: W3IDConfig) -> PublishResult:
        """Idempotently publish ``config`` and open (or update) the PR."""
        owner = self._fork_owner or await self._authenticated_login()
        branch = f"fdp-pid-{config.prefix.replace('/', '-')}"
        await self._ensure_fork(owner)
        base_branch, base_sha = await self._upstream_base()
        await self._sync_branch(owner, branch, base_sha)

        files = {config.path: config.htaccess, f"{config.prefix}/README.md": config.readme}
        for path, content in files.items():
            await self._put_file(owner, branch, path, content, config.prefix)

        pr_url, created = await self._ensure_pull_request(owner, branch, base_branch, config)
        log.info(
            "pid_w3id_published",
            prefix=config.prefix,
            target=config.target,
            branch=branch,
            pull_request=pr_url,
            created_pr=created,
        )
        return PublishResult(
            pull_request_url=pr_url, branch=branch, created_pr=created, files=list(files)
        )

    # --- internals ---------------------------------------------------------

    def _check_host(self, url: str) -> None:
        host = urlsplit(url).hostname
        if host is None or host not in self._allowed:
            raise BadRequest(
                f"GitHub host not on allow-list: {host}",
                details={"host": host, "allowed": sorted(self._allowed)},
            )

    async def _request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        full = url if url.startswith("http") else f"{API_BASE}{url}"
        self._check_host(full)
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        resp = await self._http.request(method, full, headers=headers, **kwargs)  # type: ignore[arg-type]
        return resp

    @staticmethod
    def _ok(resp: httpx.Response, *allowed: int) -> httpx.Response:
        if resp.status_code not in allowed:
            raise BadRequest(
                f"GitHub API {resp.request.method} {resp.request.url.path} "
                f"returned {resp.status_code}",
                details={"status": resp.status_code, "body": resp.text[:500]},
            )
        return resp

    async def _authenticated_login(self) -> str:
        resp = self._ok(await self._request("GET", "/user"), 200)
        login = resp.json().get("login")
        if not login:
            raise BadRequest("could not resolve the authenticated GitHub user")
        return str(login)

    async def _ensure_fork(self, owner: str) -> None:
        # Idempotent: forking an already-forked repo returns the existing fork.
        existing = await self._request("GET", f"/repos/{owner}/{UPSTREAM_REPO}")
        if existing.status_code == 200:
            return
        self._ok(
            await self._request("POST", f"/repos/{UPSTREAM_OWNER}/{UPSTREAM_REPO}/forks"),
            202,
            200,
        )

    async def _upstream_base(self) -> tuple[str, str]:
        """Return the upstream default branch name and its head SHA."""
        repo = self._ok(
            await self._request("GET", f"/repos/{UPSTREAM_OWNER}/{UPSTREAM_REPO}"), 200
        ).json()
        branch = str(repo.get("default_branch", "master"))
        ref = self._ok(
            await self._request(
                "GET", f"/repos/{UPSTREAM_OWNER}/{UPSTREAM_REPO}/git/ref/heads/{branch}"
            ),
            200,
        ).json()
        return branch, str(ref["object"]["sha"])

    async def _sync_branch(self, owner: str, branch: str, base_sha: str) -> None:
        """Create the working branch at ``base_sha`` (or fast-forward it there)."""
        ref = f"heads/{branch}"
        existing = await self._request("GET", f"/repos/{owner}/{UPSTREAM_REPO}/git/ref/{ref}")
        if existing.status_code == 200:
            self._ok(
                await self._request(
                    "PATCH",
                    f"/repos/{owner}/{UPSTREAM_REPO}/git/refs/{ref}",
                    json={"sha": base_sha, "force": True},
                ),
                200,
            )
            return
        self._ok(
            await self._request(
                "POST",
                f"/repos/{owner}/{UPSTREAM_REPO}/git/refs",
                json={"ref": f"refs/heads/{branch}", "sha": base_sha},
            ),
            201,
        )

    async def _put_file(
        self, owner: str, branch: str, path: str, content: str, prefix: str
    ) -> None:
        # The Contents API needs the blob SHA when replacing an existing file.
        current = await self._request(
            "GET", f"/repos/{owner}/{UPSTREAM_REPO}/contents/{path}", params={"ref": branch}
        )
        sha = current.json().get("sha") if current.status_code == 200 else None
        payload: dict[str, object] = {
            "message": f"FDP persistent identifiers: w3id.org/{prefix}",
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "branch": branch,
        }
        if sha:
            payload["sha"] = sha
        self._ok(
            await self._request(
                "PUT", f"/repos/{owner}/{UPSTREAM_REPO}/contents/{path}", json=payload
            ),
            200,
            201,
        )

    async def _ensure_pull_request(
        self, owner: str, branch: str, base_branch: str, config: W3IDConfig
    ) -> tuple[str, bool]:
        head = f"{owner}:{branch}"
        existing = self._ok(
            await self._request(
                "GET",
                f"/repos/{UPSTREAM_OWNER}/{UPSTREAM_REPO}/pulls",
                params={"head": head, "state": "open"},
            ),
            200,
        ).json()
        if existing:
            # The branch push already updated the open PR; just report it.
            return str(existing[0]["html_url"]), False
        created = self._ok(
            await self._request(
                "POST",
                f"/repos/{UPSTREAM_OWNER}/{UPSTREAM_REPO}/pulls",
                json={
                    "title": f"Add w3id.org/{config.prefix} redirect for a FAIR Data Point",
                    "head": head,
                    "base": base_branch,
                    "body": (
                        f"Persistent identifier redirect for a FAIR Data Point.\n\n"
                        f"- Prefix: `https://w3id.org/{config.prefix}`\n"
                        f"- Target: {config.target}\n\n"
                        "Opened automatically by `fdp pid w3id-pr`."
                    ),
                    "maintainer_can_modify": True,
                },
            ),
            201,
        ).json()
        return str(created["html_url"]), True
