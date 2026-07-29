"""End-to-end user-management facade against a real Keycloak (ADR-0013).

Imports the bundled dev realm (``deploy/keycloak/realm-fdp-dev.json``), which
ships the ``fdp-server`` confidential service-account client (granted
``manage-users``/``view-users`` on ``realm-management``) and the ``admin`` /
``steward`` realm roles that :data:`ASSIGNABLE_ROLES` expects. Exercises
:class:`KeycloakUserDirectory` against the live Admin REST API — proving the
adapter works against real Keycloak response shapes, not just respx mocks:
``client_credentials`` token, create → assign role → list (role annotation) →
get → update (role reconciliation) → delete, plus 404→NotFound mapping.

Requires Docker (testcontainers). Marked ``integration``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
from pydantic import HttpUrl, SecretStr
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

from fdpneo_server.config import IdpAdminSettings, OIDCSettings
from fdpneo_server.identity.keycloak_admin import KeycloakUserDirectory
from fdpneo_server.identity.users import CreateUserRequest, UpdateUserRequest
from fdpneo_server.shared.errors import NotFound

REPO_ROOT = Path(__file__).resolve().parents[3]
REALM_FILE = REPO_ROOT / "deploy" / "keycloak" / "realm-fdp-dev.json"
KEYCLOAK_PORT = 8080

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def keycloak() -> Iterator[DockerContainer]:
    """A Keycloak that boots with the bundled fdp-dev realm imported."""
    container = (
        DockerContainer("quay.io/keycloak/keycloak:25.0")
        .with_exposed_ports(KEYCLOAK_PORT)
        .with_env("KEYCLOAK_ADMIN", "admin")
        .with_env("KEYCLOAK_ADMIN_PASSWORD", "admin")
        .with_volume_mapping(str(REALM_FILE), "/opt/keycloak/data/import/realm-fdp-dev.json", "ro")
        .with_command("start-dev --import-realm")
        # "Listening on:" is logged once boot + realm import have completed.
        .waiting_for(LogMessageWaitStrategy("Listening on:").with_startup_timeout(180))
    )
    with container:
        yield container


@pytest.fixture
async def directory(keycloak: DockerContainer) -> AsyncIterator[KeycloakUserDirectory]:
    host = keycloak.get_container_host_ip()
    port = keycloak.get_exposed_port(KEYCLOAK_PORT)
    base = f"http://{host}:{port}"
    oidc = OIDCSettings(  # type: ignore[call-arg]
        issuer=HttpUrl(f"{base}/realms/fdp-dev"),
        audience="fdp",
    )
    idp = IdpAdminSettings(
        client_id="fdp-server",
        client_secret=SecretStr("fdp-server-dev-secret"),
        base_url=HttpUrl(base),
    )
    async with httpx.AsyncClient(timeout=30.0) as http:
        directory = KeycloakUserDirectory.from_settings(idp_admin=idp, oidc=oidc, http_client=http)
        assert directory is not None, "facade should be enabled when client creds are set"
        yield directory


async def test_user_lifecycle_against_real_keycloak(directory: KeycloakUserDirectory) -> None:
    created = await directory.create_user(
        CreateUserRequest(
            username="it-alice",
            email="it-alice@example.org",
            first_name="Alice",
            last_name="Tester",
            roles=["steward"],
            send_invite=False,  # no SMTP in the container
        )
    )
    try:
        assert created.username == "it-alice"
        assert created.roles == ["steward"]

        # The page is annotated with each user's FDP roles (two role-member calls).
        users, total = await directory.list_users(search="it-alice", limit=20, offset=0)
        assert total >= 1
        listed = next(u for u in users if u.id == created.id)
        assert listed.roles == ["steward"]

        got = await directory.get_user(created.id)
        assert got.email == "it-alice@example.org"

        # Reconcile the role set steward → admin, and rename.
        updated = await directory.update_user(
            created.id, UpdateUserRequest(roles=["admin"], last_name="Renamed")
        )
        assert set(updated.roles) == {"admin"}
        assert updated.last_name == "Renamed"
        assert await directory.count_admins() >= 1
    finally:
        await directory.delete_user(created.id)

    with pytest.raises(NotFound):
        await directory.get_user(created.id)


async def test_get_unknown_user_maps_to_not_found(directory: KeycloakUserDirectory) -> None:
    with pytest.raises(NotFound):
        await directory.get_user("00000000-0000-0000-0000-000000000000")
