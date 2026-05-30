"""Bootstrap config endpoint (task 6.4).

Returns the FDP's self-description that the client reads at startup:
which IdP to point users at, which features are enabled, and what
deployment profile is active. Unauthenticated by design — the client
needs this before it can ask the user to log in.

No secrets in the payload. Anything sensitive (signing keys, audit
content, raw policy text) must not be added to :class:`BootstrapConfig`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter
from pydantic import BaseModel

from fdp import __version__
from fdp.metadata.profiles.state import ProfileStateRepository

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from fdp.config import Settings


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


class BootstrapConfig(BaseModel):
    """Self-description payload returned from ``GET /config``."""

    fdp_url: str
    fdp_namespace: str
    fdp_version: str
    oidc: OIDCBootstrap
    profile: ProfileBootstrap | None
    features: FeatureFlags


def build_bootstrap_router(
    *,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    client_id_hint: str | None = "fdp-client",
) -> APIRouter:
    """Construct a router that serves ``GET /config``.

    The session-factory dep is opened on every request so the response
    reflects the current profile state (e.g. when an admin re-applies a
    profile while the server is running).
    """
    router = APIRouter(tags=["config"])

    @router.get("/config", response_model=BootstrapConfig, name="bootstrap_config")
    async def get_bootstrap_config() -> BootstrapConfig:  # pyright: ignore[reportUnusedFunction]
        """Self-describing config payload. Unauthenticated. No secrets."""
        profile: ProfileBootstrap | None = None
        async with session_factory() as session:
            row = await ProfileStateRepository(session).current()
            if row is not None:
                profile = ProfileBootstrap(name=row.name, version=row.version)

        return BootstrapConfig(
            fdp_url=str(settings.base_url).rstrip("/"),
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
            ),
        )

    return router


__all__ = [
    "BootstrapConfig",
    "FeatureFlags",
    "OIDCBootstrap",
    "ProfileBootstrap",
    "build_bootstrap_router",
]
