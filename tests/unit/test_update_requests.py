"""Update requests for the host updater (ADR-0030): validation, agent presence, lifecycle."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from fdpneo_server.identity.deps import require_auth
from fdpneo_server.shared.context import RequestContext
from fdpneo_server.shared.errors import (
    BadRequest,
    Conflict,
    Forbidden,
    register_exception_handlers,
)
from fdpneo_server.storage.postgres.models import Base, register_all_models
from fdpneo_server.update_requests import (
    AgentPoll,
    AgentReport,
    UpdateRequestCreate,
    UpdateRequestService,
    build_update_requests_router,
)
from fdpneo_server.updates import ReleaseView, UpdatesView

ADMIN = RequestContext(subject="kc#admin", roles=frozenset({"admin"}), trace_id="t")
STEWARD = RequestContext(subject="kc#alice", roles=frozenset({"steward"}), trace_id="t")
AGENT = RequestContext(subject="kc#admin", roles=frozenset({"updater"}), trace_id="t")


def _release(version: str, **extra: Any) -> ReleaseView:
    base: dict[str, Any] = {
        "version": version,
        "url": f"https://example.org/v{version}",
        "metadata_present": True,
        "security": False,
        "manual_steps": False,
        "min_upgrade_from": "0.17.0",
    }
    base.update(extra)
    return ReleaseView(**base)


class FakeChecker:
    def __init__(self, server: list[ReleaseView], client: list[ReleaseView]) -> None:
        self.server = server
        self.client = client

    async def view(self) -> UpdatesView:
        return UpdatesView(
            enabled=True,
            env_enabled=True,
            checked_at=None,
            error=None,
            server_version="0.20.0",
            latest_server=self.server[0].version if self.server else None,
            latest_client=self.client[0].version if self.client else None,
            server_releases=self.server,
            client_releases=self.client,
        )


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    register_all_models()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _service(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    server: list[ReleaseView] | None = None,
    client: list[ReleaseView] | None = None,
    clock: Clock | None = None,
) -> UpdateRequestService:
    checker = FakeChecker(
        server if server is not None else [_release("0.21.0")],
        client if client is not None else [_release("0.10.0", requires_server=">=0.20.0")],
    )
    return UpdateRequestService(
        session_factory=session_factory,
        checker=checker,  # pyright: ignore[reportArgumentType]
        running_version="0.20.0",
        clock=clock or Clock(),
    )


async def _connect(service: UpdateRequestService) -> None:
    await service.next_for_agent(AGENT, AgentPoll(agent_version="1.0"))


# --- requesting --------------------------------------------------------------


@pytest.mark.unit
async def test_request_needs_a_connected_agent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    clock = Clock()
    service = _service(session_factory, clock=clock)
    body = UpdateRequestCreate(component="server", version="0.21.0")
    with pytest.raises(Conflict, match="no updater agent"):
        await service.request(ADMIN, body)
    await _connect(service)
    created = await service.request(ADMIN, body)
    assert created.status == "pending"
    assert created.from_version == "0.20.0"
    assert created.requested_by == "kc#admin"
    # An agent silent for longer than the window no longer counts.
    await service.cancel(ADMIN, created.id)
    clock.now += timedelta(minutes=6)
    with pytest.raises(Conflict, match="no updater agent"):
        await service.request(ADMIN, body)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("release", "match"),
    [
        (_release("0.20.0"), "not newer"),
        (_release("0.21.0", metadata_present=False), "no upgrade metadata"),
        (_release("0.21.0", manual_steps=True), "manual upgrade steps"),
        (_release("0.21.0", manual_steps=None), "manual upgrade steps"),
        (_release("0.21.0", min_upgrade_from="0.20.1"), "too old"),
    ],
)
async def test_server_requests_are_validated(
    session_factory: async_sessionmaker[AsyncSession], release: ReleaseView, match: str
) -> None:
    service = _service(session_factory, server=[release])
    await _connect(service)
    with pytest.raises(BadRequest, match=match):
        await service.request(
            ADMIN, UpdateRequestCreate(component="server", version=release.version)
        )


@pytest.mark.unit
async def test_client_requests_check_the_running_client_and_server(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = _service(
        session_factory,
        client=[
            _release("0.11.0", requires_server=">=0.21.0", min_upgrade_from="0.1.0"),
            _release("0.10.0", min_upgrade_from="0.1.0"),
        ],
    )
    await _connect(service)
    with pytest.raises(BadRequest, match="running client version"):
        await service.request(ADMIN, UpdateRequestCreate(component="client", version="0.10.0"))
    with pytest.raises(BadRequest, match="newer server"):
        await service.request(
            ADMIN, UpdateRequestCreate(component="client", version="0.11.0", from_version="0.9.0")
        )
    created = await service.request(
        ADMIN, UpdateRequestCreate(component="client", version="v0.10.0", from_version="0.9.0")
    )
    assert created.target_version == "0.10.0"
    assert created.from_version == "0.9.0"


@pytest.mark.unit
async def test_unknown_releases_and_non_admins_are_refused(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = _service(session_factory)
    await _connect(service)
    with pytest.raises(BadRequest, match="unknown release"):
        await service.request(ADMIN, UpdateRequestCreate(component="server", version="0.99.0"))
    with pytest.raises(Forbidden):
        await service.request(STEWARD, UpdateRequestCreate(component="server", version="0.21.0"))


@pytest.mark.unit
async def test_only_one_request_may_be_open(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = _service(session_factory)
    await _connect(service)
    await service.request(ADMIN, UpdateRequestCreate(component="server", version="0.21.0"))
    with pytest.raises(Conflict, match="still open"):
        await service.request(ADMIN, UpdateRequestCreate(component="server", version="0.21.0"))


# --- the agent's side ----------------------------------------------------------


@pytest.mark.unit
async def test_agent_lifecycle(session_factory: async_sessionmaker[AsyncSession]) -> None:
    service = _service(session_factory)
    first = await service.next_for_agent(AGENT, AgentPoll(agent_version="1.0"))
    assert first.request is None
    assert first.server_version == "0.20.0"

    created = await service.request(
        ADMIN, UpdateRequestCreate(component="server", version="0.21.0")
    )
    polled = await service.next_for_agent(AGENT, AgentPoll(agent_version="1.0"))
    assert polled.request is not None
    assert polled.request.id == created.id

    claimed = await service.claim(AGENT, created.id)
    assert claimed.status == "claimed"
    assert claimed.claimed_at is not None
    with pytest.raises(Conflict):
        await service.claim(AGENT, created.id)  # a second agent loses the race
    with pytest.raises(Conflict):
        await service.cancel(ADMIN, created.id)  # too late to cancel

    done = await service.report(
        AGENT,
        created.id,
        AgentReport(status="succeeded", message="ok", backup_location="/backups/x.fdpneo"),
    )
    assert done.status == "succeeded"
    assert done.backup_location == "/backups/x.fdpneo"
    with pytest.raises(Conflict):
        await service.report(AGENT, created.id, AgentReport(status="failed"))

    history = await service.history(ADMIN)
    assert history.agent_connected
    assert history.agent_version == "1.0"
    assert [r.status for r in history.requests] == ["succeeded"]
    assert (await service.next_for_agent(AGENT, AgentPoll())).request is None


@pytest.mark.unit
async def test_roles_are_enforced_on_both_sides(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = _service(session_factory)
    with pytest.raises(Forbidden):
        await service.next_for_agent(ADMIN, AgentPoll())
    with pytest.raises(Forbidden):
        await service.history(AGENT)


@pytest.mark.unit
async def test_router_round_trip(session_factory: async_sessionmaker[AsyncSession]) -> None:
    service = _service(session_factory)
    current = {"ctx": AGENT}
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(build_update_requests_router(service=service), prefix="/fdp-api")
    app.dependency_overrides[require_auth] = lambda: current["ctx"]
    with TestClient(app) as client:
        assert client.post("/fdp-api/admin/updates/agent/next", json={}).json()["request"] is None
        current["ctx"] = ADMIN
        res = client.post(
            "/fdp-api/admin/updates/requests", json={"component": "server", "version": "0.21.0"}
        )
        assert res.status_code == 201
        request_id = res.json()["id"]
        listing = client.get("/fdp-api/admin/updates/requests").json()
        assert listing["agent_connected"] is True
        assert listing["requests"][0]["id"] == request_id
        cancelled = client.post(f"/fdp-api/admin/updates/requests/{request_id}/cancel")
        assert cancelled.json()["status"] == "cancelled"
        refused = client.post(
            "/fdp-api/admin/updates/requests", json={"component": "server", "version": "0.20.0"}
        )
        assert refused.status_code == 400
