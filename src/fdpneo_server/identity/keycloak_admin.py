"""Keycloak Admin REST adapter for the `/users` facade (ADR-0013).

Implements the :class:`fdpneo_server.identity.users.UserDirectory` port against Keycloak's
Admin REST API, authenticating as a confidential **service-account** client via
the OAuth2 ``client_credentials`` grant. The access token is cached until just
before expiry (mirroring :class:`fdpneo_server.identity.jwks.JWKSClient`) and refreshed on
a ``401``.

Endpoint shapes (Keycloak Admin REST, realm ``{realm}``):

* ``GET    /users?search&first&max``        — page of user representations
* ``GET    /users/count?search``            — total for pagination
* ``GET|PUT|DELETE /users/{id}``            — single user CRUD
* ``POST   /users``                         — create (id in ``Location``)
* ``GET    /roles/{name}``                  — a realm-role representation
* ``GET    /roles/{name}/users``            — members of a realm role
* ``GET|POST|DELETE /users/{id}/role-mappings/realm`` — a user's realm roles
* ``PUT    /users/{id}/execute-actions-email`` — invite (set-password/verify)

Errors are mapped to the standard envelope: 404→``NotFound``, 409→``Conflict``,
400→``BadRequest``, everything else (incl. transport / 5xx / auth failures of the
service account itself) →``UpstreamError`` (502).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

import httpx
import structlog

from fdpneo_server.identity.users import (
    ASSIGNABLE_ROLES,
    CreateUserRequest,
    UpdateUserRequest,
    UserInfo,
)
from fdpneo_server.shared.errors import BadRequest, Conflict, NotFound, UpstreamError

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Callable

    from fdpneo_server.config import IdpAdminSettings, OIDCSettings

log = structlog.get_logger(__name__)

_INVITE_ACTIONS: Final = ["UPDATE_PASSWORD", "VERIFY_EMAIL"]
# Members lookups are unpaged here; fine for the realm sizes this facade targets.
_ROLE_MEMBERS_MAX: Final = 2000


# --- token client ----------------------------------------------------------


class KeycloakAdminTokenClient:
    """Caches a ``client_credentials`` access token until just before expiry."""

    def __init__(
        self,
        *,
        token_url: str,
        client_id: str,
        client_secret: str,
        http_client: httpx.AsyncClient,
        leeway_seconds: int = 30,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._token_url = token_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = http_client
        self._leeway = leeway_seconds
        self._clock = clock or (lambda: datetime.now(UTC))
        self._cached: tuple[str, datetime] | None = None
        self._lock: asyncio.Lock | None = None

    async def token(self) -> str:
        cached = self._cached
        if cached is not None and cached[1] > self._clock():
            return cached[0]
        lock = self._ensure_lock()
        async with lock:
            cached = self._cached
            if cached is not None and cached[1] > self._clock():
                return cached[0]
            return await self._fetch()

    def invalidate(self) -> None:
        self._cached = None

    def _ensure_lock(self) -> asyncio.Lock:
        # Created lazily so the client can be constructed off the event loop.
        import asyncio

        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def _fetch(self) -> str:
        try:
            resp = await self._http.post(
                self._token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
            )
        except httpx.HTTPError as exc:
            raise UpstreamError(f"IdP token request failed: {exc}") from exc
        if resp.status_code >= 400:
            raise UpstreamError(f"IdP token endpoint returned HTTP {resp.status_code}")
        payload: dict[str, Any] = resp.json()
        access = payload.get("access_token")
        if not isinstance(access, str):
            raise UpstreamError("IdP token response missing access_token")
        expires_in = payload.get("expires_in", 60)
        ttl = max(int(expires_in) - self._leeway, 5) if isinstance(expires_in, int) else 30
        self._cached = (access, self._clock() + timedelta(seconds=ttl))
        return access


# --- directory adapter -----------------------------------------------------


class KeycloakUserDirectory:
    """:class:`UserDirectory` backed by the Keycloak Admin REST API."""

    def __init__(
        self,
        *,
        admin_base: str,
        token_client: KeycloakAdminTokenClient,
        http_client: httpx.AsyncClient,
    ) -> None:
        self._base = admin_base.rstrip("/")
        self._token = token_client
        self._http = http_client

    @classmethod
    def from_settings(
        cls,
        *,
        idp_admin: IdpAdminSettings,
        oidc: OIDCSettings,
        http_client: httpx.AsyncClient,
    ) -> KeycloakUserDirectory | None:
        """Build the directory, or ``None`` when the facade is unconfigured."""
        if not idp_admin.enabled:
            return None
        issuer = str(oidc.issuer).rstrip("/")
        kc_base = (
            str(idp_admin.base_url).rstrip("/")
            if idp_admin.base_url
            else issuer.split("/realms/")[0]
        )
        realm = idp_admin.realm or issuer.split("/realms/")[-1]
        assert idp_admin.client_id is not None and idp_admin.client_secret is not None  # enabled
        token_client = KeycloakAdminTokenClient(
            token_url=f"{issuer}/protocol/openid-connect/token",
            client_id=idp_admin.client_id,
            client_secret=idp_admin.client_secret.get_secret_value(),
            http_client=http_client,
            leeway_seconds=idp_admin.token_cache_leeway_seconds,
        )
        return cls(
            admin_base=f"{kc_base}/admin/realms/{realm}",
            token_client=token_client,
            http_client=http_client,
        )

    # --- UserDirectory ------------------------------------------------------

    async def list_users(
        self, *, search: str | None, limit: int, offset: int
    ) -> tuple[list[UserInfo], int]:
        params: dict[str, Any] = {"first": offset, "max": limit, "briefRepresentation": False}
        if search:
            params["search"] = search
        reps = (await self._get("/users", params=params)).json()
        # Two calls annotate the whole page with FDP roles, regardless of page size.
        members = {role: await self._role_member_ids(role) for role in ASSIGNABLE_ROLES}
        users = [
            _to_user_info(rep, [r for r in ASSIGNABLE_ROLES if rep.get("id") in members[r]])
            for rep in reps
        ]
        total = await self._count(search)
        return users, total

    async def get_user(self, user_id: str) -> UserInfo:
        rep = (await self._get(f"/users/{user_id}")).json()
        roles = await self._user_realm_roles(user_id)
        return _to_user_info(rep, roles)

    async def create_user(self, req: CreateUserRequest) -> UserInfo:
        body: dict[str, Any] = {
            "username": req.username,
            "enabled": req.enabled,
            "email": req.email,
            "firstName": req.first_name,
            "lastName": req.last_name,
        }
        resp = await self._request("POST", "/users", json=_drop_none(body))
        user_id = _id_from_location(resp)
        for role in req.roles:
            await self._add_realm_role(user_id, role)
        if req.send_invite:
            await self._request(
                "PUT", f"/users/{user_id}/execute-actions-email", json=_INVITE_ACTIONS
            )
        return await self.get_user(user_id)

    async def update_user(self, user_id: str, req: UpdateUserRequest) -> UserInfo:
        current = (await self._get(f"/users/{user_id}")).json()  # 404s if missing
        patch: dict[str, Any] = {}
        if req.enabled is not None:
            patch["enabled"] = req.enabled
        if req.first_name is not None:
            patch["firstName"] = req.first_name
        if req.last_name is not None:
            patch["lastName"] = req.last_name
        if req.email is not None:
            patch["email"] = req.email
        if patch:
            await self._request("PUT", f"/users/{user_id}", json={**current, **patch})
        if req.roles is not None:
            await self._reconcile_roles(user_id, desired=set(req.roles))
        return await self.get_user(user_id)

    async def delete_user(self, user_id: str) -> None:
        await self._request("DELETE", f"/users/{user_id}")

    async def count_admins(self) -> int:
        return len(await self._role_member_ids("admin"))

    # --- internals ----------------------------------------------------------

    async def _reconcile_roles(self, user_id: str, *, desired: set[str]) -> None:
        desired &= set(ASSIGNABLE_ROLES)
        current = set(await self._user_realm_roles(user_id))
        for role in desired - current:
            await self._add_realm_role(user_id, role)
        for role in (current - desired) & set(ASSIGNABLE_ROLES):
            await self._remove_realm_role(user_id, role)

    async def _user_realm_roles(self, user_id: str) -> list[str]:
        reps = (await self._get(f"/users/{user_id}/role-mappings/realm")).json()
        return [name for rep in reps if (name := rep.get("name")) in ASSIGNABLE_ROLES]

    async def _role_member_ids(self, role: str) -> set[str]:
        reps = (await self._get(f"/roles/{role}/users", params={"max": _ROLE_MEMBERS_MAX})).json()
        return {rep["id"] for rep in reps if "id" in rep}

    async def _role_rep(self, role: str) -> dict[str, Any]:
        return (await self._get(f"/roles/{role}")).json()

    async def _add_realm_role(self, user_id: str, role: str) -> None:
        await self._request(
            "POST", f"/users/{user_id}/role-mappings/realm", json=[await self._role_rep(role)]
        )

    async def _remove_realm_role(self, user_id: str, role: str) -> None:
        await self._request(
            "DELETE", f"/users/{user_id}/role-mappings/realm", json=[await self._role_rep(role)]
        )

    async def _count(self, search: str | None) -> int:
        params = {"search": search} if search else None
        return int((await self._get("/users/count", params=params)).json())

    async def _get(self, path: str, *, params: dict[str, Any] | None = None) -> httpx.Response:
        return await self._request("GET", path, params=params)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> httpx.Response:
        resp = await self._send(method, path, params, json)
        if resp.status_code == 401:
            # Token may have been revoked/rotated; refresh once and retry.
            self._token.invalidate()
            resp = await self._send(method, path, params, json)
        _raise_for_status(resp, method, path)
        return resp

    async def _send(
        self, method: str, path: str, params: dict[str, Any] | None, json: Any
    ) -> httpx.Response:
        token = await self._token.token()
        try:
            return await self._http.request(
                method,
                f"{self._base}{path}",
                params=params,
                json=json,
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as exc:
            raise UpstreamError(f"IdP admin request failed: {exc}") from exc


# --- helpers ---------------------------------------------------------------


def _to_user_info(rep: dict[str, Any], roles: list[str]) -> UserInfo:
    return UserInfo(
        id=str(rep.get("id", "")),
        username=str(rep.get("username", "")),
        email=rep.get("email"),
        first_name=rep.get("firstName"),
        last_name=rep.get("lastName"),
        roles=roles,
        enabled=bool(rep.get("enabled", True)),
    )


def _drop_none(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}


def _id_from_location(resp: httpx.Response) -> str:
    location = resp.headers.get("Location", "")
    user_id = location.rstrip("/").rsplit("/", 1)[-1]
    if not user_id:
        raise UpstreamError("IdP did not return the created user id")
    return user_id


def _raise_for_status(resp: httpx.Response, method: str, path: str) -> None:
    if resp.status_code < 400:
        return
    detail = _kc_message(resp)
    if resp.status_code == 404:
        raise NotFound(detail or "user not found", details={"path": path})
    if resp.status_code == 409:
        raise Conflict(detail or "username or email already exists", details={"path": path})
    if resp.status_code == 400:
        raise BadRequest(detail or "the IdP rejected the request", details={"path": path})
    raise UpstreamError(
        f"IdP admin {method} {path} returned HTTP {resp.status_code}",
        details={"status": resp.status_code, "detail": detail},
    )


def _kc_message(resp: httpx.Response) -> str | None:
    try:
        body: dict[str, Any] = resp.json()
    except Exception:
        return None
    for key in ("errorMessage", "error_description", "error"):
        value = body.get(key)
        if isinstance(value, str) and value:
            return value
    return None


__all__ = ["KeycloakAdminTokenClient", "KeycloakUserDirectory"]
