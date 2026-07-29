"""Middleware behaviour for API keys + principal recording (Phase 11.1, ADR-0011).

Two concerns: the ``fdpk_`` prefix dispatch (an API-key bearer is resolved via
the authenticator, not the JWT path), and the throttled ``subject_principal``
upsert that runs on JWT logins so a long-lived key tracks the owner's roles.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from typing import Any, cast

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import HttpUrl

from fdpneo_server.config import OIDCSettings
from fdpneo_server.identity.jwks import JWKSClient
from fdpneo_server.identity.middleware import AuthenticationMiddleware
from fdpneo_server.shared.context import RequestContext, get_current
from tests.unit.identity.conftest import AUDIENCE, ISSUER, IdPFixture

TTL = timedelta(seconds=300)
APIKEY = "fdpk_secrettoken"


class _FakeAuthenticator:
    def __init__(self, mapping: dict[str, RequestContext]) -> None:
        self._mapping = mapping
        self.calls: list[str] = []

    async def authenticate(self, token: str, *, trace_id: str) -> RequestContext | None:
        self.calls.append(token)
        base = self._mapping.get(token)
        if base is None:
            return None
        return RequestContext(
            subject=base.subject, roles=base.roles, groups=base.groups, trace_id=trace_id
        )


class _FakeRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, frozenset[str], frozenset[str]]] = []

    async def record(self, subject: str, *, roles: frozenset[str], groups: frozenset[str]) -> None:
        self.calls.append((subject, roles, groups))


def _whoami_app(
    *,
    jwks_provider: Any,
    authenticator: _FakeAuthenticator | None = None,
    recorder: _FakeRecorder | None = None,
) -> FastAPI:
    app = FastAPI()
    app.add_middleware(
        AuthenticationMiddleware,
        oidc=OIDCSettings(issuer=HttpUrl(ISSUER), audience=AUDIENCE),
        jwks_client_provider=jwks_provider,
        api_key_authenticator_provider=(lambda: authenticator) if authenticator else None,
        principal_recorder_provider=(lambda: recorder) if recorder else None,
    )

    @app.get("/whoami")
    async def whoami() -> dict[str, object]:  # pyright: ignore[reportUnusedFunction]
        ctx = get_current()
        assert ctx is not None
        return {"subject": ctx.subject, "roles": sorted(ctx.roles)}

    return app


# --- fdpk_ dispatch (no JWT needed) ----------------------------------------


@pytest.mark.unit
def test_valid_api_key_authenticates_via_authenticator() -> None:
    auth = _FakeAuthenticator(
        {APIKEY: RequestContext(subject="svc#1", roles=frozenset({"steward"}), trace_id="")}
    )
    recorder = _FakeRecorder()
    client = TestClient(
        _whoami_app(jwks_provider=lambda: cast(Any, None), authenticator=auth, recorder=recorder)
    )
    resp = client.get("/whoami", headers={"Authorization": f"Bearer {APIKEY}"})
    assert resp.status_code == 200
    assert resp.json() == {"subject": "svc#1", "roles": ["steward"]}
    # Principal recording is a JWT-login concern; the API-key path never records.
    assert recorder.calls == []


@pytest.mark.unit
def test_invalid_api_key_is_401() -> None:
    auth = _FakeAuthenticator({})  # nothing resolves
    client = TestClient(_whoami_app(jwks_provider=lambda: cast(Any, None), authenticator=auth))
    resp = client.get("/whoami", headers={"Authorization": "Bearer fdpk_nope"})
    assert resp.status_code == 401
    assert resp.json()["code"] == "fdp.unauthenticated"


@pytest.mark.unit
def test_api_key_when_feature_unwired_is_401() -> None:
    # No authenticator provider → fdpk_ tokens are rejected, not treated as JWTs.
    client = TestClient(_whoami_app(jwks_provider=lambda: cast(Any, None)))
    resp = client.get("/whoami", headers={"Authorization": f"Bearer {APIKEY}"})
    assert resp.status_code == 401


# --- JWT path records the principal (throttled) ----------------------------


@pytest.fixture
def jwt_client_and_recorder(idp: IdPFixture) -> Iterator[tuple[TestClient, _FakeRecorder]]:
    with respx.mock(assert_all_called=False) as router:
        router.get(idp.discovery_uri).respond(200, json=idp.discovery_doc())
        router.get(idp.jwks_uri).respond(200, json=idp.jwks())
        jwks_client = JWKSClient(issuer=idp.issuer, http_client=httpx.AsyncClient(), cache_ttl=TTL)
        recorder = _FakeRecorder()
        app = _whoami_app(jwks_provider=lambda: jwks_client, recorder=recorder)
        yield TestClient(app), recorder


@pytest.mark.unit
def test_jwt_login_records_principal_and_throttles(
    jwt_client_and_recorder: tuple[TestClient, _FakeRecorder], idp: IdPFixture
) -> None:
    client, recorder = jwt_client_and_recorder
    token = idp.token(sub="alice", roles=["steward"])
    hdr = {"Authorization": f"Bearer {token}"}

    assert client.get("/whoami", headers=hdr).status_code == 200
    assert len(recorder.calls) == 1
    subject, roles, _ = recorder.calls[0]
    assert subject == f"{ISSUER}#alice"
    assert roles == frozenset({"steward"})

    # Identical login is throttled — no second write.
    client.get("/whoami", headers=hdr)
    assert len(recorder.calls) == 1


@pytest.mark.unit
def test_role_change_records_immediately(
    jwt_client_and_recorder: tuple[TestClient, _FakeRecorder], idp: IdPFixture
) -> None:
    client, recorder = jwt_client_and_recorder
    client.get(
        "/whoami", headers={"Authorization": f"Bearer {idp.token(sub='alice', roles=['steward'])}"}
    )
    assert len(recorder.calls) == 1
    # A login whose roles changed records again right away (reflects the change).
    client.get(
        "/whoami", headers={"Authorization": f"Bearer {idp.token(sub='alice', roles=['admin'])}"}
    )
    assert len(recorder.calls) == 2
    assert recorder.calls[1][1] == frozenset({"admin"})
