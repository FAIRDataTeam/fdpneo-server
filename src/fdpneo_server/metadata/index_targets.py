"""Runtime-managed FDP Index ping targets (ADR-0025).

The outbound Index ping (:mod:`fdpneo_server.metadata.index_ping`, ADR-0020/0021)
originally read its targets only from ``FDP_INDEX_PING_TARGETS`` at boot. This
module makes targets **admin-managed runtime data**: a Postgres table unioned
with the (read-only) env-configured set, exposed through an admin REST surface

* ``GET    /fdp-api/index/targets``       — list (env + runtime, labeled)
* ``POST   /fdp-api/index/targets``       — add a runtime target
* ``DELETE /fdp-api/index/targets/{id}``  — remove a runtime target
* ``POST   /fdp-api/index/ping``          — ping every effective target now

and wired into the pinger via an async ``targets_provider`` so a deployment
that booted with **zero** targets starts announcing itself the moment the first
index is added — **no restart**. Per-target ping status is recorded on the
runtime rows (durable) and in memory for env targets (best-effort, lost on
restart; env targets have no row to own).

Admin-supplied URLs pass the shared SSRF guard (``assert_public_url``) —
translated to 400 here, because at this boundary the URL is the caller's own
request body, not server-supplied metadata (where the guard's 502 belongs).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Final, Literal
from urllib.parse import urlsplit, urlunsplit

import structlog
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import Boolean, Integer, String, select
from sqlalchemy.orm import Mapped, mapped_column

from fdpneo_server.identity.deps import require_auth
from fdpneo_server.shared.context import RequestContext
from fdpneo_server.shared.errors import BadRequest, Conflict, Forbidden, NotFound, UpstreamError
from fdpneo_server.shared.ssrf import assert_public_url
from fdpneo_server.storage.postgres.models import Base
from fdpneo_server.storage.postgres.types import AwareDateTime

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from fdpneo_server.config import IndexSettings
    from fdpneo_server.metadata.index_ping import IndexPinger, PingResult

log = structlog.get_logger(__name__)

_ADMIN_ROLE: Final = "admin"
_ALLOWED_SCHEMES: Final = frozenset({"http", "https"})


def normalize_target(url: str) -> str:
    """Canonical form for dedupe: lowercase scheme+host, no trailing slash.

    Compatible with ``IndexSettings.targets`` (which trims + rstrips), so env
    and runtime entries land in the same normal form and dedupe coherently.
    """
    parts = urlsplit(url.strip())
    netloc = parts.netloc.lower()
    return urlunsplit(
        (parts.scheme.lower(), netloc, parts.path.rstrip("/"), parts.query, "")
    ).rstrip("/")


# --- model -------------------------------------------------------------------


class IndexTargetRow(Base):
    """One admin-registered FDP Index target."""

    __tablename__ = "index_targets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    url: Mapped[str] = mapped_column(String(2048), nullable=False, unique=True)
    note: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    last_ping_at: Mapped[datetime | None] = mapped_column(AwareDateTime(), nullable=True)
    last_status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_detail: Mapped[str | None] = mapped_column(String(1024), nullable=True)


# --- repository ---------------------------------------------------------------


class IndexTargetRepository:
    """Async CRUD over ``index_targets``. One session per method (api-keys pattern)."""

    def __init__(self, *, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def add(self, row: IndexTargetRow) -> None:
        async with self._session_factory() as session:
            session.add(row)
            await session.commit()

    async def get(self, target_id: str) -> IndexTargetRow | None:
        async with self._session_factory() as session:
            return await session.get(IndexTargetRow, target_id)

    async def get_by_url(self, url: str) -> IndexTargetRow | None:
        async with self._session_factory() as session:
            stmt = select(IndexTargetRow).where(IndexTargetRow.url == url)
            return (await session.execute(stmt)).scalar_one_or_none()

    async def list_all(self) -> list[IndexTargetRow]:
        async with self._session_factory() as session:
            stmt = select(IndexTargetRow).order_by(IndexTargetRow.created_at)
            return list((await session.execute(stmt)).scalars())

    async def delete(self, target_id: str) -> bool:
        async with self._session_factory() as session:
            row = await session.get(IndexTargetRow, target_id)
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
            return True

    async def update_status(
        self,
        url: str,
        *,
        when: datetime,
        status_code: int | None,
        ok: bool,
        detail: str | None,
    ) -> None:
        """Record a ping outcome on the row; a no-op if the row is gone
        (a target may be removed while a ping batch is in flight)."""
        async with self._session_factory() as session:
            stmt = select(IndexTargetRow).where(IndexTargetRow.url == url)
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is None:
                return
            row.last_ping_at = when
            row.last_status_code = status_code
            row.last_ok = ok
            row.last_detail = detail
            await session.commit()


# --- DTOs ---------------------------------------------------------------------


class IndexTargetCreateRequest(BaseModel):
    """Body for ``POST /fdp-api/index/targets``."""

    url: str = Field(min_length=1, max_length=2048)
    note: str | None = Field(None, max_length=512)


class IndexTargetInfo(BaseModel):
    """One effective ping target (env-configured or runtime-managed)."""

    id: str | None
    url: str
    source: Literal["env", "runtime"]
    note: str | None = None
    created_at: datetime | None = None
    created_by: str | None = None
    last_ping_at: datetime | None = None
    last_status_code: int | None = None
    last_ok: bool | None = None
    last_detail: str | None = None


class IndexTargetList(BaseModel):
    targets: list[IndexTargetInfo]


class PingResultView(BaseModel):
    target: str
    status: int | None
    ok: bool
    detail: str | None = None


class IndexPingRunView(BaseModel):
    results: list[PingResultView]


# --- service ------------------------------------------------------------------


@dataclass
class _EnvStatus:
    when: datetime
    status_code: int | None
    ok: bool
    detail: str | None


class IndexTargetService:
    """Effective-target management: env set union runtime rows, plus ping status."""

    def __init__(
        self,
        *,
        repository: IndexTargetRepository,
        settings: IndexSettings,
        clock: Callable[[], datetime] | None = None,
        url_guard: Callable[..., Awaitable[None]] | None = None,
    ) -> None:
        self._repo = repository
        self._settings = settings
        self._clock = clock or (lambda: datetime.now(UTC))
        self._url_guard = url_guard or assert_public_url
        # Best-effort status for env targets (no row to own it); keyed by
        # normalized URL, in-memory only.
        self._env_status: dict[str, _EnvStatus] = {}

    # -- reads ------------------------------------------------------------

    def env_targets(self) -> list[str]:
        """Env-configured targets, re-read (late-bound) and normalized."""
        return [normalize_target(t) for t in self._settings.targets]

    async def effective_urls(self) -> Sequence[str]:
        """Env union runtime, deduped by normalized URL — the pinger's provider."""
        seen: dict[str, None] = dict.fromkeys(self.env_targets())
        for row in await self._repo.list_all():
            seen.setdefault(row.url)
        return list(seen)

    async def list_targets(self) -> list[IndexTargetInfo]:
        infos: list[IndexTargetInfo] = []
        for url in self.env_targets():
            status = self._env_status.get(url)
            infos.append(
                IndexTargetInfo(
                    id=None,
                    url=url,
                    source="env",
                    last_ping_at=status.when if status else None,
                    last_status_code=status.status_code if status else None,
                    last_ok=status.ok if status else None,
                    last_detail=status.detail if status else None,
                )
            )
        for row in await self._repo.list_all():
            infos.append(
                IndexTargetInfo(
                    id=row.id,
                    url=row.url,
                    source="runtime",
                    note=row.note,
                    created_at=row.created_at,
                    created_by=row.created_by,
                    last_ping_at=row.last_ping_at,
                    last_status_code=row.last_status_code,
                    last_ok=row.last_ok,
                    last_detail=row.last_detail,
                )
            )
        return infos

    # -- writes -----------------------------------------------------------

    async def add(self, *, url: str, note: str | None, subject: str | None) -> IndexTargetInfo:
        normalized = normalize_target(url)
        parts = urlsplit(normalized)
        if parts.scheme not in _ALLOWED_SCHEMES:
            raise BadRequest(
                f"index target scheme is not permitted: {parts.scheme or '<none>'!r}",
                details={"allowed_schemes": sorted(_ALLOWED_SCHEMES)},
            )
        if not parts.hostname:
            raise BadRequest("index target URL has no host")
        # SSRF guard: an index target is an admin-supplied URL this server will
        # POST to on a schedule. The shared guard raises UpstreamError (502 —
        # right for server-supplied metadata); here the URL is the caller's own
        # request body, so translate to 400.
        try:
            await self._url_guard(normalized)
        except UpstreamError as err:
            raise BadRequest(f"index target URL rejected: {err.message}") from err
        if normalized in self.env_targets():
            raise Conflict(
                "target is already configured via FDP_INDEX_PING_TARGETS",
                details={"url": normalized},
            )
        if await self._repo.get_by_url(normalized) is not None:
            raise Conflict("target is already registered", details={"url": normalized})
        row = IndexTargetRow(
            id=str(uuid.uuid4()),
            url=normalized,
            note=note,
            created_at=self._clock(),
            created_by=subject,
        )
        await self._repo.add(row)
        log.info("index_target_added", url=normalized, subject=subject)
        return IndexTargetInfo(
            id=row.id,
            url=row.url,
            source="runtime",
            note=row.note,
            created_at=row.created_at,
            created_by=row.created_by,
        )

    async def remove(self, target_id: str) -> None:
        if not await self._repo.delete(target_id):
            raise NotFound(f"no index target with id {target_id}")
        log.info("index_target_removed", target_id=target_id)

    # -- pinger hook --------------------------------------------------------

    async def record_results(self, results: list[PingResult]) -> None:
        """Persist per-target outcomes after a ping batch (any trigger).

        Status recording must never fail a ping — errors are logged, not raised.
        """
        when = self._clock()
        env = set(self.env_targets())
        for result in results:
            url = normalize_target(result.target)
            try:
                if url in env:
                    self._env_status[url] = _EnvStatus(
                        when=when, status_code=result.status, ok=result.ok, detail=result.detail
                    )
                else:
                    await self._repo.update_status(
                        url,
                        when=when,
                        status_code=result.status,
                        ok=result.ok,
                        detail=result.detail,
                    )
            except Exception:
                log.warning("index_ping_status_record_failed", target=url)


# --- router --------------------------------------------------------------------


def build_index_targets_router(
    *,
    service: IndexTargetService,
    pinger: IndexPinger,
    prefix: str = "/index",
) -> APIRouter:
    """Admin surface for runtime index targets. Every route is admin-gated —
    unlike licenses/policies, even the list: targets reveal deployment topology,
    not FAIR metadata."""
    router = APIRouter(prefix=prefix, tags=["index"])

    def _require_admin(ctx: RequestContext) -> None:
        if _ADMIN_ROLE not in ctx.roles:
            raise Forbidden(
                "admin role required to manage index targets",
                details={"required_role": _ADMIN_ROLE},
            )

    @router.get("/targets", response_model=IndexTargetList, name="index_target_list")
    async def list_targets(  # pyright: ignore[reportUnusedFunction]
        ctx: Annotated[RequestContext, Depends(require_auth)],
    ) -> IndexTargetList:
        _require_admin(ctx)
        return IndexTargetList(targets=await service.list_targets())

    @router.post(
        "/targets", response_model=IndexTargetInfo, status_code=201, name="index_target_add"
    )
    async def add_target(  # pyright: ignore[reportUnusedFunction]
        body: IndexTargetCreateRequest,
        ctx: Annotated[RequestContext, Depends(require_auth)],
    ) -> IndexTargetInfo:
        _require_admin(ctx)
        # Deliberately no auto-ping: POST /fdp-api/index/ping gives deterministic
        # per-target feedback instead of a fire-and-forget inside the create.
        return await service.add(url=body.url, note=body.note, subject=ctx.subject)

    @router.delete("/targets/{target_id}", status_code=204, name="index_target_remove")
    async def remove_target(  # pyright: ignore[reportUnusedFunction]
        target_id: str,
        ctx: Annotated[RequestContext, Depends(require_auth)],
    ) -> None:
        _require_admin(ctx)
        await service.remove(target_id)

    @router.post("/ping", response_model=IndexPingRunView, name="index_ping_now")
    async def ping_now(  # pyright: ignore[reportUnusedFunction]
        ctx: Annotated[RequestContext, Depends(require_auth)],
    ) -> IndexPingRunView:
        _require_admin(ctx)
        results = await pinger.ping_now("admin")
        return IndexPingRunView(
            results=[
                PingResultView(target=r.target, status=r.status, ok=r.ok, detail=r.detail)
                for r in results
            ]
        )

    return router


__all__ = [
    "IndexPingRunView",
    "IndexTargetCreateRequest",
    "IndexTargetInfo",
    "IndexTargetList",
    "IndexTargetRepository",
    "IndexTargetRow",
    "IndexTargetService",
    "PingResultView",
    "build_index_targets_router",
    "normalize_target",
]
