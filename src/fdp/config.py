"""Application configuration.

Settings are layered: built-in defaults, then ``.env`` file (if present), then
environment variables (which override the file). Secrets are referenced by name
and resolved from the environment at startup; no secret values are read from
disk except through standard secret-mount paths.

Settings are immutable once loaded. Tests build their own ``Settings`` instances
rather than mutating the global one.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, HttpUrl, PostgresDsn, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

DownloadMode = Literal["redirect", "stream"]


class TripleStoreSettings(BaseSettings):
    """Configuration for the operator-chosen triple store backend.

    The FDP communicates with the store exclusively over SPARQL 1.1 Protocol;
    the specific backend (GraphDB, Fuseki, Oxigraph, ...) is opaque beyond the
    capability flags below.
    """

    model_config = SettingsConfigDict(env_prefix="FDP_TRIPLESTORE_", extra="ignore")

    query_endpoint: HttpUrl
    update_endpoint: HttpUrl
    graph_store_endpoint: HttpUrl | None = None  # optional, for graph-store protocol
    username: str | None = None
    password: SecretStr | None = None

    # Capability flags - opted into per backend
    supports_repository_management: bool = False  # e.g., GraphDB
    supports_named_graph_sync: bool = False


class OIDCSettings(BaseSettings):
    """Configuration for the external OIDC identity provider.

    The FDP delegates all authentication to the configured provider. Claim
    names are configurable to accommodate different IdP conventions (Keycloak,
    Auth0, institutional IdPs).
    """

    model_config = SettingsConfigDict(env_prefix="FDP_OIDC_", extra="ignore")

    issuer: HttpUrl
    audience: str
    jwks_cache_ttl_seconds: int = 300
    roles_claim: str = "realm_access.roles"  # Keycloak default
    groups_claim: str = "groups"


class MetricsSettings(BaseSettings):
    """Configuration for the anonymous metrics pipeline.

    See ADR-0002 for the privacy design. These settings tune the boundaries but
    cannot widen them: identifying data is dropped at ingress regardless of
    configuration.
    """

    model_config = SettingsConfigDict(env_prefix="FDP_METRICS_", extra="ignore")

    enabled: bool = True
    unique_visitor_counting: bool = True  # disable in jurisdictions that require it
    geoip_database_path: str = "/var/lib/fdp/GeoLite2-City.mmdb"
    aggregate_to_hourly_after_seconds: int = 300
    discard_hourly_after_days: int = 2


class ProfileSettings(BaseSettings):
    """Configuration for the deployment-profile bootstrap (architecture §12)."""

    model_config = SettingsConfigDict(env_prefix="FDP_PROFILE_", extra="ignore")

    auto_apply: bool = False
    """If True and ``path`` is set, the lifespan handler applies the profile
    when ``profile_applied`` is empty. Off by default so dev startup never
    surprises an operator with a destructive operation."""

    path: Path | None = None
    """Filesystem location of the profile bundle (the directory containing
    ``profile.yaml``). Resolved before the auto-bootstrap runs."""


class DataSettings(BaseSettings):
    """Configuration for the simple data provider (architecture §5.6).

    The provider serves only distributions whose Offer permits anonymous
    read; that invariant is enforced in code regardless of these
    settings.
    """

    model_config = SettingsConfigDict(env_prefix="FDP_DATA_", extra="ignore")

    download_mode: DownloadMode = "redirect"
    """``redirect`` issues a 302 to the upstream ``dcat:downloadURL``;
    ``stream`` proxies bytes through the FDP (range-aware). Redirect is
    the default because it is cheaper and avoids the FDP becoming a
    bandwidth bottleneck."""

    proxy_timeout_seconds: float = 30.0
    """Per-request timeout when streaming an upstream download. Applies
    only in ``stream`` mode."""

    proxy_max_bytes: int = 1024 * 1024 * 1024  # 1 GiB
    """Hard cap on a streamed download's size. Applies only in ``stream``
    mode; oversize responses are aborted mid-stream."""


class Settings(BaseSettings):
    """Top-level application settings.

    Subgroups (triple store, OIDC, metrics) are loaded from their own prefixed
    environment variables and accessed via the corresponding attributes.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )

    environment: Literal["development", "staging", "production"] = "development"
    base_url: HttpUrl = Field(
        default=HttpUrl("http://localhost:8000"),
        description="The public URL at which this FDP is reachable. Used to mint resource URIs.",
    )
    fdp_namespace: HttpUrl = Field(
        default=HttpUrl("https://w3id.org/fdp/o#"),
        description=(
            "The IRI for the FDP-specific RDF vocabulary used by this deployment. "
            "Each FDP can publish its own persistent namespace; defaults to the "
            "W3ID-hosted FDP ontology."
        ),
    )

    # Postgres
    postgres_dsn: PostgresDsn

    # Subgroups — required fields are filled from prefixed env vars at construction time.
    triplestore: TripleStoreSettings = Field(
        default_factory=lambda: TripleStoreSettings(),  # type: ignore[call-arg]
    )
    oidc: OIDCSettings = Field(default_factory=lambda: OIDCSettings())  # type: ignore[call-arg]
    metrics: MetricsSettings = Field(default_factory=lambda: MetricsSettings())
    data: DataSettings = Field(default_factory=lambda: DataSettings())
    profile: ProfileSettings = Field(default_factory=lambda: ProfileSettings())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the singleton ``Settings`` instance.

    Cached for the lifetime of the process. Tests should clear the cache
    (``get_settings.cache_clear()``) or build their own ``Settings`` instance
    and pass it explicitly.
    """
    return Settings()  # type: ignore[call-arg]  # pydantic-settings fills from env
