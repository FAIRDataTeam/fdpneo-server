"""Unit tests for the OIDC authentication ASGI middleware."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import HttpUrl

from fdpneo_server.config import OIDCSettings
from fdpneo_server.identity.jwks import JWKSClient
from fdpneo_server.identity.middleware import AuthenticationMiddleware
from fdpneo_server.shared.context import get_current
from tests.unit.identity.conftest import AUDIENCE, ISSUER, IdPFixture, TokenSigner

TTL = timedelta(seconds=300)


@pytest.fixture
def app_factory(idp: IdPFixture) -> Iterator[FastAPI]:
    """Yield an app with the middleware installed and a one-route surface.

    The route reflects what the middleware put on the ContextVar so we can
    assert against it from the client side.
    """
    with respx.mock(assert_all_called=False) as router:
        router.get(idp.discovery_uri).respond(200, json=idp.discovery_doc())
        router.get(idp.jwks_uri).respond(200, json=idp.jwks())

        http_client = httpx.AsyncClient()
        jwks_client = JWKSClient(
            issuer=idp.issuer,
            http_client=http_client,
            cache_ttl=TTL,
        )
        oidc = OIDCSettings(
            issuer=HttpUrl(idp.issuer),
            audience=AUDIENCE,
        )

        app = FastAPI()
        app.add_middleware(
            AuthenticationMiddleware,
            oidc=oidc,
            jwks_client_provider=lambda: jwks_client,
        )

        @app.get("/whoami")
        async def whoami() -> dict[str, object]:  # pyright: ignore[reportUnusedFunction]
            ctx = get_current()
            assert ctx is not None
            return {
                "subject": ctx.subject,
                "roles": sorted(ctx.roles),
                "trace_id": ctx.trace_id,
                "is_anonymous": ctx.is_anonymous,
            }

        yield app


@pytest.fixture
def client(app_factory: FastAPI) -> TestClient:
    return TestClient(app_factory)


@pytest.mark.unit
def test_no_authorization_header_is_anonymous(client: TestClient) -> None:
    response = client.get("/whoami")
    assert response.status_code == 200
    body = response.json()
    assert body["is_anonymous"] is True
    assert body["subject"] is None
    assert body["roles"] == []
    assert body["trace_id"]


@pytest.mark.unit
def test_valid_bearer_yields_authenticated_context(
    client: TestClient,
    idp: IdPFixture,
) -> None:
    token = idp.token(sub="alice", roles=["steward", "viewer"])
    response = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    body = response.json()
    assert body["is_anonymous"] is False
    assert body["subject"] == f"{ISSUER}#alice"
    assert body["roles"] == ["steward", "viewer"]


@pytest.mark.unit
def test_expired_token_is_rejected(client: TestClient, idp: IdPFixture) -> None:
    token = idp.token(exp_offset=-60)
    response = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
    assert response.json()["code"] == "fdp.unauthenticated"


@pytest.mark.unit
def test_wrong_audience_is_rejected(client: TestClient, idp: IdPFixture) -> None:
    token = idp.token(aud="some-other-service")
    response = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


@pytest.mark.unit
def test_wrong_issuer_is_rejected(client: TestClient, idp: IdPFixture) -> None:
    token = idp.token(iss="http://attacker.example/realms/x")
    response = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


@pytest.mark.unit
def test_bad_signature_is_rejected(
    client: TestClient,
    idp: IdPFixture,
    second_signer: TokenSigner,
) -> None:
    # Token signed by an unknown key whose kid is announced as 'primary'.
    rogue = TokenSigner(kid="primary", private_key=second_signer.private_key)
    fake_idp = IdPFixture(signers=[rogue])
    token = fake_idp.token(sub="alice")

    response = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


@pytest.mark.unit
def test_unknown_kid_that_refresh_cannot_resolve_is_rejected(
    client: TestClient,
    idp: IdPFixture,
    second_signer: TokenSigner,
) -> None:
    token = idp.token(sub="alice")  # signed by 'primary'
    # Swap the kid in the header to a kid the IdP doesn't know.
    parts = token.split(".")
    import base64
    import json as _json

    header = _json.loads(base64.urlsafe_b64decode(parts[0] + "=="))
    header["kid"] = second_signer.kid
    parts[0] = base64.urlsafe_b64encode(_json.dumps(header).encode()).rstrip(b"=").decode()
    forged = ".".join(parts)

    response = client.get("/whoami", headers={"Authorization": f"Bearer {forged}"})
    assert response.status_code == 401


@pytest.mark.unit
def test_basic_authorization_scheme_is_rejected(client: TestClient) -> None:
    response = client.get("/whoami", headers={"Authorization": "Basic dXNlcjpwYXNz"})
    assert response.status_code == 401
    assert response.json()["code"] == "fdp.unauthenticated"


@pytest.mark.unit
def test_empty_bearer_token_is_rejected(client: TestClient) -> None:
    response = client.get("/whoami", headers={"Authorization": "Bearer "})
    assert response.status_code == 401


@pytest.mark.unit
def test_missing_roles_claim_yields_empty_roles(
    client: TestClient,
    idp: IdPFixture,
) -> None:
    token = idp.token(sub="alice")  # no roles=
    response = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    body = response.json()
    assert body["is_anonymous"] is False
    assert body["roles"] == []


@pytest.mark.unit
def test_nested_roles_claim_is_resolved(client: TestClient, idp: IdPFixture) -> None:
    token = idp.token(
        sub="alice",
        roles=["steward"],
        roles_path=("realm_access", "roles"),
    )
    response = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    body = response.json()
    assert body["roles"] == ["steward"]
