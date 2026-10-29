"""Update requests for the optional host updater (ADR-0030).

The server never touches the container runtime. An administrator *requests*
an update to a release the update check (ADR-0028) knows about; the server
validates it and records it in ``update_request``. The host updater agent —
off by default, in its own compose profile or as a systemd unit — polls with
an ``updater``-scoped API key, claims the request, re-validates the release
against GitHub on its own, applies it, and reports the outcome.

Surfaces:

* Admins: ``POST /admin/updates/requests`` (request), ``GET`` (history plus
  whether an agent is connected), ``POST …/{id}/cancel`` (while pending).
* The agent (``updater`` role only): ``POST /admin/updates/agent/next``
  (heartbeat; returns the oldest pending request or ``null``), ``POST
  …/agent/requests/{id}/claim`` and ``POST …/agent/requests/{id}/report``.

Validation (ADR-0030 §2) — a request is refused unless the target release is
newer than the running version, carries upgrade metadata with
``manual_steps: false``, its ``min_upgrade_from`` is satisfied, a client
target's ``requires.server`` holds against the running server, no other
request is open, and an agent was seen recently.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Final, Literal

import structlog
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import Integer, String, select, update
from sqlalchemy.orm import Mapped, mapped_column

from fdpneo_server.identity.api_keys import UPDATER_ROLE
from fdpneo_server.identity.deps import require_auth
from fdpneo_server.shared.context import RequestContext
from fdpneo_server.shared.errors import BadRequest, Conflict, Forbidden, NotFound
from fdpneo_server.storage.postgres.models import Base
from fdpneo_server.storage.postgres.types import AwareDateTime
from fdpneo_server.updates import ReleaseView, parse_version, satisfies

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from fdpneo_server.updates import UpdateChecker

log = structlog.get_logger(__name__)

_ADMIN_ROLE: Final = "admin"
_AGENT_ROW_ID: Final = 1
AGENT_ONLINE_WINDOW: Final = timedelta(minutes=5)
"""An agent that polled within this window counts as connected."""

Component = Literal["server", "client"]
OpenStatus = Literal["pending", "claimed"]
FinalStatus = Literal["succeeded", "rolled_back", "failed", "needs_attention"]
Status = Literal[
    "pending", "claimed", "succeeded", "rolled_back", "failed", "needs_attention", "cancelled"
]
_OPEN: Final = ("pending", "claimed")
AuthCtx = Annotated[RequestContext, Depends(require_auth)]


# --- persistence -------------------------------------------------------------


class UpdateRequestRow(Base):
    """One admin-requested update and its outcome."""

    __tablename__ = "update_request"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    component: Mapped[str] = mapped_column(String(16), nullable=False)
    target_version: Mapped[str] = mapped_column(String(32), nullable=False)
    from_version: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    requested_by: Mapped[str] = mapped_column(String(2048), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(AwareDateTime(), nullable=False)
    claimed_at: Mapped[datetime | None] = mapped_column(AwareDateTime(), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(AwareDateTime(), nullable=True)
    message: Mapped[str | None] = mapped_column(String(4000), nullable=True)
    backup_location: Mapped[str | None] = mapped_column(String(1024), nullable=True)


class UpdaterAgentRow(Base):
    """Last contact from the host updater agent (single row, ``id = 1``)."""

    __tablename__ = "updater_agent"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    last_seen_at: Mapped[datetime] = mapped_column(AwareDateTime(), nullable=False)
    agent_version: Mapped[str | None] = mapped_column(String(64), nullable=True)


# --- models ------------------------------------------------------------------


class UpdateRequestCreate(BaseModel):
    component: Component
    version: str = Field(description="Target release, X.Y.Z.")
    from_version: str | None = Field(
        default=None,
        description="Client requests: the client version the admin is running (the server "
        "knows its own).",
    )


class UpdateRequestView(BaseModel):
    id: str
    component: Component
    target_version: str
    from_version: str
    status: Status
    requested_by: str
    requested_at: datetime
    claimed_at: datetime | None
    finished_at: datetime | None
    message: str | None
    backup_location: str | None


class UpdateRequestList(BaseModel):
    agent_connected: bool
    agent_last_seen: datetime | None
    agent_version: str | None
    requests: list[UpdateRequestView]


class AgentPoll(BaseModel):
    agent_version: str | None = Field(default=None, max_length=64)


class AgentNext(BaseModel):
    request: UpdateRequestView | None
    server_version: str = Field(description="The version of the server answering the poll.")


class AgentReport(BaseModel):
    status: FinalStatus
    message: str | None = Field(default=None, max_length=4000)
    backup_location: str | None = Field(default=None, max_length=1024)


# --- service -----------------------------------------------------------------


class UpdateRequestService:
    """Validates, records and hands out update requests."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        checker: UpdateChecker,
        running_version: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._sessions = session_factory
        self._checker = checker
        self._running = running_version
        self._clock = clock or (lambda: datetime.now(UTC))

    # admin side

    async def request(self, ctx: RequestContext, body: UpdateRequestCreate) -> UpdateRequestView:
        _require(ctx, _ADMIN_ROLE)
        assert ctx.subject is not None  # require_auth guarantees a subject
        target = body.version.strip().removeprefix("v")
        current = self._running if body.component == "server" else (body.from_version or "")
        if parse_version(target) is None:
            raise BadRequest("version must be X.Y.Z", details={"version": body.version})
        if parse_version(current) is None:
            raise BadRequest(
                "the running client version is required and must be X.Y.Z",
                details={"from_version": body.from_version},
            )
        release = await self._release(body.component, target)
        _validate(body.component, release, current=current, running_server=self._running)

        now = self._clock()
        async with self._sessions() as session:
            agent = await session.get(UpdaterAgentRow, _AGENT_ROW_ID)
            if agent is None or now - agent.last_seen_at > AGENT_ONLINE_WINDOW:
                raise Conflict("no updater agent is connected; see ADR-0030 to enable one")
            open_rows = await session.scalars(
                select(UpdateRequestRow).where(UpdateRequestRow.status.in_(_OPEN))
            )
            if open_rows.first() is not None:
                raise Conflict("another update request is still open")
            row = UpdateRequestRow(
                id=str(uuid.uuid4()),
                component=body.component,
                target_version=target,
                from_version=current,
                status="pending",
                requested_by=ctx.subject,
                requested_at=now,
            )
            session.add(row)
            await session.commit()
        log.info(
            "update_requested",
            request_id=row.id,
            component=row.component,
            target=target,
            from_version=current,
            by=ctx.subject,
        )
        return _view(row)

    async def history(self, ctx: RequestContext, *, limit: int = 20) -> UpdateRequestList:
        _require(ctx, _ADMIN_ROLE)
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(UpdateRequestRow)
                    .order_by(UpdateRequestRow.requested_at.desc())
                    .limit(limit)
                )
            ).all()
            agent = await session.get(UpdaterAgentRow, _AGENT_ROW_ID)
        now = self._clock()
        return UpdateRequestList(
            agent_connected=agent is not None and now - agent.last_seen_at <= AGENT_ONLINE_WINDOW,
            agent_last_seen=agent.last_seen_at if agent is not None else None,
            agent_version=agent.agent_version if agent is not None else None,
            requests=[_view(row) for row in rows],
        )

    async def cancel(self, ctx: RequestContext, request_id: str) -> UpdateRequestView:
        _require(ctx, _ADMIN_ROLE)
        row = await self._transition(request_id, from_status="pending", to_status="cancelled")
        log.info("update_request_cancelled", request_id=request_id, by=ctx.subject)
        return row

    # agent side

    async def next_for_agent(self, ctx: RequestContext, poll: AgentPoll) -> AgentNext:
        _require(ctx, UPDATER_ROLE)
        now = self._clock()
        async with self._sessions() as session:
            agent = await session.get(UpdaterAgentRow, _AGENT_ROW_ID)
            if agent is None:
                session.add(
                    UpdaterAgentRow(
                        id=_AGENT_ROW_ID, last_seen_at=now, agent_version=poll.agent_version
                    )
                )
            else:
                agent.last_seen_at = now
                agent.agent_version = poll.agent_version
            pending = (
                await session.scalars(
                    select(UpdateRequestRow)
                    .where(UpdateRequestRow.status == "pending")
                    .order_by(UpdateRequestRow.requested_at)
                    .limit(1)
                )
            ).first()
            await session.commit()
        return AgentNext(
            request=_view(pending) if pending is not None else None,
            server_version=self._running,
        )

    async def claim(self, ctx: RequestContext, request_id: str) -> UpdateRequestView:
        _require(ctx, UPDATER_ROLE)
        row = await self._transition(request_id, from_status="pending", to_status="claimed")
        log.info("update_request_claimed", request_id=request_id)
        return row

    async def report(
        self, ctx: RequestContext, request_id: str, body: AgentReport
    ) -> UpdateRequestView:
        _require(ctx, UPDATER_ROLE)
        row = await self._transition(
            request_id,
            from_status="claimed",
            to_status=body.status,
            message=body.message,
            backup_location=body.backup_location,
        )
        log.info("update_request_finished", request_id=request_id, status=body.status)
        return row

    # internals

    async def _release(self, component: Component, version: str) -> ReleaseView:
        view = await self._checker.view()
        releases = view.server_releases if component == "server" else view.client_releases
        for release in releases:
            if release.version == version:
                return release
        raise BadRequest(
            "unknown release; run an update check first",
            details={"component": component, "version": version},
        )

    async def _transition(
        self,
        request_id: str,
        *,
        from_status: OpenStatus,
        to_status: Status,
        message: str | None = None,
        backup_location: str | None = None,
    ) -> UpdateRequestView:
        now = self._clock()
        values: dict[str, object] = {"status": to_status}
        if to_status == "claimed":
            values["claimed_at"] = now
        else:
            values["finished_at"] = now
            values["message"] = message
            values["backup_location"] = backup_location
        async with self._sessions() as session:
            # Compare-and-set on the status, so two agents can never both
            # claim, and a report cannot land on a cancelled request.
            result = await session.execute(
                update(UpdateRequestRow)
                .where(UpdateRequestRow.id == request_id, UpdateRequestRow.status == from_status)
                .values(**values)
            )
            await session.commit()
            row = await session.get(UpdateRequestRow, request_id)
        if row is None:
            raise NotFound(f"no update request: {request_id}")
        if getattr(result, "rowcount", 0) != 1:
            raise Conflict(
                f"update request is {row.status}, not {from_status}",
                details={"status": row.status},
            )
        return _view(row)


def _require(ctx: RequestContext, role: str) -> None:
    if role not in ctx.roles:
        raise Forbidden(f"{role} role required")


def _validate(
    component: Component, release: ReleaseView, *, current: str, running_server: str
) -> None:
    target = parse_version(release.version)
    have = parse_version(current)
    if target is None or have is None or target <= have:
        raise BadRequest(
            "the target release is not newer than the running version",
            details={"target": release.version, "current": current},
        )
    if not release.metadata_present:
        raise BadRequest(
            "this release publishes no upgrade metadata; update it manually",
            details={"version": release.version},
        )
    if release.manual_steps is not False:
        raise BadRequest(
            "this release needs manual upgrade steps; follow its upgrade notes instead",
            details={"version": release.version, "upgrade_notes": release.upgrade_notes},
        )
    if (
        satisfies(current, f">={release.min_upgrade_from}" if release.min_upgrade_from else None)
        is not True
    ):
        raise BadRequest(
            "the running version is too old to upgrade to this release directly",
            details={"current": current, "min_upgrade_from": release.min_upgrade_from},
        )
    if component == "client" and satisfies(running_server, release.requires_server) is not True:
        raise BadRequest(
            "this client release needs a newer server; upgrade the server first",
            details={"requires_server": release.requires_server, "server": running_server},
        )


def _view(row: UpdateRequestRow) -> UpdateRequestView:
    return UpdateRequestView.model_validate(row, from_attributes=True)


# --- router ------------------------------------------------------------------


def build_update_requests_router(*, service: UpdateRequestService) -> APIRouter:
    router = APIRouter(tags=["updates"])

    @router.post(
        "/admin/updates/requests",
        response_model=UpdateRequestView,
        status_code=201,
        name="update_request_create",
    )
    async def create(body: UpdateRequestCreate, ctx: AuthCtx) -> UpdateRequestView:  # pyright: ignore[reportUnusedFunction]
        """Request an update; the host updater agent applies it (ADR-0030)."""
        return await service.request(ctx, body)

    @router.get(
        "/admin/updates/requests", response_model=UpdateRequestList, name="update_request_list"
    )
    async def history(ctx: AuthCtx) -> UpdateRequestList:  # pyright: ignore[reportUnusedFunction]
        """Recent requests, and whether an updater agent is connected."""
        return await service.history(ctx)

    @router.post(
        "/admin/updates/requests/{request_id}/cancel",
        response_model=UpdateRequestView,
        name="update_request_cancel",
    )
    async def cancel(request_id: str, ctx: AuthCtx) -> UpdateRequestView:  # pyright: ignore[reportUnusedFunction]
        """Cancel a request the agent has not claimed yet."""
        return await service.cancel(ctx, request_id)

    @router.post("/admin/updates/agent/next", response_model=AgentNext, name="update_agent_next")
    async def agent_next(poll: AgentPoll, ctx: AuthCtx) -> AgentNext:  # pyright: ignore[reportUnusedFunction]
        """Agent heartbeat: the oldest pending request, or null."""
        return await service.next_for_agent(ctx, poll)

    @router.post(
        "/admin/updates/agent/requests/{request_id}/claim",
        response_model=UpdateRequestView,
        name="update_agent_claim",
    )
    async def agent_claim(request_id: str, ctx: AuthCtx) -> UpdateRequestView:  # pyright: ignore[reportUnusedFunction]
        return await service.claim(ctx, request_id)

    @router.post(
        "/admin/updates/agent/requests/{request_id}/report",
        response_model=UpdateRequestView,
        name="update_agent_report",
    )
    async def agent_report(  # pyright: ignore[reportUnusedFunction]
        request_id: str, body: AgentReport, ctx: AuthCtx
    ) -> UpdateRequestView:
        return await service.report(ctx, request_id, body)

    return router


__all__ = [
    "AGENT_ONLINE_WINDOW",
    "AgentNext",
    "AgentPoll",
    "AgentReport",
    "UpdateRequestCreate",
    "UpdateRequestList",
    "UpdateRequestRow",
    "UpdateRequestService",
    "UpdateRequestView",
    "UpdaterAgentRow",
    "build_update_requests_router",
]
