"""Update notifications (ADR-0028).

A composition-level concern, like :mod:`fdpneo_server.operational`: it cuts
across every bounded context and owns none of them.

* :class:`GitHubReleaseSource` reads the GitHub Releases API for one
  repository, ignores drafts and pre-releases, and attaches each release's
  ``fdpneo-release.json`` upgrade metadata (ADR-0028 §2). Requests are
  conditional (``If-None-Match``), so an unchanged repository costs nothing
  against GitHub's unauthenticated rate limit.
* :class:`UpdateChecker` runs the daily check in-process and caches the
  outcome in the single-row ``release_check`` table, so replicas share it and
  a restart does not refetch.
* :func:`build_updates_router` exposes ``GET /admin/updates`` and
  ``POST /admin/updates/check`` to administrators only.

What leaves the host: a ``GET`` to ``api.github.com`` (and the release asset
download) with ``User-Agent: fdpneo-server/<version>``. Nothing about the
deployment's content, users or identifier base is sent.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Any, Final

import httpx
import structlog
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import JSON, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from fdpneo_server import __version__
from fdpneo_server.identity.deps import require_auth
from fdpneo_server.shared.context import RequestContext
from fdpneo_server.shared.errors import Forbidden, TooManyRequests
from fdpneo_server.storage.postgres.models import Base
from fdpneo_server.storage.postgres.types import AwareDateTime

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from fdpneo_server.config import UpdatesSettings

log = structlog.get_logger(__name__)

RELEASE_ASSET: Final = "fdpneo-release.json"
"""Name of the upgrade-metadata asset attached to every release (ADR-0028 §2)."""

_RELEASES_PER_PAGE: Final = 30
# Metadata is fetched for the newest releases only; older ones are listed
# without it ("read the changelog"). Cached per version, so this bounds the
# asset downloads of a single check, not the history the client can see.
_METADATA_FETCH_LIMIT: Final = 10
_MAX_METADATA_BYTES: Final = 64 * 1024
_FORCE_MIN_INTERVAL_SECONDS: Final = 60.0
_LOOP_TICK_SECONDS: Final = 3600.0
_ROW_ID: Final = 1
_VERSION_RE: Final = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
_REQUIREMENT_RE: Final = re.compile(r"^\s*(>=|==)?\s*v?(\d+\.\d+\.\d+)\s*$")
_ADMIN_ROLE: Final = "admin"

Version = tuple[int, int, int]


# --- versions ----------------------------------------------------------------


def parse_version(value: str | None) -> Version | None:
    """``"0.19.0"`` or ``"v0.19.0"`` → ``(0, 19, 0)``; anything else → ``None``.

    Pre-release and build suffixes are deliberately unsupported: those releases
    are never offered as updates.
    """
    if value is None:
        return None
    match = _VERSION_RE.match(value.strip())
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def satisfies(version: str, requirement: str | None) -> bool | None:
    """Whether ``version`` meets a ``>=X.Y.Z`` (or ``==X.Y.Z`` / ``X.Y.Z``) requirement.

    ``None`` when either side cannot be parsed, so callers can say "unknown"
    instead of guessing.
    """
    if requirement is None:
        return True
    have = parse_version(version)
    match = _REQUIREMENT_RE.match(requirement)
    if have is None or match is None:
        return None
    need = parse_version(match.group(2))
    if need is None:
        return None
    if match.group(1) == "==":
        return have == need
    return have >= need


def _format(version: Version) -> str:
    return f"{version[0]}.{version[1]}.{version[2]}"


# --- persistence -------------------------------------------------------------


class ReleaseCheckRow(Base):
    """The last release check (single row, ``id = 1``)."""

    __tablename__ = "release_check"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    checked_at: Mapped[datetime | None] = mapped_column(AwareDateTime(), nullable=True)
    # SQLite (unit tests) has no JSONB; the variant falls back to JSON there.
    payload_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=False
    )
    error: Mapped[str | None] = mapped_column(String(1024), nullable=True)


# --- models ------------------------------------------------------------------


class ReleaseView(BaseModel):
    """One published release and its upgrade metadata (when the release has it)."""

    version: str
    published_at: str | None = None
    url: str
    metadata_present: bool = False
    security: bool | None = None
    manual_steps: bool | None = None
    min_upgrade_from: str | None = None
    requires_server: str | None = Field(
        default=None, description="Client releases: minimum server version, e.g. '>=0.18.0'."
    )
    upgrade_notes: str | None = None


class _ComponentCache(BaseModel):
    repo: str
    etag: str | None = None
    releases: list[ReleaseView] = Field(default_factory=list)


class _Payload(BaseModel):
    server: _ComponentCache | None = None
    client: _ComponentCache | None = None


class UpdatesView(BaseModel):
    """What ``GET /admin/updates`` returns."""

    enabled: bool = Field(description="Whether the daily check currently runs.")
    env_enabled: bool = Field(
        description="FDP_UPDATES_CHECK_ENABLED; when false the runtime setting cannot enable it."
    )
    checked_at: datetime | None
    error: str | None
    server_version: str
    latest_server: str | None
    latest_client: str | None
    server_releases: list[ReleaseView] = Field(
        description="Server releases newer than the running server, newest first."
    )
    client_releases: list[ReleaseView] = Field(
        description="Known client releases, newest first; the client compares its own version."
    )


# --- GitHub ------------------------------------------------------------------


class GitHubReleaseSource:
    """Reads one repository's published releases and their upgrade metadata."""

    def __init__(self, *, http_client: httpx.AsyncClient, api_base: str) -> None:
        self._http = http_client
        self._api = api_base.rstrip("/")
        self._headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": f"fdpneo-server/{__version__}",
        }

    async def fetch(
        self, repo: str, *, etag: str | None, known: dict[str, ReleaseView]
    ) -> tuple[list[ReleaseView], str | None] | None:
        """Releases newest first plus the new ETag, or ``None`` when unchanged (304)."""
        headers = dict(self._headers)
        if etag:
            headers["If-None-Match"] = etag
        response = await self._http.get(
            f"{self._api}/repos/{repo}/releases",
            params={"per_page": _RELEASES_PER_PAGE},
            headers=headers,
            timeout=10.0,
        )
        if response.status_code == 304:
            return None
        response.raise_for_status()
        items = response.json()
        if not isinstance(items, list):
            raise ValueError("unexpected GitHub releases payload")

        published: list[tuple[Version, dict[str, Any]]] = []
        for raw in items:  # pyright: ignore[reportUnknownVariableType]
            if not isinstance(raw, dict):
                continue
            item: dict[str, Any] = raw  # pyright: ignore[reportUnknownVariableType]
            if item.get("draft") or item.get("prerelease"):
                continue
            version = parse_version(str(item.get("tag_name", "")))
            if version is not None:
                published.append((version, item))
        published.sort(key=lambda pair: pair[0], reverse=True)

        releases: list[ReleaseView] = []
        for index, (version, item) in enumerate(published):
            text = _format(version)
            cached = known.get(text)
            if cached is not None and cached.metadata_present:
                releases.append(cached)
                continue
            metadata = await self._metadata(item, text) if index < _METADATA_FETCH_LIMIT else None
            releases.append(_release_view(text, item, metadata))
        return releases, response.headers.get("etag")

    async def _metadata(self, item: dict[str, Any], version: str) -> dict[str, Any] | None:
        assets = item.get("assets")
        if not isinstance(assets, list):
            return None
        url: str | None = None
        for asset in assets:  # pyright: ignore[reportUnknownVariableType]
            if isinstance(asset, dict) and asset.get("name") == RELEASE_ASSET:  # pyright: ignore[reportUnknownMemberType]
                candidate = asset.get("browser_download_url")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
                # Asset links must be https. Only a deliberately plain-http
                # API base (a local test double) may serve plain-http assets.
                if isinstance(candidate, str) and (
                    candidate.startswith("https://")
                    or (self._api.startswith("http://") and candidate.startswith("http://"))
                ):
                    url = candidate
                break
        if url is None:
            return None
        try:
            response = await self._http.get(
                url,
                headers={"User-Agent": self._headers["User-Agent"]},
                follow_redirects=True,
                timeout=10.0,
            )
            response.raise_for_status()
            if len(response.content) > _MAX_METADATA_BYTES:
                return None
            data = json.loads(response.content)
        except (httpx.HTTPError, ValueError):
            log.warning("release_metadata_unreadable", version=version, url=url)
            return None
        if not isinstance(data, dict) or data.get("version") != version:
            log.warning("release_metadata_mismatch", version=version)
            return None
        return data  # pyright: ignore[reportUnknownVariableType]


def _optional_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _release_view(
    version: str, item: dict[str, Any], metadata: dict[str, Any] | None
) -> ReleaseView:
    url = item.get("html_url")
    published = item.get("published_at")
    view = ReleaseView(
        version=version,
        published_at=published if isinstance(published, str) else None,
        url=url if isinstance(url, str) else "",
    )
    if metadata is None:
        return view
    requires = metadata.get("requires")
    requires_server = (
        _optional_str(requires.get("server"))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        if isinstance(requires, dict)
        else None
    )
    return view.model_copy(
        update={
            "metadata_present": True,
            "security": _optional_bool(metadata.get("security")),
            "manual_steps": _optional_bool(metadata.get("manual_steps")),
            "min_upgrade_from": _optional_str(metadata.get("min_upgrade_from")),
            "requires_server": requires_server,
            "upgrade_notes": _optional_str(metadata.get("upgrade_notes")),
        }
    )


# --- checker -----------------------------------------------------------------


class UpdateChecker:
    """Runs the release check on a schedule and serves its cached outcome."""

    def __init__(
        self,
        *,
        settings: UpdatesSettings,
        session_factory: async_sessionmaker[AsyncSession],
        source: GitHubReleaseSource,
        enabled_provider: Callable[[], Awaitable[bool]],
        running_version: str = __version__,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._sessions = session_factory
        self._source = source
        self._enabled_provider = enabled_provider
        self._running = running_version
        self._clock = clock
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._last_forced: float | None = None

    # lifecycle

    def start(self) -> None:
        """Start the background loop unless the hard switch is off. Idempotent."""
        if not self._settings.check_enabled:
            log.info("update_check_disabled_by_env")
            return
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="update-check")

    async def stop(self) -> None:
        """Cancel the loop. Idempotent."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        await asyncio.sleep(self._settings.startup_delay_seconds)
        while True:
            try:
                await self.check_if_due()
            except Exception:  # a failed check must never kill the loop
                log.warning("update_check_failed", exc_info=True)
            await asyncio.sleep(min(_LOOP_TICK_SECONDS, self._settings.check_interval_hours * 3600))

    # checks

    async def enabled(self) -> bool:
        """Effective switch: the env hard switch AND the ``updates.check`` setting."""
        if not self._settings.check_enabled:
            return False
        return await self._enabled_provider()

    async def check_if_due(self) -> bool:
        """Run a check when enabled and the last one is older than the interval."""
        if not await self.enabled():
            return False
        row = await self._load()
        interval = timedelta(hours=self._settings.check_interval_hours)
        if (
            row is not None
            and row.checked_at is not None
            and datetime.now(UTC) - row.checked_at < interval
        ):
            return False
        await self._check()
        return True

    async def check_now(self) -> UpdatesView:
        """An admin-forced check, at most once a minute. Disabled → current view."""
        if not await self.enabled():
            return await self.view()
        now = self._clock()
        if self._last_forced is not None and now - self._last_forced < _FORCE_MIN_INTERVAL_SECONDS:
            raise TooManyRequests("an update check ran less than a minute ago")
        self._last_forced = now
        await self._check()
        return await self.view()

    async def _check(self) -> None:
        async with self._lock:
            row = await self._load()
            payload = _Payload.model_validate(row.payload_json) if row is not None else _Payload()
            errors: list[str] = []
            for component, repo in (
                ("server", self._settings.server_repo),
                ("client", self._settings.client_repo),
            ):
                cache: _ComponentCache | None = getattr(payload, component)
                if cache is None or cache.repo != repo:
                    cache = _ComponentCache(repo=repo)
                known = {release.version: release for release in cache.releases}
                try:
                    result = await self._source.fetch(repo, etag=cache.etag, known=known)
                except (httpx.HTTPError, ValueError) as exc:
                    errors.append(f"{component}: {type(exc).__name__}: {exc}"[:300])
                    setattr(payload, component, cache)
                    continue
                if result is not None:
                    releases, etag = result
                    cache = _ComponentCache(repo=repo, etag=etag, releases=releases)
                setattr(payload, component, cache)
            await self._save(payload, error="; ".join(errors) or None)
            log.info(
                "update_check_completed",
                errors=len(errors),
                latest_server=_latest(payload.server),
                latest_client=_latest(payload.client),
            )

    # views

    async def view(self) -> UpdatesView:
        row = await self._load()
        payload = _Payload.model_validate(row.payload_json) if row is not None else _Payload()
        running = parse_version(self._running)
        server_releases = payload.server.releases if payload.server is not None else []
        newer = [
            release
            for release in server_releases
            if running is not None and (parse_version(release.version) or (0, 0, 0)) > running
        ]
        return UpdatesView(
            enabled=await self.enabled(),
            env_enabled=self._settings.check_enabled,
            checked_at=row.checked_at if row is not None else None,
            error=row.error if row is not None else None,
            server_version=self._running,
            latest_server=_latest(payload.server),
            latest_client=_latest(payload.client),
            server_releases=newer,
            client_releases=payload.client.releases if payload.client is not None else [],
        )

    # persistence

    async def _load(self) -> ReleaseCheckRow | None:
        async with self._sessions() as session:
            return await session.get(ReleaseCheckRow, _ROW_ID)

    async def _save(self, payload: _Payload, *, error: str | None) -> None:
        async with self._sessions() as session:
            row = await session.get(ReleaseCheckRow, _ROW_ID)
            data = payload.model_dump(mode="json")
            if row is None:
                session.add(
                    ReleaseCheckRow(
                        id=_ROW_ID, checked_at=datetime.now(UTC), payload_json=data, error=error
                    )
                )
            else:
                row.checked_at = datetime.now(UTC)
                row.payload_json = data
                row.error = error
            await session.commit()


def _latest(cache: _ComponentCache | None) -> str | None:
    if cache is None or not cache.releases:
        return None
    return cache.releases[0].version


# --- router ------------------------------------------------------------------


def build_updates_router(*, checker: UpdateChecker) -> APIRouter:
    """Admin-only update notifications at ``/admin/updates``."""
    router = APIRouter(tags=["updates"])

    def _require_admin(ctx: RequestContext) -> None:
        if _ADMIN_ROLE not in ctx.roles:
            raise Forbidden("admin role required to view update information")

    @router.get("/admin/updates", response_model=UpdatesView, name="updates_view")
    async def get_updates(  # pyright: ignore[reportUnusedFunction]
        ctx: Annotated[RequestContext, Depends(require_auth)],
    ) -> UpdatesView:
        """The cached outcome of the last release check."""
        _require_admin(ctx)
        return await checker.view()

    @router.post("/admin/updates/check", response_model=UpdatesView, name="updates_check")
    async def check_updates(  # pyright: ignore[reportUnusedFunction]
        ctx: Annotated[RequestContext, Depends(require_auth)],
    ) -> UpdatesView:
        """Check GitHub now (at most once a minute); returns the refreshed view."""
        _require_admin(ctx)
        return await checker.check_now()

    return router


__all__ = [
    "RELEASE_ASSET",
    "GitHubReleaseSource",
    "ReleaseCheckRow",
    "ReleaseView",
    "UpdateChecker",
    "UpdatesView",
    "build_updates_router",
    "parse_version",
    "satisfies",
]
