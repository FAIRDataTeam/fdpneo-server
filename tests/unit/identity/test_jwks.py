"""Unit tests for ``fdpneo_server.identity.jwks``."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from fdpneo_server.identity.jwks import (
    JWKSClient,
    JWKSFetchError,
    UnknownSigningKey,
)
from tests.unit.identity.conftest import IdPFixture, TokenSigner

TTL = timedelta(seconds=300)


def _client(http_client: httpx.AsyncClient) -> JWKSClient:
    fixed_time = datetime(2026, 1, 1, tzinfo=UTC)
    return JWKSClient(
        issuer=IdPFixture().issuer,
        http_client=http_client,
        cache_ttl=TTL,
        clock=lambda: fixed_time,
    )


@pytest.fixture
def async_client() -> httpx.AsyncClient:
    return httpx.AsyncClient()


@pytest.mark.unit
@respx.mock
async def test_get_signing_key_fetches_discovery_then_jwks(
    async_client: httpx.AsyncClient,
    idp: IdPFixture,
) -> None:
    respx.get(idp.discovery_uri).respond(200, json=idp.discovery_doc())
    respx.get(idp.jwks_uri).respond(200, json=idp.jwks())

    jwks = _client(async_client)
    key = await jwks.get_signing_key("primary")
    assert key is not None


@pytest.mark.unit
@respx.mock
async def test_cache_hit_within_ttl_makes_no_new_requests(
    async_client: httpx.AsyncClient,
    idp: IdPFixture,
) -> None:
    disc = respx.get(idp.discovery_uri).respond(200, json=idp.discovery_doc())
    jks = respx.get(idp.jwks_uri).respond(200, json=idp.jwks())

    jwks = _client(async_client)
    await jwks.get_signing_key("primary")
    await jwks.get_signing_key("primary")

    assert disc.call_count == 1
    assert jks.call_count == 1


@pytest.mark.unit
@respx.mock
async def test_unknown_kid_triggers_rotation_refresh_and_succeeds(
    async_client: httpx.AsyncClient,
    idp: IdPFixture,
    second_signer: TokenSigner,
) -> None:
    respx.get(idp.discovery_uri).respond(200, json=idp.discovery_doc())
    initial = {"keys": [idp.signers[0].jwk()]}
    rotated = {"keys": [idp.signers[0].jwk(), second_signer.jwk()]}
    jks = respx.get(idp.jwks_uri).mock(
        side_effect=[
            httpx.Response(200, json=initial),
            httpx.Response(200, json=rotated),
        ]
    )

    jwks = _client(async_client)
    await jwks.get_signing_key("primary")  # warms cache
    key = await jwks.get_signing_key(second_signer.kid)  # forces refresh
    assert key is not None
    assert jks.call_count == 2


@pytest.mark.unit
@respx.mock
async def test_unknown_kid_after_refresh_raises(
    async_client: httpx.AsyncClient,
    idp: IdPFixture,
) -> None:
    respx.get(idp.discovery_uri).respond(200, json=idp.discovery_doc())
    respx.get(idp.jwks_uri).respond(200, json=idp.jwks())

    jwks = _client(async_client)
    with pytest.raises(UnknownSigningKey):
        await jwks.get_signing_key("nope")


@pytest.mark.unit
@respx.mock
async def test_discovery_non_200_raises_fetch_error(
    async_client: httpx.AsyncClient,
    idp: IdPFixture,
) -> None:
    respx.get(idp.discovery_uri).respond(503, text="unavailable")

    jwks = _client(async_client)
    with pytest.raises(JWKSFetchError):
        await jwks.get_signing_key("primary")


@pytest.mark.unit
@respx.mock
async def test_concurrent_cold_cache_fetches_only_once(
    async_client: httpx.AsyncClient,
    idp: IdPFixture,
) -> None:
    disc = respx.get(idp.discovery_uri).respond(200, json=idp.discovery_doc())
    jks = respx.get(idp.jwks_uri).respond(200, json=idp.jwks())

    jwks = _client(async_client)
    await asyncio.gather(*(jwks.get_signing_key("primary") for _ in range(8)))

    assert disc.call_count == 1
    assert jks.call_count == 1
