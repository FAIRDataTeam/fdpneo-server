"""Bootstrap config endpoint (task 6.4).

Returns the FDP's self-description that the client reads at startup:
which IdP to point users at, which features are enabled, and what
deployment profile is active. Unauthenticated by design — the client
needs this before it can ask the user to log in.

No secrets in the payload. Anything sensitive (signing keys, audit
content, raw policy text) must not be added to :class:`BootstrapConfig`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from fastapi import APIRouter
from pydantic import BaseModel

from fdpneo_server import __version__

if TYPE_CHECKING:
    from fdpneo_server.config import Settings


class OIDCBootstrap(BaseModel):
    """OIDC endpoint hints for the client.

    The issuer's ``.well-known/openid-configuration`` is the authoritative
    source for endpoint URLs; the client uses it to discover the
    authorization, token, and JWKS endpoints. We expose only the issuer
    and audience here, plus a non-binding ``client_id_hint`` so a stock
    client can pre-fill its OIDC registration if the operator hasn't
    customised it.
    """

    issuer: str
    audience: str
    client_id_hint: str | None = None


class ProfileBootstrap(BaseModel):
    """Currently-applied deployment profile."""

    name: str
    version: str


ProfileReader = Callable[[], Awaitable[ProfileBootstrap | None]]
"""Async callable returning the currently-applied profile, or ``None``.

Injected by the composition root (``main.py``) so this endpoint stays free of
any ``metadata``/``storage`` dependency — the ``profile_applied`` state table is
owned by the ``metadata`` context."""


class FeatureFlags(BaseModel):
    """Coarse feature flags surfaced to the client.

    The client hides any UI that depends on a feature whose flag is
    ``False``. Flags reflect what the server actually exposes — not what
    the deployment *could* be configured to enable.
    """

    metrics: bool
    sparql: bool
    data_provider: bool
    search: bool = False
    index: bool = False
    user_management: bool = False


class BootstrapConfig(BaseModel):
    """Self-description payload returned from ``GET /config``."""

    fdp_url: str
    """The FDP's canonical, persistent base IRI — the identifier namespace
    records are minted under (ADR-0014). Equals ``serving_url`` when no PID
    namespace is configured (local/dev). The client renders record PIDs against
    this, but issues API calls against ``serving_url``."""

    serving_url: str
    """The serving origin where this FDP's API is reachable (``base_url``). A PID
    redirector (W3ID/PURL) ultimately points here. Equals ``fdp_url`` in dev."""

    fdp_namespace: str
    fdp_version: str
    oidc: OIDCBootstrap
    profile: ProfileBootstrap | None
    features: FeatureFlags


def build_bootstrap_router(
    *,
    settings: Settings,
    profile_reader: ProfileReader,
    client_id_hint: str | None = "fdp-client",
) -> APIRouter:
    """Construct a router that serves ``GET /config``.

    ``profile_reader`` is awaited on every request so the response reflects the
    current profile state (e.g. when an admin re-applies a profile while the
    server is running).
    """
    router = APIRouter(tags=["config"])

    @router.get("/config", response_model=BootstrapConfig, name="bootstrap_config")
    async def get_bootstrap_config() -> BootstrapConfig:  # pyright: ignore[reportUnusedFunction]
        """Self-describing config payload. Unauthenticated. No secrets."""
        profile = await profile_reader()

        return BootstrapConfig(
            fdp_url=settings.resolved_identifier_base,
            serving_url=settings.serving_base,
            fdp_namespace=str(settings.fdp_namespace),
            fdp_version=__version__,
            oidc=OIDCBootstrap(
                issuer=str(settings.oidc.issuer).rstrip("/"),
                audience=settings.oidc.audience,
                client_id_hint=client_id_hint,
            ),
            profile=profile,
            features=FeatureFlags(
                metrics=settings.metrics.enabled,
                sparql=True,
                data_provider=True,
                search=settings.search.enabled,
                # index stays False until the FDP Index protocol (Phase 8) ships.
                user_management=settings.idp_admin.enabled,
            ),
        )

    return router


__all__ = [
    "BootstrapConfig",
    "FeatureFlags",
    "OIDCBootstrap",
    "ProfileBootstrap",
    "ProfileReader",
    "build_bootstrap_router",
]
