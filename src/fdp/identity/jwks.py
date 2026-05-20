"""OIDC discovery + JWKS fetch and cache.

**Responsibilities**

* Resolve the IdP's JWKS URI from its OIDC discovery document
  (``<issuer>/.well-known/openid-configuration``).
* Cache the JWKS for the configured TTL and refresh on miss (key rotation).

**Non-responsibilities**

* JWT validation. The middleware feeds the returned public key into
  :func:`jwt.decode`.
* Owning the :class:`httpx.AsyncClient`. The lifespan handler creates one
  and passes it in; the client is closed on shutdown.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

import httpx
import structlog
from jwt import PyJWK

if TYPE_CHECKING:
    from collections.abc import Callable

    from fdp.config import OIDCSettings

log = structlog.get_logger(__name__)


class JWKSError(Exception):
    """Base class for failures while resolving the IdP's signing keys."""


class JWKSFetchError(JWKSError):
    """Discovery doc or JWKS endpoint returned a non-2xx response or timed out."""


class UnknownSigningKey(JWKSError):
    """The requested ``kid`` is not present in the JWKS, even after refresh."""


@dataclass(frozen=True)
class _Cache:
    keys: dict[str, PyJWK]
    expires_at: datetime


class JWKSClient:
    """Async JWKS fetcher with TTL cache and rotation-on-miss."""

    def __init__(
        self,
        *,
        issuer: str,
        http_client: httpx.AsyncClient,
        cache_ttl: timedelta,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._issuer = issuer.rstrip("/")
        self._http = http_client
        self._ttl = cache_ttl
        self._clock = clock or (lambda: datetime.now(UTC))
        self._cache: _Cache | None = None
        self._lock = asyncio.Lock()

    @property
    def discovery_url(self) -> str:
        return f"{self._issuer}/.well-known/openid-configuration"

    async def get_signing_key(self, kid: str) -> PyJWK:
        """Return the :class:`PyJWK` for ``kid``, refreshing the cache if needed."""
        cache = self._cache
        if cache is not None and cache.expires_at > self._clock() and kid in cache.keys:
            return cache.keys[kid]

        async with self._lock:
            # Re-check inside the lock so concurrent waiters don't refetch.
            cache = self._cache
            if cache is None or cache.expires_at <= self._clock():
                cache = await self._refresh()

            if kid in cache.keys:
                return cache.keys[kid]

            # kid not in cache — single rotation attempt, then give up.
            log.info("jwks_kid_miss_refreshing", kid=kid)
            cache = await self._refresh()

        if kid not in cache.keys:
            raise UnknownSigningKey(kid)
        return cache.keys[kid]

    async def _refresh(self) -> _Cache:
        discovery = await self._fetch_json(self.discovery_url)
        jwks_uri = discovery.get("jwks_uri")
        if not isinstance(jwks_uri, str):
            raise JWKSFetchError("discovery document missing 'jwks_uri'")

        jwks = await self._fetch_json(jwks_uri)
        raw_keys = jwks.get("keys")
        if not isinstance(raw_keys, list):
            raise JWKSFetchError("JWKS response missing 'keys' array")

        keys: dict[str, PyJWK] = {}
        for raw_jwk in raw_keys:  # type: ignore[reportUnknownVariableType]
            if not isinstance(raw_jwk, dict):
                continue
            jwk_dict: dict[str, Any] = raw_jwk  # type: ignore[assignment]
            kid = jwk_dict.get("kid")
            if not isinstance(kid, str):
                continue
            try:
                keys[kid] = PyJWK(jwk_dict)
            except Exception as exc:
                log.warning("jwks_skip_invalid_key", kid=kid, error=repr(exc))

        cache = _Cache(keys=keys, expires_at=self._clock() + self._ttl)
        self._cache = cache
        return cache

    async def _fetch_json(self, url: str) -> dict[str, Any]:
        try:
            response = await self._http.get(url)
        except httpx.HTTPError as exc:
            raise JWKSFetchError(f"transport error fetching {url}: {exc}") from exc
        if response.status_code >= 400:
            raise JWKSFetchError(f"{url} returned HTTP {response.status_code}")
        payload: Any = response.json()
        if not isinstance(payload, dict):
            raise JWKSFetchError(f"{url} returned non-object JSON")
        return cast("dict[str, Any]", payload)


def build_jwks_client(oidc: OIDCSettings, http_client: httpx.AsyncClient) -> JWKSClient:
    """Construct a :class:`JWKSClient` from :class:`OIDCSettings`."""
    return JWKSClient(
        issuer=str(oidc.issuer),
        http_client=http_client,
        cache_ttl=timedelta(seconds=oidc.jwks_cache_ttl_seconds),
    )


__all__ = [
    "JWKSClient",
    "JWKSError",
    "JWKSFetchError",
    "UnknownSigningKey",
    "build_jwks_client",
]
