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
from typing import Annotated, Literal

from pydantic import Field, HttpUrl, PostgresDsn, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

DownloadMode = Literal["redirect", "stream"]


class TripleStoreSettings(BaseSettings):
    """Configuration for the operator-chosen triple store backend.

    The FDP communicates with the store exclusively over SPARQL 1.1 Protocol;
    the specific backend (GraphDB, Fuseki, Oxigraph, ...) is opaque beyond the
    capability flags below.
    """

    model_config = SettingsConfigDict(
        env_prefix="FDP_TRIPLESTORE_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

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

    model_config = SettingsConfigDict(
        env_prefix="FDP_OIDC_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    issuer: HttpUrl
    audience: str
    jwks_cache_ttl_seconds: int = 300
    roles_claim: str = "realm_access.roles"  # Keycloak default
    groups_claim: str = "groups"


class CORSSettings(BaseSettings):
    """Configuration for browser cross-origin access (CORS).

    The reference web client (`fdp-client`) is a single-page app served from a
    different origin than the API in every realistic deployment (separate dev
    ports in development; separate hosts/subdomains in production). Browsers
    therefore send a CORS preflight before any write (`PUT`/`PATCH`/`DELETE`)
    and before reads that carry the bearer token. Without these headers the
    browser blocks the request and the SPA reports the server as unreachable.

    Defaults cover the local dev stack only (Vite on :5173). Production
    deployments MUST set ``FDP_CORS_ALLOW_ORIGINS`` to the client's real
    origin(s) — never widen this to ``*`` while credentials are allowed.
    """

    model_config = SettingsConfigDict(
        env_prefix="FDP_CORS_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ``NoDecode`` disables pydantic-settings' automatic JSON decoding of the
    # env value, which otherwise runs *before* our validator and would reject a
    # plain comma-separated string (the friendlier form we document). We parse
    # both shapes ourselves in ``_split_origins``.
    allow_origins: Annotated[list[str], NoDecode] = Field(
        # Both loopback spellings of the Vite dev server: browsers treat
        # ``localhost`` and ``127.0.0.1`` as distinct origins, and depending on
        # how the SPA (or an OIDC redirect) is opened either may be the one
        # making requests. Allowing both avoids a "server unreachable" failure
        # that is really a CORS rejection.
        default_factory=lambda: [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ],
        description=(
            "Origins permitted to make browser requests. Accepts a JSON array or "
            "a comma-separated string via FDP_CORS_ALLOW_ORIGINS."
        ),
    )
    allow_credentials: bool = True
    """Allow the browser to send credentials (the Authorization bearer). When
    True the allowed origins must be explicit — the CORS spec forbids pairing
    credentials with the ``*`` wildcard."""

    @field_validator("allow_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        """Parse the env value into a list of origins.

        Accepts both a JSON array (``["a","b"]``) and the friendlier
        comma-separated string (``a,b``). A value that is already a list (the
        default, or programmatic construction) passes through untouched.
        """
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("["):
                import json

                return json.loads(text)
            return [origin.strip() for origin in text.split(",") if origin.strip()]
        return value


class MetricsSettings(BaseSettings):
    """Configuration for the anonymous metrics pipeline.

    See ADR-0002 for the privacy design. These settings tune the boundaries but
    cannot widen them: identifying data is dropped at ingress regardless of
    configuration.
    """

    model_config = SettingsConfigDict(
        env_prefix="FDP_METRICS_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    enabled: bool = True
    unique_visitor_counting: bool = True  # disable in jurisdictions that require it
    geoip_database_path: str = "/var/lib/fdp/GeoLite2-City.mmdb"
    aggregate_to_hourly_after_seconds: int = 300
    discard_hourly_after_days: int = 2


class ProfileSettings(BaseSettings):
    """Configuration for the deployment-profile bootstrap (architecture §12)."""

    model_config = SettingsConfigDict(
        env_prefix="FDP_PROFILE_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    auto_apply: bool = False
    """If True and ``path`` is set, the lifespan handler applies the profile
    when ``profile_applied`` is empty. Off by default so dev startup never
    surprises an operator with a destructive operation."""

    path: Path | None = None
    """Filesystem location of the profile bundle (the directory containing
    ``profile.yaml``). Resolved before the auto-bootstrap runs."""


class SearchSettings(BaseSettings):
    """Configuration for the metadata search index + query API (Phase 7)."""

    model_config = SettingsConfigDict(
        env_prefix="FDP_SEARCH_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    enabled: bool = True
    """When False the indexer subscriber does not start and ``/search`` 404s."""

    default_language: str = "english"
    """Postgres text-search configuration used to build ``tsvector`` and parse
    queries. A valid ``regconfig`` name (``english``, ``simple``, …)."""

    max_limit: int = 100
    """Hard cap on the page size a single ``/search`` request may ask for."""

    default_limit: int = 20
    """Page size when the request omits ``limit``."""


class ApiKeySettings(BaseSettings):
    """Configuration for machine-to-machine API keys (Phase 11.1, ADR-0011).

    Keys are an alternate credential for an existing OIDC subject — they mint
    no new identity. Freshness of a long-lived key's authorization is handled
    by the ``subject_principal`` record (refreshed on interactive login), not
    by forcing expiry; these settings bound issuance.
    """

    model_config = SettingsConfigDict(
        env_prefix="FDP_API_KEYS_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    enabled: bool = True
    """When False, the ``/me/api-keys`` surface 404s and ``fdpk_`` bearer
    tokens are rejected — deployments that prefer per-client ``client_credentials``
    can turn the feature off entirely."""

    max_per_user: int = 20
    """Cap on the number of *active* (non-revoked) keys one subject may hold."""

    max_ttl_days: int | None = None
    """Optional ceiling on a key's lifetime. ``None`` allows non-expiring keys;
    set it to force every key to carry an ``expires_at`` no further out than
    this many days."""


class SchemaSyncSettings(BaseSettings):
    """Configuration for remote SHACL-schema synchronization (Phase 10.2).

    A published schema may declare ``dct:source <remote-url>``; when sync is
    enabled the server periodically refetches that URL and republishes the
    shape (bumping its ``owl:versionInfo``) if the remote content changed.

    Two independent gates, both off by default:

    * ``enabled`` — the scheduled background sync. Off so a deployment never
      makes outbound fetches it wasn't configured for. A manual
      ``fdp schema sync`` run is an explicit operator action and ignores this.
    * ``allowed_hosts`` — the allow-list of hostnames the syncer may fetch
      from. **Empty means no host is allowed**, so even an explicit run is a
      no-op until an operator lists the hosts. This is the structural "no
      unauthenticated outbound fetches to arbitrary IRIs" boundary; it is
      enforced on every fetch regardless of ``enabled``.
    """

    model_config = SettingsConfigDict(
        env_prefix="FDP_SCHEMA_SYNC_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    enabled: bool = False
    """If True, a scheduled run (arq cron / external scheduler calling
    ``fdp schema sync``) refetches remote-sourced schemas. Off by default."""

    interval_seconds: int = 86400
    """How often an external scheduler should invoke the sync. Daily by
    default; the value is advisory (the CLI runs one pass per invocation)."""

    # ``NoDecode`` for the same reason as ``CORSSettings.allow_origins``: accept
    # a friendly comma-separated string in addition to a JSON array.
    allowed_hosts: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        description=(
            "Hostnames the schema syncer may fetch from. Empty denies all. "
            "Accepts a JSON array or a comma-separated string via "
            "FDP_SCHEMA_SYNC_ALLOWED_HOSTS."
        ),
    )

    timeout_seconds: float = 30.0
    """Per-fetch timeout when retrieving a remote shape."""

    max_bytes: int = 5 * 1024 * 1024  # 5 MiB — shapes are small
    """Hard cap on a fetched shape's size; oversize responses are rejected."""

    @field_validator("allowed_hosts", mode="before")
    @classmethod
    def _split_hosts(cls, value: object) -> object:
        """Parse the env value into a list of hostnames (JSON array or CSV)."""
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("["):
                import json

                return json.loads(text)
            return [host.strip() for host in text.split(",") if host.strip()]
        return value


class DataSettings(BaseSettings):
    """Configuration for the simple data provider (architecture §5.6).

    The provider serves only distributions whose Offer permits anonymous
    read; that invariant is enforced in code regardless of these
    settings.
    """

    model_config = SettingsConfigDict(
        env_prefix="FDP_DATA_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

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
    cors: CORSSettings = Field(default_factory=lambda: CORSSettings())
    metrics: MetricsSettings = Field(default_factory=lambda: MetricsSettings())
    data: DataSettings = Field(default_factory=lambda: DataSettings())
    profile: ProfileSettings = Field(default_factory=lambda: ProfileSettings())
    schema_sync: SchemaSyncSettings = Field(default_factory=lambda: SchemaSyncSettings())
    api_keys: ApiKeySettings = Field(default_factory=lambda: ApiKeySettings())
    search: SearchSettings = Field(default_factory=lambda: SearchSettings())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the singleton ``Settings`` instance.

    Cached for the lifetime of the process. Tests should clear the cache
    (``get_settings.cache_clear()``) or build their own ``Settings`` instance
    and pass it explicitly.
    """
    return Settings()  # type: ignore[call-arg]  # pydantic-settings fills from env
