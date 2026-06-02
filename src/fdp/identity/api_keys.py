"""API keys — model, repository, service, and the ``/me/api-keys`` router (Phase 11.1).

A key is a long-lived bearer credential for an existing OIDC subject (ADR-0011):
it mints no new identity, is shown once at creation, and is stored only as a
``sha256`` hash. Authentication resolves the owner's *live* roles from the
``subject_principal`` record (falling back to the mint-time snapshot), so a
long-lived key tracks the owner's current authorization rather than freezing it.

Surfaces, all under ``/me/api-keys`` and requiring authentication:

* ``POST``         — mint a key; returns the plaintext token **once**.
* ``GET``          — list the caller's keys (metadata only; never the secret).
* ``DELETE /{id}`` — revoke a key. The owner, or any admin, may revoke.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Any, Final

import structlog
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import JSON, String, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from fdp.identity.deps import require_auth
from fdp.shared.context import RequestContext
from fdp.shared.errors import BadRequest, Forbidden, NotFound
from fdp.storage.postgres.models import Base
from fdp.storage.postgres.types import AwareDateTime

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from fdp.config import ApiKeySettings
    from fdp.identity.principal import SubjectPrincipalRepository

log = structlog.get_logger(__name__)

_ADMIN_ROLE: Final = "admin"
TOKEN_PREFIX: Final = "fdpk_"  # noqa: S105 (a public, non-secret prefix marker)
"""Every API-key token starts with this; the middleware dispatches on it."""

_SECRET_BYTES: Final = 32  # ~190 bits of entropy
_TOUCH_THROTTLE: Final = timedelta(seconds=60)


# --- token helpers ---------------------------------------------------------


def generate_token() -> str:
    """Return a fresh ``fdpk_…`` token (shown to the user once)."""
    return f"{TOKEN_PREFIX}{secrets.token_urlsafe(_SECRET_BYTES)}"


def hash_token(token: str) -> str:
    """Return the stored ``sha256`` hash of ``token`` (64 hex chars).

    A fast hash is correct for high-entropy keys (ADR-0011): the lookup is an
    indexed equality, and brute-forcing ~190 bits is infeasible.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _display_prefix(token: str) -> str:
    """A non-secret label for the list view, e.g. ``fdpk_Ab3dEf12…wxyz``."""
    return f"{token[:13]}…{token[-4:]}"


# --- model -----------------------------------------------------------------


class ApiKeyRow(Base):
    """One issued API key."""

    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_subject: Mapped[str] = mapped_column(String(2048), nullable=False)
    label: Mapped[str] = mapped_column(String(256), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    display_prefix: Mapped[str] = mapped_column(String(32), nullable=False)
    roles_json: Mapped[list[str]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=False
    )
    groups_json: Mapped[list[str]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(AwareDateTime(), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(AwareDateTime(), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(AwareDateTime(), nullable=True)


# --- repository ------------------------------------------------------------


class ApiKeyRepository:
    """Async CRUD over ``api_keys``."""

    def __init__(self, *, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def add(self, row: ApiKeyRow) -> None:
        async with self._session_factory() as session:
            session.add(row)
            await session.commit()

    async def get(self, key_id: str) -> ApiKeyRow | None:
        async with self._session_factory() as session:
            return await session.get(ApiKeyRow, key_id)

    async def get_by_hash(self, key_hash: str) -> ApiKeyRow | None:
        async with self._session_factory() as session:
            stmt = select(ApiKeyRow).where(ApiKeyRow.key_hash == key_hash)
            return (await session.execute(stmt)).scalar_one_or_none()

    async def list_for_owner(self, subject: str) -> list[ApiKeyRow]:
        async with self._session_factory() as session:
            stmt = (
                select(ApiKeyRow)
                .where(ApiKeyRow.owner_subject == subject)
                .order_by(ApiKeyRow.created_at.desc())
            )
            return list((await session.execute(stmt)).scalars().all())

    async def count_active_for_owner(self, subject: str) -> int:
        async with self._session_factory() as session:
            stmt = (
                select(func.count())
                .select_from(ApiKeyRow)
                .where(
                    ApiKeyRow.owner_subject == subject,
                    ApiKeyRow.revoked_at.is_(None),
                )
            )
            return int((await session.execute(stmt)).scalar_one())

    async def set_revoked(self, key_id: str, *, when: datetime) -> None:
        async with self._session_factory() as session:
            row = await session.get(ApiKeyRow, key_id)
            if row is None or row.revoked_at is not None:
                return
            row.revoked_at = when
            await session.commit()

    async def touch_last_used(self, key_id: str, *, when: datetime) -> None:
        async with self._session_factory() as session:
            row = await session.get(ApiKeyRow, key_id)
            if row is None:
                return
            row.last_used_at = when
            await session.commit()


# --- DTOs ------------------------------------------------------------------


class ApiKeyCreateRequest(BaseModel):
    """Body for ``POST /me/api-keys``."""

    label: str = Field(min_length=1, max_length=256)
    expires_at: datetime | None = None


class ApiKeyInfo(BaseModel):
    """Public (secret-free) view of a key."""

    id: str
    label: str
    display_prefix: str
    roles: list[str]
    groups: list[str]
    created_at: datetime
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None
    active: bool


class ApiKeyCreated(ApiKeyInfo):
    """``POST`` response — carries the plaintext token exactly once."""

    key: str


class ApiKeyList(BaseModel):
    keys: list[ApiKeyInfo]


# --- service ---------------------------------------------------------------


@dataclass(frozen=True)
class MintResult:
    token: str
    info: ApiKeyInfo


class ApiKeyService:
    """Mint / list / revoke keys and authenticate ``fdpk_`` bearer tokens."""

    def __init__(
        self,
        *,
        repository: ApiKeyRepository,
        principals: SubjectPrincipalRepository,
        settings: ApiKeySettings,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repo = repository
        self._principals = principals
        self._settings = settings
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def enabled(self) -> bool:
        return self._settings.enabled

    async def mint(
        self, ctx: RequestContext, *, label: str, expires_at: datetime | None
    ) -> MintResult:
        """Issue a key for ``ctx``'s subject, snapshotting its roles/groups."""
        if ctx.subject is None:  # defensive; the router requires auth
            raise Forbidden("authentication required to mint an API key")
        now = self._clock()
        expires_at = self._validate_expiry(expires_at, now=now)
        active = await self._repo.count_active_for_owner(ctx.subject)
        if active >= self._settings.max_per_user:
            raise BadRequest(
                "maximum number of active API keys reached",
                details={"max_per_user": self._settings.max_per_user},
            )
        token = generate_token()
        roles = sorted(ctx.roles)
        groups = sorted(ctx.groups)
        row = ApiKeyRow(
            id=str(uuid.uuid4()),
            owner_subject=ctx.subject,
            label=label,
            key_hash=hash_token(token),
            display_prefix=_display_prefix(token),
            roles_json=roles,
            groups_json=groups,
            created_at=now,
            expires_at=expires_at,
            last_used_at=None,
            revoked_at=None,
        )
        await self._repo.add(row)
        # Minting carries a fresh JWT, so seed the principal record now — the
        # key reflects the owner's current roles from its very first use.
        await self._safe_record_principal(ctx)
        log.info("api_key_minted", key_id=row.id, subject=ctx.subject, label=label)
        return MintResult(token=token, info=self._view(row, now=now))

    async def list_for(self, ctx: RequestContext) -> list[ApiKeyInfo]:
        if ctx.subject is None:
            return []
        now = self._clock()
        rows = await self._repo.list_for_owner(ctx.subject)
        return [self._view(row, now=now) for row in rows]

    async def revoke(self, ctx: RequestContext, key_id: str) -> None:
        """Revoke ``key_id``. The owner or any admin may do so."""
        row = await self._repo.get(key_id)
        if row is None:
            raise NotFound(f"no API key: {key_id}")
        is_admin = _ADMIN_ROLE in ctx.roles
        if row.owner_subject != ctx.subject and not is_admin:
            raise Forbidden("only the key's owner or an admin may revoke it")
        await self._repo.set_revoked(key_id, when=self._clock())
        log.info(
            "api_key_revoked",
            key_id=key_id,
            by=ctx.subject,
            owner=row.owner_subject,
            admin=is_admin,
        )

    async def authenticate(self, token: str, *, trace_id: str) -> RequestContext | None:
        """Resolve a ``fdpk_`` token to a context, or ``None`` if it is not valid.

        Returns ``None`` (→ 401 at the middleware) for an unknown, revoked,
        expired, or feature-disabled key — without disclosing which. Genuine
        infrastructure errors propagate.
        """
        if not self._settings.enabled or not token.startswith(TOKEN_PREFIX):
            return None
        row = await self._repo.get_by_hash(hash_token(token))
        if row is None or row.revoked_at is not None:
            return None
        now = self._clock()
        if row.expires_at is not None and row.expires_at <= now:
            return None
        roles, groups = await self._resolve_principal(row)
        await self._maybe_touch(row, now=now)
        return RequestContext(
            subject=row.owner_subject,
            roles=roles,
            groups=groups,
            trace_id=trace_id,
        )

    # --- internals ---------------------------------------------------------

    def _validate_expiry(self, expires_at: datetime | None, *, now: datetime) -> datetime | None:
        cap_days = self._settings.max_ttl_days
        cap = now + timedelta(days=cap_days) if cap_days is not None else None
        if expires_at is None:
            return cap  # None when uncapped → non-expiring key
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at <= now:
            raise BadRequest("expires_at must be in the future")
        if cap is not None and expires_at > cap:
            raise BadRequest(
                "expires_at exceeds the maximum allowed key lifetime",
                details={"max_ttl_days": cap_days},
            )
        return expires_at

    async def _resolve_principal(self, row: ApiKeyRow) -> tuple[frozenset[str], frozenset[str]]:
        """Live roles from ``subject_principal``, else the mint-time snapshot."""
        principal = await self._principals.get(row.owner_subject)
        if principal is not None:
            return principal.roles, principal.groups
        return frozenset(_as_str_list(row.roles_json)), frozenset(_as_str_list(row.groups_json))

    async def _maybe_touch(self, row: ApiKeyRow, *, now: datetime) -> None:
        if row.last_used_at is not None and now - row.last_used_at < _TOUCH_THROTTLE:
            return
        try:
            await self._repo.touch_last_used(row.id, when=now)
        except Exception as err:  # best-effort; never fail auth on a touch
            log.warning("api_key_touch_failed", key_id=row.id, error=repr(err))

    async def _safe_record_principal(self, ctx: RequestContext) -> None:
        if ctx.subject is None:
            return
        try:
            await self._principals.record(ctx.subject, roles=ctx.roles, groups=ctx.groups)
        except Exception as err:  # best-effort seed
            log.warning("principal_seed_failed", subject=ctx.subject, error=repr(err))

    def _view(self, row: ApiKeyRow, *, now: datetime) -> ApiKeyInfo:
        active = row.revoked_at is None and (row.expires_at is None or row.expires_at > now)
        return ApiKeyInfo(
            id=row.id,
            label=row.label,
            display_prefix=row.display_prefix,
            roles=_as_str_list(row.roles_json),
            groups=_as_str_list(row.groups_json),
            created_at=row.created_at,
            expires_at=row.expires_at,
            last_used_at=row.last_used_at,
            revoked_at=row.revoked_at,
            active=active,
        )


def _as_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [x for x in value if isinstance(x, str)]


# --- router ----------------------------------------------------------------


def build_api_keys_router(*, service: ApiKeyService) -> APIRouter:
    """Construct the ``/me/api-keys`` router. All routes require authentication."""
    router = APIRouter(tags=["api-keys"])

    def _require_enabled() -> None:
        if not service.enabled:
            raise NotFound("API keys are not enabled on this deployment")

    @router.post(
        "/me/api-keys", response_model=ApiKeyCreated, status_code=201, name="api_key_create"
    )
    async def create_key(  # pyright: ignore[reportUnusedFunction]
        body: ApiKeyCreateRequest,
        ctx: Annotated[RequestContext, Depends(require_auth)],
    ) -> ApiKeyCreated:
        """Mint a key. The plaintext token is in the response **once** — store it now."""
        _require_enabled()
        result = await service.mint(ctx, label=body.label, expires_at=body.expires_at)
        return ApiKeyCreated(key=result.token, **result.info.model_dump())

    @router.get("/me/api-keys", response_model=ApiKeyList, name="api_key_list")
    async def list_keys(  # pyright: ignore[reportUnusedFunction]
        ctx: Annotated[RequestContext, Depends(require_auth)],
    ) -> ApiKeyList:
        _require_enabled()
        return ApiKeyList(keys=await service.list_for(ctx))

    @router.delete("/me/api-keys/{key_id}", status_code=204, name="api_key_revoke")
    async def revoke_key(  # pyright: ignore[reportUnusedFunction]
        key_id: str,
        ctx: Annotated[RequestContext, Depends(require_auth)],
    ) -> None:
        _require_enabled()
        await service.revoke(ctx, key_id)

    return router


__all__ = [
    "TOKEN_PREFIX",
    "ApiKeyCreateRequest",
    "ApiKeyCreated",
    "ApiKeyInfo",
    "ApiKeyList",
    "ApiKeyRepository",
    "ApiKeyRow",
    "ApiKeyService",
    "MintResult",
    "build_api_keys_router",
    "generate_token",
    "hash_token",
]
