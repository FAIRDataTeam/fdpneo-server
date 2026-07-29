"""User-management admin facade (ADR-0013) — `/users`.

A thin, admin-scoped proxy over the IdP's user-admin operations so the client can
manage users and their FDP roles in-app without touching the IdP console. The FDP
keeps **no internal user store** (ADR-0001): identities live in the IdP and this
surface forwards to it through a :class:`UserDirectory` port (implemented by
:class:`fdpneo_server.identity.keycloak_admin.KeycloakUserDirectory`).

Scope is deliberately narrow — list/search, role changes, enable/disable, invite,
remove. Passwords, MFA, and federation stay in the IdP's own console. Creation is
**invite-only**: the IdP emails a set-password/verify link; no password ever
flows through this API.

The facade is **capability-gated**: when no IdP-admin service account is
configured, ``directory`` is ``None`` and every endpoint returns
``503 fdp.service_unavailable`` (and ``features.user_management`` is ``False``).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Final, Protocol

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from fdpneo_server.identity.deps import require_auth
from fdpneo_server.shared.context import RequestContext
from fdpneo_server.shared.errors import BadRequest, Conflict, Forbidden, ServiceUnavailable
from fdpneo_server.shared.events import AdminActionAudited

if TYPE_CHECKING:
    from fdpneo_server.shared.events import EventBus

_ADMIN_ROLE: Final = "admin"

# Stable audit operation codes for /users mutations (audit R-11). Mirror
# fdpneo_server.metadata.audit.AuditOperation.USER_* without importing across the boundary.
_OP_CREATE: Final = "user_create"
_OP_UPDATE: Final = "user_update"
_OP_DELETE: Final = "user_delete"

# IdP user ids are UUIDs (== the token `sub`). Validate the path param before it
# is interpolated into the Admin REST URL — defense-in-depth (audit R-07).
_UUID_RE: Final = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

# The curated set of FDP roles this facade manages and exposes via /users/roles.
# Intentionally NOT the full realm role list (which includes IdP machinery like
# offline_access / default-roles-*): only roles the PDP reads (ADR-0006).
ASSIGNABLE_ROLES: Final[tuple[str, ...]] = ("steward", "admin")


# --- models ----------------------------------------------------------------


class UserInfo(BaseModel):
    """An IdP user as the FDP admin surface sees it. ``id`` equals the token ``sub``."""

    id: str
    username: str
    email: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    roles: list[str] = []
    enabled: bool = True


class UserListView(BaseModel):
    users: list[UserInfo]
    total: int


class RolesView(BaseModel):
    roles: list[str]


class CreateUserRequest(BaseModel):
    username: str
    email: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    roles: list[str] = []
    enabled: bool = True
    send_invite: bool = True


class UpdateUserRequest(BaseModel):
    """Partial update. ``roles`` (when present) is the *full desired set*."""

    roles: list[str] | None = None
    enabled: bool | None = None
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None


# --- port ------------------------------------------------------------------


class UserDirectory(Protocol):
    """The IdP-backed operations the facade forwards to (ADR-0013).

    Implementations raise the standard :class:`fdpneo_server.shared.errors.FDPError`
    subclasses — ``NotFound`` (404), ``Conflict`` (409, dup username/email),
    ``BadRequest`` (400), ``UpstreamError`` (502, IdP call failed).
    """

    async def list_users(
        self, *, search: str | None, limit: int, offset: int
    ) -> tuple[list[UserInfo], int]: ...

    async def get_user(self, user_id: str) -> UserInfo: ...

    async def create_user(self, req: CreateUserRequest) -> UserInfo: ...

    async def update_user(self, user_id: str, req: UpdateUserRequest) -> UserInfo: ...

    async def delete_user(self, user_id: str) -> None: ...

    async def count_admins(self) -> int: ...


# --- router ----------------------------------------------------------------


def build_users_router(
    *,
    directory: UserDirectory | None,
    event_bus: EventBus | None = None,
    prefix: str = "/users",
) -> APIRouter:
    """Build the user-admin router. Every endpoint requires the ``admin`` role.

    ``directory`` is ``None`` when the IdP-admin facade is unconfigured — every
    route then returns ``503``. When ``event_bus`` is supplied, successful
    create/update/delete actions are mirrored into the FDP audit trail (R-11).
    """
    router = APIRouter(prefix=prefix, tags=["users"])

    async def _audit(operation: str, target: str, ctx: RequestContext) -> None:
        if event_bus is not None:
            await event_bus.publish(
                AdminActionAudited(
                    target=target,
                    operation=operation,
                    subject=ctx.subject,
                    timestamp=datetime.now(UTC),
                )
            )

    def _require_admin(ctx: RequestContext) -> None:
        if _ADMIN_ROLE not in ctx.roles:
            raise Forbidden(
                "admin role required to manage users",
                details={"required_role": _ADMIN_ROLE},
            )

    def _dir() -> UserDirectory:
        if directory is None:
            raise ServiceUnavailable(
                "user management is not enabled on this deployment",
                details={"feature": "user_management"},
            )
        return directory

    def _validate_roles(roles: list[str]) -> None:
        unknown = sorted(set(roles) - set(ASSIGNABLE_ROLES))
        if unknown:
            raise BadRequest(
                "unknown role(s); see GET /users/roles",
                details={"unknown_roles": unknown, "assignable": list(ASSIGNABLE_ROLES)},
            )

    def _require_uuid(user_id: str) -> None:
        if not _UUID_RE.match(user_id):
            raise BadRequest("user id must be a UUID", details={"id": user_id})

    def _caller_user_id(ctx: RequestContext) -> str:
        # ctx.subject is "{issuer}#{sub}"; the IdP user id == the sub.
        return (ctx.subject or "").rsplit("#", 1)[-1]

    async def _guard_not_self_lockout(
        ctx: RequestContext, user_id: str, *, removing_admin: bool, disabling: bool
    ) -> None:
        if user_id == _caller_user_id(ctx) and (removing_admin or disabling):
            raise Conflict(
                "cannot remove your own admin access",
                details={"user": user_id},
            )

    async def _guard_not_last_admin(
        directory: UserDirectory, current: UserInfo, *, removing_admin: bool, disabling: bool
    ) -> None:
        if (
            _ADMIN_ROLE in current.roles
            and (removing_admin or disabling)
            and await directory.count_admins() <= 1
        ):
            raise Conflict(
                "cannot demote or disable the last admin",
                details={"user": current.id},
            )

    @router.get("", response_model=UserListView, name="user_list")
    async def list_users(  # pyright: ignore[reportUnusedFunction]
        ctx: Annotated[RequestContext, Depends(require_auth)],
        search: str | None = None,
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ) -> UserListView:
        _require_admin(ctx)
        users, total = await _dir().list_users(search=search, limit=limit, offset=offset)
        return UserListView(users=users, total=total)

    @router.get("/roles", response_model=RolesView, name="user_roles")
    async def list_roles(  # pyright: ignore[reportUnusedFunction]
        ctx: Annotated[RequestContext, Depends(require_auth)],
    ) -> RolesView:
        _require_admin(ctx)
        _dir()  # 503 when unconfigured, for consistency with the rest of the surface
        return RolesView(roles=list(ASSIGNABLE_ROLES))

    @router.get("/{user_id}", response_model=UserInfo, name="user_get")
    async def get_user(  # pyright: ignore[reportUnusedFunction]
        user_id: str,
        ctx: Annotated[RequestContext, Depends(require_auth)],
    ) -> UserInfo:
        _require_admin(ctx)
        _require_uuid(user_id)
        return await _dir().get_user(user_id)

    @router.post("", response_model=UserInfo, status_code=201, name="user_create")
    async def create_user(  # pyright: ignore[reportUnusedFunction]
        body: CreateUserRequest,
        ctx: Annotated[RequestContext, Depends(require_auth)],
    ) -> UserInfo:
        _require_admin(ctx)
        if not body.username.strip():
            raise BadRequest("username is required")
        _validate_roles(body.roles)
        if body.send_invite and not (body.email or "").strip():
            raise BadRequest("email is required to send an invite")
        created = await _dir().create_user(body)
        await _audit(_OP_CREATE, created.id, ctx)
        return created

    @router.patch("/{user_id}", response_model=UserInfo, name="user_update")
    async def update_user(  # pyright: ignore[reportUnusedFunction]
        user_id: str,
        body: UpdateUserRequest,
        ctx: Annotated[RequestContext, Depends(require_auth)],
    ) -> UserInfo:
        _require_admin(ctx)
        _require_uuid(user_id)
        if body.roles is not None:
            _validate_roles(body.roles)
        directory = _dir()
        removing_admin = body.roles is not None and _ADMIN_ROLE not in body.roles
        disabling = body.enabled is False
        await _guard_not_self_lockout(
            ctx, user_id, removing_admin=removing_admin, disabling=disabling
        )
        if removing_admin or disabling:
            current = await directory.get_user(user_id)  # also 404s if missing
            await _guard_not_last_admin(
                directory, current, removing_admin=removing_admin, disabling=disabling
            )
        updated = await directory.update_user(user_id, body)
        await _audit(_OP_UPDATE, user_id, ctx)
        return updated

    @router.delete("/{user_id}", status_code=204, name="user_delete")
    async def delete_user(  # pyright: ignore[reportUnusedFunction]
        user_id: str,
        ctx: Annotated[RequestContext, Depends(require_auth)],
    ) -> None:
        _require_admin(ctx)
        _require_uuid(user_id)
        directory = _dir()
        # Deleting yourself, or the last admin, locks the deployment out.
        await _guard_not_self_lockout(ctx, user_id, removing_admin=True, disabling=True)
        current = await directory.get_user(user_id)  # 404s if missing
        await _guard_not_last_admin(directory, current, removing_admin=True, disabling=True)
        await directory.delete_user(user_id)
        await _audit(_OP_DELETE, user_id, ctx)

    return router


__all__ = [
    "ASSIGNABLE_ROLES",
    "CreateUserRequest",
    "RolesView",
    "UpdateUserRequest",
    "UserDirectory",
    "UserInfo",
    "UserListView",
    "build_users_router",
]
