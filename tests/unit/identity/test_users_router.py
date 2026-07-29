"""Unit tests for the `/users` admin facade router (ADR-0013).

The router is driven over a FastAPI app with a fake :class:`UserDirectory`, so
these cover the HTTP behaviour the server owns: admin gating, status codes,
validation, the self-lockout / last-admin guards, and the feature-off (503) path.
The Keycloak adapter is tested separately in ``test_keycloak_admin.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fdpneo_server.identity.users import (
    CreateUserRequest,
    UpdateUserRequest,
    UserDirectory,
    UserInfo,
    build_users_router,
)
from fdpneo_server.shared.context import RequestContext
from fdpneo_server.shared.errors import Conflict, NotFound
from fdpneo_server.shared.events import EventBus

ISSUER = "http://idp.local/realms/fdp"
ADMIN_ID = "11111111-1111-1111-1111-111111111111"
STEWARD_ID = "22222222-2222-2222-2222-222222222222"
ABSENT_ID = "33333333-3333-3333-3333-333333333333"


# --- fake directory --------------------------------------------------------


@dataclass
class _FakeDirectory:
    users: dict[str, UserInfo] = field(default_factory=dict)

    async def list_users(
        self, *, search: str | None, limit: int, offset: int
    ) -> tuple[list[UserInfo], int]:
        items = list(self.users.values())
        if search:
            items = [u for u in items if search.lower() in (u.username + (u.email or "")).lower()]
        total = len(items)
        return items[offset : offset + limit], total

    async def get_user(self, user_id: str) -> UserInfo:
        if user_id not in self.users:
            raise NotFound(f"no user: {user_id}")
        return self.users[user_id]

    async def create_user(self, req: CreateUserRequest) -> UserInfo:
        if any(u.username == req.username for u in self.users.values()):
            raise Conflict("username exists")
        uid = f"id-{req.username}"
        info = UserInfo(
            id=uid,
            username=req.username,
            email=req.email,
            first_name=req.first_name,
            last_name=req.last_name,
            roles=list(req.roles),
            enabled=req.enabled,
        )
        self.users[uid] = info
        return info

    async def update_user(self, user_id: str, req: UpdateUserRequest) -> UserInfo:
        cur = await self.get_user(user_id)
        self.users[user_id] = cur.model_copy(
            update={k: v for k, v in req.model_dump().items() if v is not None}
        )
        return self.users[user_id]

    async def delete_user(self, user_id: str) -> None:
        await self.get_user(user_id)
        del self.users[user_id]

    async def count_admins(self) -> int:
        return sum(1 for u in self.users.values() if "admin" in u.roles)


def _seed() -> _FakeDirectory:
    return _FakeDirectory(
        users={
            ADMIN_ID: UserInfo(
                id=ADMIN_ID, username="admin", email="a@x", roles=["admin", "steward"]
            ),
            STEWARD_ID: UserInfo(id=STEWARD_ID, username="alice", email="al@x", roles=["steward"]),
        }
    )


# --- client harness --------------------------------------------------------


def _client(
    directory: UserDirectory | None,
    *,
    ctx: RequestContext,
    event_bus: EventBus | None = None,
) -> TestClient:
    from fdpneo_server.identity.deps import current_context
    from fdpneo_server.shared.errors import register_exception_handlers

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(build_users_router(directory=directory, event_bus=event_bus))
    app.dependency_overrides[current_context] = lambda: ctx
    return TestClient(app)


@dataclass
class _CapturingBus:
    # A capturing stand-in for EventBus; events are read back via isinstance
    # checks in the assertions, so the element type is intentionally Any.
    events: list[Any] = field(default_factory=list)

    async def publish(self, event: object) -> None:
        self.events.append(event)


def _admin() -> RequestContext:
    return RequestContext(
        subject=f"{ISSUER}#{ADMIN_ID}", roles=frozenset({"admin", "steward"}), trace_id="t"
    )


def _steward() -> RequestContext:
    return RequestContext(
        subject=f"{ISSUER}#{STEWARD_ID}", roles=frozenset({"steward"}), trace_id="t"
    )


def _anon() -> RequestContext:
    return RequestContext.anonymous(trace_id="t")


# --- auth gating -----------------------------------------------------------


@pytest.mark.unit
def test_requires_authentication() -> None:
    assert _client(_seed(), ctx=_anon()).get("/users").status_code == 401


@pytest.mark.unit
def test_requires_admin() -> None:
    assert _client(_seed(), ctx=_steward()).get("/users").status_code == 403


@pytest.mark.unit
def test_feature_off_returns_503() -> None:
    c = _client(None, ctx=_admin())
    assert c.get("/users").status_code == 503
    assert c.get("/users/roles").status_code == 503
    assert c.get(f"/users/{ADMIN_ID}").status_code == 503


# --- reads -----------------------------------------------------------------


@pytest.mark.unit
def test_list_and_search_and_roles() -> None:
    c = _client(_seed(), ctx=_admin())
    body = c.get("/users").json()
    assert body["total"] == 2 and {u["username"] for u in body["users"]} == {"admin", "alice"}
    assert c.get("/users", params={"search": "alice"}).json()["total"] == 1
    assert c.get("/users/roles").json()["roles"] == ["steward", "admin"]
    assert c.get(f"/users/{ADMIN_ID}").json()["username"] == "admin"
    assert c.get(f"/users/{ABSENT_ID}").status_code == 404


@pytest.mark.unit
def test_limit_bounds_enforced() -> None:
    c = _client(_seed(), ctx=_admin())
    assert c.get("/users", params={"limit": 0}).status_code == 422
    assert c.get("/users", params={"limit": 500}).status_code == 422


# --- create ----------------------------------------------------------------


@pytest.mark.unit
def test_create_invite() -> None:
    dir_ = _seed()
    r = _client(dir_, ctx=_admin()).post(
        "/users",
        json={"username": "jdoe", "email": "j@x", "roles": ["steward"], "send_invite": True},
    )
    assert r.status_code == 201, r.text
    assert r.json()["roles"] == ["steward"]


@pytest.mark.unit
def test_create_unknown_role_400() -> None:
    r = _client(_seed(), ctx=_admin()).post(
        "/users", json={"username": "x", "email": "x@x", "roles": ["wizard"]}
    )
    assert r.status_code == 400
    assert "wizard" in r.json()["details"]["unknown_roles"]


@pytest.mark.unit
def test_create_invite_requires_email_400() -> None:
    r = _client(_seed(), ctx=_admin()).post("/users", json={"username": "x", "send_invite": True})
    assert r.status_code == 400


@pytest.mark.unit
def test_create_duplicate_409() -> None:
    r = _client(_seed(), ctx=_admin()).post(
        "/users", json={"username": "admin", "email": "a@x", "send_invite": False}
    )
    assert r.status_code == 409


# --- update / guards -------------------------------------------------------


@pytest.mark.unit
def test_update_roles_ok() -> None:
    r = _client(_seed(), ctx=_admin()).patch(
        f"/users/{STEWARD_ID}", json={"roles": ["steward", "admin"]}
    )
    assert r.status_code == 200
    assert set(r.json()["roles"]) == {"steward", "admin"}


@pytest.mark.unit
def test_update_unknown_role_400() -> None:
    r = _client(_seed(), ctx=_admin()).patch(f"/users/{STEWARD_ID}", json={"roles": ["wizard"]})
    assert r.status_code == 400


@pytest.mark.unit
def test_cannot_remove_own_admin() -> None:
    r = _client(_seed(), ctx=_admin()).patch(f"/users/{ADMIN_ID}", json={"roles": ["steward"]})
    assert r.status_code == 409
    assert "your own admin" in r.json()["message"]


@pytest.mark.unit
def test_cannot_self_disable() -> None:
    r = _client(_seed(), ctx=_admin()).patch(f"/users/{ADMIN_ID}", json={"enabled": False})
    assert r.status_code == 409


@pytest.mark.unit
def test_cannot_demote_last_admin() -> None:
    # An API-key admin (subject is not a realm user) demotes the sole realm admin.
    dir_ = _seed()
    key_admin = RequestContext(
        subject=f"{ISSUER}#svc-key", roles=frozenset({"admin"}), trace_id="t"
    )
    r = _client(dir_, ctx=key_admin).patch(f"/users/{ADMIN_ID}", json={"roles": ["steward"]})
    assert r.status_code == 409
    assert "last admin" in r.json()["message"]


# --- delete ----------------------------------------------------------------


@pytest.mark.unit
def test_delete_ok() -> None:
    dir_ = _seed()
    assert _client(dir_, ctx=_admin()).delete(f"/users/{STEWARD_ID}").status_code == 204
    assert STEWARD_ID not in dir_.users


@pytest.mark.unit
def test_cannot_delete_self() -> None:
    assert _client(_seed(), ctx=_admin()).delete(f"/users/{ADMIN_ID}").status_code == 409


@pytest.mark.unit
def test_cannot_delete_last_admin() -> None:
    dir_ = _seed()
    key_admin = RequestContext(
        subject=f"{ISSUER}#svc-key", roles=frozenset({"admin"}), trace_id="t"
    )
    assert _client(dir_, ctx=key_admin).delete(f"/users/{ADMIN_ID}").status_code == 409


# --- user id validation (audit R-07) ----------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("method", ["get", "patch", "delete"])
def test_non_uuid_id_rejected_before_upstream(method: str) -> None:
    client = _client(_seed(), ctx=_admin())
    kwargs = {"json": {"roles": ["steward"]}} if method == "patch" else {}
    resp = client.request(method.upper(), "/users/not-a-uuid", **kwargs)
    assert resp.status_code == 400
    assert resp.json()["code"] == "fdp.bad_request"


# --- audit trail of mutations (audit R-11) ----------------------------------


@pytest.mark.unit
def test_mutations_emit_admin_action_audit_events() -> None:
    from fdpneo_server.shared.events import AdminActionAudited

    bus = _CapturingBus()
    dir_ = _seed()
    client = _client(dir_, ctx=_admin(), event_bus=cast(EventBus, bus))

    client.post("/users", json={"username": "jdoe", "email": "j@x", "roles": ["steward"]})
    client.patch(f"/users/{STEWARD_ID}", json={"roles": ["steward", "admin"]})
    client.delete(f"/users/{STEWARD_ID}")

    ops = [(e.operation, e.subject) for e in bus.events if isinstance(e, AdminActionAudited)]
    assert ("user_create", f"{ISSUER}#{ADMIN_ID}") in ops
    assert ("user_update", f"{ISSUER}#{ADMIN_ID}") in ops
    assert ("user_delete", f"{ISSUER}#{ADMIN_ID}") in ops
    # the delete event targets the deleted user id
    delete_ev = next(e for e in bus.events if e.operation == "user_delete")
    assert delete_ev.target == STEWARD_ID


@pytest.mark.unit
def test_rejected_mutation_emits_no_audit_event() -> None:
    bus = _CapturingBus()
    # self-lockout (409) must not produce an audit row.
    _client(_seed(), ctx=_admin(), event_bus=cast(EventBus, bus)).patch(
        f"/users/{ADMIN_ID}", json={"roles": ["steward"]}
    )
    assert bus.events == []
