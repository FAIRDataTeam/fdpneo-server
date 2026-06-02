"""Unit tests for API keys (Phase 11.1, ADR-0011).

Token/hash helpers, the repository + service over in-memory SQLite, and the
``/me/api-keys`` router. The headline behaviours: a key is shown once and stored
only as a hash; authentication resolves the owner's *live* roles from
``subject_principal`` (snapshot only as fallback); expiry/revocation/limits are
enforced; and reads/writes are owner-scoped with admin revoke override.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fdp.config import ApiKeySettings
from fdp.identity.api_keys import (
    TOKEN_PREFIX,
    ApiKeyRepository,
    ApiKeyRow,
    ApiKeyService,
    build_api_keys_router,
    generate_token,
    hash_token,
)
from fdp.identity.deps import current_context
from fdp.identity.principal import SubjectPrincipalRepository
from fdp.shared.context import RequestContext
from fdp.shared.errors import BadRequest, Forbidden, NotFound, register_exception_handlers
from fdp.storage.postgres.models import Base, register_all_models

NOW = datetime(2026, 6, 2, 12, 0, tzinfo=UTC)
ALICE = "https://idp/realms/fdp#alice"
BOB = "https://idp/realms/fdp#bob"


# --- fixtures --------------------------------------------------------------


@pytest.fixture
async def session_factory() -> Any:
    register_all_models()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _ctx(subject: str | None, *, roles: frozenset[str] = frozenset()) -> RequestContext:
    return RequestContext(subject=subject, roles=roles, trace_id="t", request_timestamp=NOW)


def _service(
    session_factory: Any, *, settings: ApiKeySettings | None = None, clock: Any = None
) -> ApiKeyService:
    return ApiKeyService(
        repository=ApiKeyRepository(session_factory=session_factory),
        principals=SubjectPrincipalRepository(session_factory=session_factory),
        settings=settings or ApiKeySettings(),
        clock=clock or (lambda: NOW),
    )


# --- token helpers ---------------------------------------------------------


@pytest.mark.unit
def test_token_format_and_hash() -> None:
    t1 = generate_token()
    t2 = generate_token()
    assert t1.startswith(TOKEN_PREFIX)
    assert t1 != t2
    assert hash_token(t1) == hash_token(t1)  # deterministic
    assert hash_token(t1) != hash_token(t2)
    assert len(hash_token(t1)) == 64  # sha256 hex


# --- service: mint ---------------------------------------------------------


@pytest.mark.unit
async def test_mint_returns_token_once_and_snapshots_roles(session_factory: Any) -> None:
    svc = _service(session_factory)
    result = await svc.mint(_ctx(ALICE, roles=frozenset({"steward"})), label="ci", expires_at=None)
    assert result.token.startswith(TOKEN_PREFIX)
    assert result.info.roles == ["steward"]
    assert result.info.active is True
    assert result.info.display_prefix.startswith(TOKEN_PREFIX)
    # The plaintext is not recoverable from storage — only its hash is kept.
    row = await ApiKeyRepository(session_factory=session_factory).get_by_hash(
        hash_token(result.token)
    )
    assert row is not None
    assert row.key_hash == hash_token(result.token)


@pytest.mark.unit
async def test_mint_enforces_max_per_user(session_factory: Any) -> None:
    svc = _service(session_factory, settings=ApiKeySettings(max_per_user=2))
    ctx = _ctx(ALICE)
    await svc.mint(ctx, label="a", expires_at=None)
    await svc.mint(ctx, label="b", expires_at=None)
    with pytest.raises(BadRequest):
        await svc.mint(ctx, label="c", expires_at=None)


@pytest.mark.unit
async def test_revoked_keys_do_not_count_toward_limit(session_factory: Any) -> None:
    svc = _service(session_factory, settings=ApiKeySettings(max_per_user=1))
    ctx = _ctx(ALICE)
    first = await svc.mint(ctx, label="a", expires_at=None)
    await svc.revoke(ctx, first.info.id)
    # Now under the limit again.
    await svc.mint(ctx, label="b", expires_at=None)


@pytest.mark.unit
async def test_mint_rejects_past_expiry_and_over_cap(session_factory: Any) -> None:
    svc = _service(session_factory, settings=ApiKeySettings(max_ttl_days=7))
    with pytest.raises(BadRequest):
        await svc.mint(_ctx(ALICE), label="x", expires_at=NOW - timedelta(days=1))
    with pytest.raises(BadRequest):
        await svc.mint(_ctx(ALICE), label="x", expires_at=NOW + timedelta(days=30))
    # Within the cap is fine.
    ok = await svc.mint(_ctx(ALICE), label="x", expires_at=NOW + timedelta(days=3))
    assert ok.info.expires_at == NOW + timedelta(days=3)


@pytest.mark.unit
async def test_uncapped_no_expiry_is_allowed(session_factory: Any) -> None:
    svc = _service(session_factory, settings=ApiKeySettings(max_ttl_days=None))
    result = await svc.mint(_ctx(ALICE), label="forever", expires_at=None)
    assert result.info.expires_at is None


# --- service: authenticate -------------------------------------------------


@pytest.mark.unit
async def test_authenticate_resolves_live_roles_then_snapshot_fallback(
    session_factory: Any,
) -> None:
    svc = _service(session_factory)
    principals = SubjectPrincipalRepository(session_factory=session_factory)
    minted = await svc.mint(_ctx(ALICE, roles=frozenset({"steward"})), label="ci", expires_at=None)

    # mint seeded the principal with {steward}; auth reflects it.
    ctx = await svc.authenticate(minted.token, trace_id="x")
    assert ctx is not None
    assert ctx.subject == ALICE
    assert ctx.roles == frozenset({"steward"})

    # Role change at the IdP (recorded on a later login) is reflected live.
    await principals.record(ALICE, roles=frozenset({"admin"}), groups=frozenset())
    ctx2 = await svc.authenticate(minted.token, trace_id="x")
    assert ctx2 is not None
    assert ctx2.roles == frozenset({"admin"})


@pytest.mark.unit
async def test_authenticate_falls_back_to_snapshot_without_principal(
    session_factory: Any,
) -> None:
    # Insert a key row directly so no principal record exists for its owner.
    repo = ApiKeyRepository(session_factory=session_factory)
    token = generate_token()
    await repo.add(
        ApiKeyRow(
            id="k1",
            owner_subject=BOB,
            label="legacy",
            key_hash=hash_token(token),
            display_prefix="fdpk_xx…yy",
            roles_json=["viewer"],
            groups_json=[],
            created_at=NOW,
            expires_at=None,
            last_used_at=None,
            revoked_at=None,
        )
    )
    svc = _service(session_factory)
    ctx = await svc.authenticate(token, trace_id="x")
    assert ctx is not None
    assert ctx.roles == frozenset({"viewer"})  # snapshot fallback


@pytest.mark.unit
async def test_authenticate_rejects_unknown_revoked_expired_and_disabled(
    session_factory: Any,
) -> None:
    svc = _service(session_factory)
    ctx = _ctx(ALICE)

    assert await svc.authenticate("fdpk_unknown", trace_id="x") is None
    assert await svc.authenticate("not-an-api-key", trace_id="x") is None

    revoked = await svc.mint(ctx, label="r", expires_at=None)
    await svc.revoke(ctx, revoked.info.id)
    assert await svc.authenticate(revoked.token, trace_id="x") is None

    # Expired: clock advanced past expiry.
    expiring = await svc.mint(ctx, label="e", expires_at=NOW + timedelta(days=1))
    later = _service(session_factory, clock=lambda: NOW + timedelta(days=2))
    assert await later.authenticate(expiring.token, trace_id="x") is None

    # Feature disabled.
    off = _service(session_factory, settings=ApiKeySettings(enabled=False))
    live = await svc.mint(ctx, label="ok", expires_at=None)
    assert await off.authenticate(live.token, trace_id="x") is None


@pytest.mark.unit
async def test_last_used_is_recorded(session_factory: Any) -> None:
    svc = _service(session_factory)
    minted = await svc.mint(_ctx(ALICE), label="ci", expires_at=None)
    await svc.authenticate(minted.token, trace_id="x")
    row = await ApiKeyRepository(session_factory=session_factory).get_by_hash(
        hash_token(minted.token)
    )
    assert row is not None
    assert row.last_used_at is not None


# --- service: revoke -------------------------------------------------------


@pytest.mark.unit
async def test_revoke_owner_admin_stranger(session_factory: Any) -> None:
    svc = _service(session_factory)
    minted = await svc.mint(_ctx(ALICE), label="ci", expires_at=None)

    # Stranger cannot revoke.
    with pytest.raises(Forbidden):
        await svc.revoke(_ctx(BOB), minted.info.id)
    # Admin (different subject) can revoke any key.
    await svc.revoke(_ctx(BOB, roles=frozenset({"admin"})), minted.info.id)
    # Unknown id → NotFound.
    with pytest.raises(NotFound):
        await svc.revoke(_ctx(ALICE), "missing")


# --- router ----------------------------------------------------------------


def _client(svc: ApiKeyService, *, ctx: RequestContext) -> TestClient:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(build_api_keys_router(service=svc))
    app.dependency_overrides[current_context] = lambda: ctx
    return TestClient(app)


@pytest.mark.unit
def test_router_anonymous_rejected(session_factory: Any) -> None:
    client = _client(_service(session_factory), ctx=RequestContext.anonymous(trace_id="t"))
    assert client.post("/me/api-keys", json={"label": "x"}).status_code == 401
    assert client.get("/me/api-keys").status_code == 401


@pytest.mark.unit
def test_router_create_list_revoke_flow(session_factory: Any) -> None:
    client = _client(_service(session_factory), ctx=_ctx(ALICE, roles=frozenset({"steward"})))
    created = client.post("/me/api-keys", json={"label": "ci"})
    assert created.status_code == 201
    body = created.json()
    assert body["key"].startswith(TOKEN_PREFIX)  # plaintext, once
    key_id = body["id"]

    listed = client.get("/me/api-keys").json()["keys"]
    assert len(listed) == 1
    assert "key" not in listed[0]  # secret never in the list view
    assert listed[0]["id"] == key_id

    assert client.delete(f"/me/api-keys/{key_id}").status_code == 204
    assert client.get("/me/api-keys").json()["keys"][0]["active"] is False


@pytest.mark.unit
def test_router_disabled_returns_404(session_factory: Any) -> None:
    svc = _service(session_factory, settings=ApiKeySettings(enabled=False))
    client = _client(svc, ctx=_ctx(ALICE))
    assert client.post("/me/api-keys", json={"label": "x"}).status_code == 404
    assert client.get("/me/api-keys").status_code == 404
