"""Unit tests for the `/users` admin facade router (ADR-0013).

The router is driven over a FastAPI app with a fake :class:`UserDirectory`, so
these cover the HTTP behaviour the server owns: admin gating, status codes,
validation, the self-lockout / last-admin guards, and the feature-off (503) path.
The Keycloak adapter is tested separately in ``test_keycloak_admin.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fdp.identity.users import (
    CreateUserRequest,
    UpdateUserRequest,
    UserInfo,
    build_users_router,
)
from fdp.shared.context import RequestContext
from fdp.shared.errors import Conflict, NotFound

ISSUER = "http://idp.local/realms/fdp"
ADMIN_ID = "admin-1"


# --- fake directory --------------------------------------------------------


@dataclass
class _FakeDirectory:
    users: dict[str, UserInfo] = field(default_factory=dict)

    async def list_users(self, *, search, limit, offset):
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
            "steward-1": UserInfo(
                id="steward-1", username="alice", email="al@x", roles=["steward"]
            ),
        }
    )


# --- client harness --------------------------------------------------------


def _client(directory, *, ctx: RequestContext) -> TestClient:
    from fdp.identity.deps import current_context
    from fdp.shared.errors import register_exception_handlers

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(build_users_router(directory=directory))
    app.dependency_overrides[current_context] = lambda: ctx
    return TestClient(app)


def _admin() -> RequestContext:
    return RequestContext(
        subject=f"{ISSUER}#{ADMIN_ID}", roles=frozenset({"admin", "steward"}), trace_id="t"
    )


def _steward() -> RequestContext:
    return RequestContext(subject=f"{ISSUER}#steward-1", roles=frozenset({"steward"}), trace_id="t")


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
    assert c.get("/users/nope").status_code == 404


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
        "/users/steward-1", json={"roles": ["steward", "admin"]}
    )
    assert r.status_code == 200
    assert set(r.json()["roles"]) == {"steward", "admin"}


@pytest.mark.unit
def test_update_unknown_role_400() -> None:
    r = _client(_seed(), ctx=_admin()).patch("/users/steward-1", json={"roles": ["wizard"]})
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
    assert _client(dir_, ctx=_admin()).delete("/users/steward-1").status_code == 204
    assert "steward-1" not in dir_.users


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
