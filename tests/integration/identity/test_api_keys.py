"""End-to-end API-key authentication against Postgres (+ Oxigraph) — Phase 11.1.

Unlike the other integration suites, this one does **not** override
``current_context`` — the whole point is to drive the *real* authentication
middleware so a ``Authorization: Bearer fdpk_…`` header is resolved against the
``api_keys`` table. Proves ADR-0011 end-to-end:

1. A valid key authenticates as its owner (a protected endpoint returns 200).
2. Roles resolve **live** from ``subject_principal``: a key minted with no roles
   gains admin capability the moment the owner's principal record is updated —
   no re-mint. (The admin-gated ``/me/dashboard?as_admin=true`` flips 403→200.)
3. Revoking the key makes it 401 on the next request.

Requires Docker (testcontainers). Marked ``integration``.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Coroutine, Iterator
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from testcontainers.community.postgres import PostgresContainer
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

OXIGRAPH_PORT = 7878
BASE_URL = "http://testserver"
SUBJECT = "http://idp.local/realms/fdp#svc-account"

pytestmark = pytest.mark.integration


PROFILE_MANIFEST = """\
apiVersion: fdp/v1
kind: DeploymentProfile
metadata:
  name: apikey-test
  version: 0.1.0
schemas:
  - id: dcat:Catalog
    path: schemas/catalog.ttl
offers:
  - id: system-default
    path: offers/public.ttl
    isSystemDefault: true
resourceDefinitions:
  - urlPrefix: ""
    name: Repository
    schema: dcat:Catalog
"""

CATALOG_SHAPE_TTL = """\
@prefix sh:   <http://www.w3.org/ns/shacl#> .
@prefix dcat: <http://www.w3.org/ns/dcat#> .
@prefix dct:  <http://purl.org/dc/terms/> .
<http://www.w3.org/ns/dcat#Catalog>
    a sh:NodeShape ; sh:targetClass dcat:Catalog ;
    sh:property [ sh:path dct:title ; sh:minCount 1 ] .
"""

OFFER_TTL = """\
@prefix odrl: <http://www.w3.org/ns/odrl/2/> .
<http://example.org/offers/public>
    a odrl:Offer ;
    odrl:permission [ a odrl:Permission ; odrl:action odrl:read ] .
"""


def _async_dsn(container: PostgresContainer) -> str:
    raw = container.get_connection_url()
    return raw.replace("postgresql+psycopg2://", "postgresql+asyncpg://").replace(
        "postgresql://", "postgresql+asyncpg://"
    )


@pytest.fixture
def postgres_container() -> Iterator[PostgresContainer]:
    with PostgresContainer("postgres:16-alpine") as container:
        yield container


@pytest.fixture
def oxigraph_container() -> Iterator[DockerContainer]:
    container = (
        DockerContainer("oxigraph/oxigraph:latest")
        .with_exposed_ports(OXIGRAPH_PORT)
        .with_command("serve --bind 0.0.0.0:7878 --location /data")
        .waiting_for(LogMessageWaitStrategy("Listening").with_startup_timeout(60))
    )
    with container:
        yield container


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    root = tmp_path / "profile"
    root.mkdir()
    (root / "profile.yaml").write_text(PROFILE_MANIFEST, encoding="utf-8")
    (root / "schemas").mkdir()
    (root / "schemas" / "catalog.ttl").write_text(CATALOG_SHAPE_TTL, encoding="utf-8")
    (root / "offers").mkdir()
    (root / "offers" / "public.ttl").write_text(OFFER_TTL, encoding="utf-8")
    return root


@pytest.fixture
def app_env(
    postgres_container: PostgresContainer,
    oxigraph_container: DockerContainer,
    bundle: Path,
) -> Iterator[str]:
    """Configure env, migrate, and yield the async DSN for out-of-band seeding."""
    from fdpneo_server.config import get_settings

    host = oxigraph_container.get_container_host_ip()
    port = oxigraph_container.get_exposed_port(OXIGRAPH_PORT)
    oxi = f"http://{host}:{port}"
    dsn = _async_dsn(postgres_container)
    env = {
        "POSTGRES_DSN": dsn,
        "FDP_TRIPLESTORE_QUERY_ENDPOINT": f"{oxi}/query",
        "FDP_TRIPLESTORE_UPDATE_ENDPOINT": f"{oxi}/update",
        "FDP_TRIPLESTORE_GRAPH_STORE_ENDPOINT": f"{oxi}/store",
        "FDP_OIDC_ISSUER": "http://idp.local/realms/fdp",
        "FDP_OIDC_AUDIENCE": "fdp",
        "BASE_URL": BASE_URL,
        "FDP_PROFILE_AUTO_APPLY": "true",
        "FDP_PROFILE_PATH": str(bundle),
    }
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    get_settings.cache_clear()
    config = Config(str(files("fdpneo_server") / "alembic.ini"))
    command.upgrade(config, "head")
    try:
        yield dsn
    finally:
        for key, prior in saved.items():
            if prior is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prior
        get_settings.cache_clear()


def _run(dsn: str, fn: Callable[[Any], Coroutine[Any, Any, None]]) -> None:
    """Run a one-shot async DB op against ``dsn`` on its own engine/loop."""

    async def _main() -> None:
        engine = create_async_engine(dsn, future=True)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await fn(factory)
        finally:
            await engine.dispose()

    asyncio.run(_main())


def _seed_key(dsn: str, token: str) -> None:
    from datetime import UTC, datetime

    from fdpneo_server.identity.api_keys import ApiKeyRepository, ApiKeyRow, hash_token

    async def _fn(factory: Any) -> None:
        await ApiKeyRepository(session_factory=factory).add(
            ApiKeyRow(
                id="key-1",
                owner_subject=SUBJECT,
                label="ci",
                key_hash=hash_token(token),
                display_prefix=f"{token[:13]}…{token[-4:]}",
                roles_json=[],  # no roles snapshot → starts unprivileged
                groups_json=[],
                created_at=datetime.now(UTC),
                expires_at=None,
                last_used_at=None,
                revoked_at=None,
            )
        )

    _run(dsn, _fn)


def _record_admin_principal(dsn: str) -> None:
    from fdpneo_server.identity.principal import SubjectPrincipalRepository

    async def _fn(factory: Any) -> None:
        await SubjectPrincipalRepository(session_factory=factory).record(
            SUBJECT, roles=frozenset({"admin"}), groups=frozenset()
        )

    _run(dsn, _fn)


def _make_client() -> TestClient:
    from fdpneo_server.main import create_app

    return TestClient(create_app(), base_url=BASE_URL)


def test_api_key_auth_live_roles_and_revoke(app_env: str) -> None:
    from fdpneo_server.identity.api_keys import generate_token

    dsn = app_env
    token = generate_token()
    _seed_key(dsn, token)
    auth = {"Authorization": f"Bearer {token}"}

    with _make_client() as client:
        # 1. The key authenticates as its owner — listing own keys needs auth.
        listed = client.get("/fdp-api/me/api-keys", headers=auth)
        assert listed.status_code == 200, listed.text
        assert any(k["id"] == "key-1" for k in listed.json()["keys"])

        # No header → anonymous → 401 on the same protected endpoint.
        assert client.get("/fdp-api/me/api-keys").status_code == 401

        # 2a. With no roles yet, the admin-gated dashboard view is forbidden.
        assert (
            client.get(
                "/fdp-api/me/dashboard", params={"as_admin": "true"}, headers=auth
            ).status_code
            == 403
        )

        # 2b. Grant admin via the principal record (as an interactive login
        # would) — the SAME key now resolves admin live, no re-mint.
        _record_admin_principal(dsn)
        assert (
            client.get(
                "/fdp-api/me/dashboard", params={"as_admin": "true"}, headers=auth
            ).status_code
            == 200
        )

        # 3. Owner self-revoke, then the key is rejected.
        assert client.delete("/fdp-api/me/api-keys/key-1", headers=auth).status_code == 204
        assert client.get("/fdp-api/me/api-keys", headers=auth).status_code == 401
