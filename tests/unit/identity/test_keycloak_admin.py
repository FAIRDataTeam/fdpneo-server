"""Unit tests for the Keycloak Admin adapter (ADR-0013) over a mocked IdP."""

from __future__ import annotations

import httpx
import pytest
import respx

from fdp.identity.keycloak_admin import KeycloakAdminTokenClient, KeycloakUserDirectory
from fdp.identity.users import CreateUserRequest, UpdateUserRequest
from fdp.shared.errors import Conflict, NotFound, UpstreamError

KC = "http://kc"
TOKEN_URL = f"{KC}/realms/fdp/protocol/openid-connect/token"
ADMIN = f"{KC}/admin/realms/fdp"


def _directory(http: httpx.AsyncClient) -> tuple[KeycloakUserDirectory, KeycloakAdminTokenClient]:
    token = KeycloakAdminTokenClient(
        token_url=TOKEN_URL, client_id="fdp-server", client_secret="s", http_client=http
    )
    return KeycloakUserDirectory(admin_base=ADMIN, token_client=token, http_client=http), token


def _mock_token() -> respx.Route:
    return respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 300})
    )


# --- token client ----------------------------------------------------------


@pytest.mark.unit
@respx.mock
async def test_token_is_cached_and_refreshes_on_invalidate() -> None:
    route = _mock_token()
    async with httpx.AsyncClient() as http:
        _, token = _directory(http)
        assert await token.token() == "tok"
        await token.token()
        assert route.call_count == 1  # cached
        token.invalidate()
        await token.token()
        assert route.call_count == 2


@pytest.mark.unit
@respx.mock
async def test_401_triggers_token_refresh_and_retry() -> None:
    _mock_token()
    users = respx.get(f"{ADMIN}/users/u1").mock(
        side_effect=[
            httpx.Response(401),
            httpx.Response(200, json={"id": "u1", "username": "a", "enabled": True}),
        ]
    )
    respx.get(f"{ADMIN}/users/u1/role-mappings/realm").mock(
        return_value=httpx.Response(200, json=[])
    )
    async with httpx.AsyncClient() as http:
        directory, _ = _directory(http)
        result = await directory.get_user("u1")
    assert result.id == "u1"
    assert users.call_count == 2  # retried after the 401


# --- reads / mapping -------------------------------------------------------


@pytest.mark.unit
@respx.mock
async def test_get_user_filters_roles_to_fdp_set() -> None:
    _mock_token()
    respx.get(f"{ADMIN}/users/u1").mock(
        return_value=httpx.Response(
            200,
            json={"id": "u1", "username": "alice", "email": "al@x", "enabled": True},
        )
    )
    respx.get(f"{ADMIN}/users/u1/role-mappings/realm").mock(
        return_value=httpx.Response(
            200, json=[{"name": "steward"}, {"name": "offline_access"}, {"name": "admin"}]
        )
    )
    async with httpx.AsyncClient() as http:
        directory, _ = _directory(http)
        user = await directory.get_user("u1")
    assert user.username == "alice"
    assert set(user.roles) == {"steward", "admin"}  # offline_access dropped


# --- update / role diff ----------------------------------------------------


@pytest.mark.unit
@respx.mock
async def test_update_reconciles_roles_add_and_remove() -> None:
    _mock_token()
    respx.get(f"{ADMIN}/users/u1").mock(
        return_value=httpx.Response(200, json={"id": "u1", "username": "a", "enabled": True})
    )
    respx.get(f"{ADMIN}/users/u1/role-mappings/realm").mock(
        side_effect=[
            httpx.Response(200, json=[{"name": "steward"}]),  # current (reconcile)
            httpx.Response(200, json=[{"name": "admin"}]),  # final get_user
        ]
    )
    respx.get(f"{ADMIN}/roles/steward").mock(
        return_value=httpx.Response(200, json={"id": "r-s", "name": "steward"})
    )
    respx.get(f"{ADMIN}/roles/admin").mock(
        return_value=httpx.Response(200, json={"id": "r-a", "name": "admin"})
    )
    add = respx.post(f"{ADMIN}/users/u1/role-mappings/realm").mock(httpx.Response(204))
    remove = respx.delete(f"{ADMIN}/users/u1/role-mappings/realm").mock(httpx.Response(204))

    async with httpx.AsyncClient() as http:
        directory, _ = _directory(http)
        result = await directory.update_user("u1", UpdateUserRequest(roles=["admin"]))

    assert add.called and remove.called
    assert result.roles == ["admin"]


# --- create / invite -------------------------------------------------------


@pytest.mark.unit
@respx.mock
async def test_create_assigns_roles_and_sends_invite() -> None:
    _mock_token()
    respx.post(f"{ADMIN}/users").mock(
        return_value=httpx.Response(201, headers={"Location": f"{ADMIN}/users/new-1"})
    )
    respx.get(f"{ADMIN}/roles/steward").mock(
        return_value=httpx.Response(200, json={"id": "r-s", "name": "steward"})
    )
    assign = respx.post(f"{ADMIN}/users/new-1/role-mappings/realm").mock(httpx.Response(204))
    invite = respx.put(f"{ADMIN}/users/new-1/execute-actions-email").mock(httpx.Response(204))
    respx.get(f"{ADMIN}/users/new-1").mock(
        return_value=httpx.Response(200, json={"id": "new-1", "username": "jdoe", "enabled": True})
    )
    respx.get(f"{ADMIN}/users/new-1/role-mappings/realm").mock(
        return_value=httpx.Response(200, json=[{"name": "steward"}])
    )

    async with httpx.AsyncClient() as http:
        directory, _ = _directory(http)
        result = await directory.create_user(
            CreateUserRequest(username="jdoe", email="j@x", roles=["steward"], send_invite=True)
        )

    assert assign.called and invite.called
    assert result.id == "new-1" and result.roles == ["steward"]


# --- error mapping ---------------------------------------------------------


@pytest.mark.unit
@respx.mock
async def test_error_mapping() -> None:
    _mock_token()
    respx.get(f"{ADMIN}/users/missing").mock(
        return_value=httpx.Response(404, json={"error": "User not found"})
    )
    respx.post(f"{ADMIN}/users").mock(
        return_value=httpx.Response(409, json={"errorMessage": "User exists with same username"})
    )
    respx.get(f"{ADMIN}/users/boom").mock(return_value=httpx.Response(500, text="kaboom"))

    async with httpx.AsyncClient() as http:
        directory, _ = _directory(http)
        with pytest.raises(NotFound):
            await directory.get_user("missing")
        with pytest.raises(Conflict):
            await directory.create_user(
                CreateUserRequest(username="dup", email="d@x", send_invite=False)
            )
        with pytest.raises(UpstreamError):
            await directory.get_user("boom")


# --- from_settings ---------------------------------------------------------


@pytest.mark.unit
async def test_from_settings_gating_and_derivation() -> None:
    from fdp.config import IdpAdminSettings, OIDCSettings

    oidc = OIDCSettings(issuer="http://localhost:8080/realms/fdp-dev", audience="fdp")  # type: ignore[arg-type]
    async with httpx.AsyncClient() as http:
        # Unconfigured → None.
        assert (
            KeycloakUserDirectory.from_settings(
                idp_admin=IdpAdminSettings(client_id=None, client_secret=None),
                oidc=oidc,
                http_client=http,
            )
            is None
        )
        # Configured → admin base derived from the issuer.
        directory = KeycloakUserDirectory.from_settings(
            idp_admin=IdpAdminSettings(client_id="fdp-server", client_secret="s"),  # type: ignore[arg-type]
            oidc=oidc,
            http_client=http,
        )
        assert directory is not None
        assert directory._base == "http://localhost:8080/admin/realms/fdp-dev"
