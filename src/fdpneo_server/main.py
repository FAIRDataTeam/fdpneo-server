"""FastAPI application entrypoint.

Composes the four bounded contexts behind two HTTP surfaces (LDP REST API and
SPARQL endpoint) and registers cross-cutting middleware (auth, request context,
structured logging, OpenTelemetry).

The HTTP routers are owned by the modules they belong to; this file only
imports and mounts them, plus the lifespan handler that sets up shared
dependencies (storage adapters, OIDC JWKS cache, event-bus subscribers).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator, Callable, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog
from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from rdflib import URIRef

from fdpneo_server import __version__
from fdpneo_server.access.router import build_sparql_router
from fdpneo_server.config import TripleStoreSettings, get_settings
from fdpneo_server.data.router import build_data_router
from fdpneo_server.identity import AuthenticationMiddleware, build_jwks_client
from fdpneo_server.identity.api_keys import ApiKeyRepository, ApiKeyService, build_api_keys_router
from fdpneo_server.identity.bootstrap import ProfileBootstrap, build_bootstrap_router
from fdpneo_server.identity.keycloak_admin import KeycloakUserDirectory
from fdpneo_server.identity.principal import SubjectPrincipalRepository
from fdpneo_server.identity.users import build_users_router
from fdpneo_server.metadata.admin import ResetService, build_admin_router
from fdpneo_server.metadata.audit import AuditLog
from fdpneo_server.metadata.autocomplete import AutocompleteService, build_autocomplete_router
from fdpneo_server.metadata.backup import BackupJobRegistry, build_backup_admin_router
from fdpneo_server.metadata.containment import ContainmentManager
from fdpneo_server.metadata.dashboard import DashboardService, build_dashboard_router
from fdpneo_server.metadata.extensions import build_extensions_router
from fdpneo_server.metadata.external_labels import ExternalLabelCache, ExternalLabelFetcher
from fdpneo_server.metadata.graphs import record_graph_uri
from fdpneo_server.metadata.index_ping import IndexPinger
from fdpneo_server.metadata.index_targets import (
    IndexTargetRepository,
    IndexTargetService,
    build_index_targets_router,
)
from fdpneo_server.metadata.instances import InstanceLookupService, build_instances_router
from fdpneo_server.metadata.labels import LabelResolver, build_labels_router
from fdpneo_server.metadata.ldp.router import build_ldp_router
from fdpneo_server.metadata.licenses import (
    LICENSE_SHAPE_IRI,
    LicenseService,
    build_license_router,
    predefined_license_shape_graph,
)
from fdpneo_server.metadata.lifecycle import (
    StateGate,
    StateReader,
    StateService,
    build_state_router,
)
from fdpneo_server.metadata.meta import META_SHAPE_IRI
from fdpneo_server.metadata.openapi import inject_resource_definition_paths
from fdpneo_server.metadata.policies import PolicyService, build_policy_router
from fdpneo_server.metadata.prof import ProfileService, build_profile_router
from fdpneo_server.metadata.prof_backfill import backfill_conformance
from fdpneo_server.metadata.profiles import (
    RD_SHAPE_IRI,
    ProfileStateRepository,
    ResourceDefinitionCache,
    ResourceDefinitionService,
    apply_profile,
    build_cache_from_repository,
    bundled_default_profile,
    load_profile,
    resolve_runtime_state,
)
from fdpneo_server.metadata.profiles.applier import ensure_root_service_advertisement
from fdpneo_server.metadata.rd_api import build_resource_definition_router
from fdpneo_server.metadata.repository import MetadataRepository
from fdpneo_server.metadata.schemas import SchemaService, build_schema_router
from fdpneo_server.metadata.search.indexer import SearchIndexer
from fdpneo_server.metadata.search.repository import SearchIndexRepository
from fdpneo_server.metadata.search.router import build_search_router
from fdpneo_server.metadata.search.saved import (
    SavedQueryRepository,
    SavedQueryService,
    build_saved_queries_router,
)
from fdpneo_server.metadata.search.service import SearchService
from fdpneo_server.metadata.settings import SettingsRepository, build_settings_router
from fdpneo_server.metadata.shacl import ShaclValidator
from fdpneo_server.metadata.shape_provider import MetadataShapeProvider, PredefinedShapeProvider
from fdpneo_server.metadata.signposting import REL_HAS_RESOURCE_DEFINITIONS, Link
from fdpneo_server.metrics.api import build_metrics_router
from fdpneo_server.metrics.geo import open_geo_lookup
from fdpneo_server.metrics.middleware import RequestObservationMiddleware
from fdpneo_server.metrics.pipeline import MetricsPipeline
from fdpneo_server.metrics.salt import SaltRotator
from fdpneo_server.metrics.scheduler import MetricsRollupScheduler
from fdpneo_server.operational import build_info_router, build_readiness_router
from fdpneo_server.policy.model import Action
from fdpneo_server.policy.parser import parse_offer
from fdpneo_server.policy.resolver import GraphBackedOfferResolver
from fdpneo_server.policy.runtime import RequestScopedPDP
from fdpneo_server.shared.context import RequestContext
from fdpneo_server.shared.errors import (
    CatchAllExceptionMiddleware,
    SchemaViolation,
    register_exception_handlers,
)
from fdpneo_server.shared.events import EventBus
from fdpneo_server.shared.limits import BodySizeLimitMiddleware, RateLimitMiddleware
from fdpneo_server.shared.logging import configure_logging
from fdpneo_server.shared.reserved import RESERVED_API_PATH
from fdpneo_server.shared.security_headers import SecurityHeadersMiddleware
from fdpneo_server.storage.postgres.engine import build_engine, build_session_factory
from fdpneo_server.storage.triplestore.adapter import TripleStoreAdapter
from fdpneo_server.storage.triplestore.conformance import verify_named_graph_isolation

log = structlog.get_logger(__name__)

TripleStoreFactory = Callable[[TripleStoreSettings | None], TripleStoreAdapter]
"""Downstream seam (ADR-0023): builds the adapter every service will use.

The default is :meth:`TripleStoreAdapter.from_settings`; a downstream that
needs to mediate RDF I/O (driver quirks, telemetry, query budgets) passes
its own callable to :func:`create_app` and returns a subclassed or wrapped
adapter.

The factory receives ``settings.triplestore``, which is ``None`` when the
operator configured no ``FDP_TRIPLESTORE_*`` environment — the normal state
for an embedder whose factory owns storage configuration and ignores the
argument. Only the no-factory default path *requires* the settings.
"""


def _docs_enabled(environment: str, expose_api_docs: bool) -> bool:
    """Whether to serve the interactive docs UIs (audit R-04).

    On in development, or anywhere ``expose_api_docs`` is explicitly set — off by
    default in staging/production to remove the Swagger "try it out" surface.
    """
    return expose_api_docs or environment == "development"


def _build_shared_state(
    app: FastAPI,
    *,
    triple_store_factory: TripleStoreFactory | None = None,
) -> None:
    """Construct singletons and attach them to ``app.state``.

    Done in ``create_app`` rather than ``lifespan`` so routers that
    depend on ``session_factory`` / ``event_bus`` can be mounted before
    the first request. Engines and HTTP clients here are lazy — they
    don't open sockets until used — so this is safe at import time.
    """
    settings = get_settings()

    http_client = httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=2.0))
    app.state.http_client = http_client
    app.state.jwks_client = build_jwks_client(settings.oidc, http_client)

    engine = build_engine(settings)
    app.state.engine = engine
    app.state.session_factory = build_session_factory(engine)

    # API keys (Phase 11.1 / ADR-0011). The principal repository records each
    # subject's freshest IdP roles on JWT login (via the middleware) so a
    # long-lived key resolves current roles, not a frozen snapshot.
    app.state.subject_principal_repo = SubjectPrincipalRepository(
        session_factory=app.state.session_factory
    )
    app.state.api_key_service = ApiKeyService(
        repository=ApiKeyRepository(session_factory=app.state.session_factory),
        principals=app.state.subject_principal_repo,
        settings=settings.api_keys,
    )

    # User-management facade (ADR-0013). None — and the /users surface returns
    # 503 — unless an IdP-admin service account is configured. Reuses the shared
    # httpx client for outbound Admin REST calls.
    app.state.user_directory = KeycloakUserDirectory.from_settings(
        idp_admin=settings.idp_admin,
        oidc=settings.oidc,
        http_client=http_client,
    )

    app.state.event_bus = EventBus()

    # Triple store adapter + metadata repository, shared across requests.
    # Each request that needs the adapter borrows the singleton; the
    # underlying httpx client pools connections. A downstream may supply
    # its own factory (ADR-0023) to mediate the adapter — every RDF read
    # and write in the app goes through the instance built here. Only the
    # default path demands FDP_TRIPLESTORE_* configuration; a factory
    # receives whatever is configured (usually None) and may ignore it.
    if triple_store_factory is None:
        app.state.triplestore = TripleStoreAdapter.from_settings(settings.require_triplestore())
    else:
        app.state.triplestore = triple_store_factory(settings.triplestore)
    app.state.metadata_repository = MetadataRepository(app.state.triplestore)
    # Set True until the startup named-graph isolation self-test runs (audit R-03);
    # the lifespan flips it to the probe result. Until then reads behave normally.
    app.state.sparql_multigraph_safe = True

    # PDP wrapper that opens a fresh Postgres session per call — required
    # because CacheRepository binds to one session, and the data/SPARQL
    # routers are concurrent. See fdpneo_server.policy.runtime.
    #
    # The offer resolver walks dct:isPartOf for inheritance; the
    # system-default Offer IRI is published on app.state by the
    # auto-bootstrap hook and read lazily on every call so the resolver
    # picks it up without rebuild.
    app.state.system_default_offer_iri = None
    app.state.offer_resolver = GraphBackedOfferResolver(
        app.state.metadata_repository,
        system_default_provider=lambda: app.state.system_default_offer_iri,
    )
    app.state.pdp = RequestScopedPDP(
        session_factory=app.state.session_factory,
        offer_resolver=app.state.offer_resolver,
    )

    # Publication-state lifecycle (Phase 12 / ADR-0010). The reader fronts the
    # meta-graph state triples; the gate layers state visibility over the ODRL
    # read decision at every read PEP; the service drives the transition API.
    app.state.state_reader = StateReader(app.state.triplestore)
    app.state.state_gate = StateGate(reader=app.state.state_reader, pdp=app.state.pdp)
    app.state.state_service = StateService(
        adapter=app.state.triplestore,
        reader=app.state.state_reader,
        pdp=app.state.pdp,
        event_bus=app.state.event_bus,
        clock=lambda: datetime.now(UTC),
    )

    # Resource-definition cache — populated by the profile applier (CLI
    # apply or lifespan auto-bootstrap). Initialised None so attribute
    # access always succeeds; the OpenAPI generator and the LDP
    # container registry both no-op when it's not yet set.
    app.state.resource_definitions = None

    # SHACL validator backed by shapes stored in the metadata triple
    # store (architecture §5.3). Bootstrap-warming happens after the
    # profile is applied so the validator's parsed-shape cache is
    # populated against the IRIs the profile declares.
    # Server-owned shapes (the managed-license shape, ADR-0012) are constants and
    # resolve from code, so license validation works even on a deployment whose
    # profile was applied before the shape existed; profile-declared schema
    # shapes still come from the triple store via the delegate.
    app.state.shacl_validator = ShaclValidator(
        PredefinedShapeProvider(
            predefined={
                LICENSE_SHAPE_IRI: predefined_license_shape_graph().serialize(format="turtle")
            },
            delegate=MetadataShapeProvider(app.state.metadata_repository),
        )
    )

    # Now that the validator exists, make the repository validate the
    # meta-metadata graph against META_SHAPE_IRI on every write (architecture
    # §6.2 / task 2.5). Installed post-construction because the validator's
    # shape provider reads through this same repository — a missing meta shape
    # degrades safely (see MetaWriter), and the applier stores the shape at
    # bootstrap so runtime writes have it.
    app.state.metadata_repository.enable_meta_validation(
        validator=app.state.shacl_validator, shape_iri=META_SHAPE_IRI
    )

    # Resource-definition mutation coordinator (ADR-0009). Each runtime
    # create/replace/delete writes through the repository, rebuilds the cache
    # from the store, and publishes it via the on_rebuilt callback below —
    # which swaps app.state.resource_definitions, drops the cached OpenAPI,
    # and warms the validator + authz caches so the new type's endpoints and
    # docs light up immediately.
    app.state.resource_definition_service = ResourceDefinitionService(
        repository=app.state.metadata_repository,
        adapter=app.state.triplestore,
        base_url=settings.resolved_identifier_base,
        validator=app.state.shacl_validator,
        on_rebuilt=lambda cache: _publish_resource_definitions(app, cache),
    )

    # Runtime SHACL-shape admin (Phase 10.1). Stores shapes as records and
    # keeps the validator cache coherent; the resource-definition admin API
    # requires the shapes it publishes.
    app.state.schema_service = SchemaService(
        repository=app.state.metadata_repository,
        adapter=app.state.triplestore,
        validator=app.state.shacl_validator,
        base_url=settings.resolved_identifier_base,
        # The FDP root schema (the root RD's schema) is editable but not
        # deletable (task 10.5). Resolved per-call from the live RD cache, which
        # is published after this point and swapped on every profile re-apply /
        # RD mutation.
        root_schema_iri_provider=lambda: _root_schema_iri(app),
    )
    # Read-only access to PROF profiles (ADR-0019); the schema service is the
    # writer (auto-provisions a 1:1 profile on every schema publish).
    app.state.profile_service = ProfileService(
        repository=app.state.metadata_repository,
        adapter=app.state.triplestore,
        base_url=settings.resolved_identifier_base,
    )

    # First-class ODRL policy and license documents (Phase 14 / ADR-0012). Two
    # separate subsystems: /policies stores PDP-enforced Offers (a write clears
    # the authz cache so the change takes effect); /licenses stores descriptive
    # license documents referenced via dct:license (no PDP coupling).
    app.state.policy_service = PolicyService(
        repository=app.state.metadata_repository,
        adapter=app.state.triplestore,
        pdp=app.state.pdp,
        base_url=settings.resolved_identifier_base,
        event_bus=app.state.event_bus,
    )
    app.state.license_service = LicenseService(
        repository=app.state.metadata_repository,
        adapter=app.state.triplestore,
        validator=app.state.shacl_validator,
        base_url=settings.resolved_identifier_base,
        event_bus=app.state.event_bus,
    )

    # GeoLite2 lookup degrades to no-op if the DB is missing — safe for dev.
    app.state.geo = open_geo_lookup(settings.metrics.geoip_database_path)
    app.state.salt_rotator = SaltRotator()
    app.state.metrics_pipeline = MetricsPipeline(
        session_factory=app.state.session_factory,
        geo=app.state.geo,
        salt_rotator=app.state.salt_rotator,
        enabled=settings.metrics.enabled,
        counting_enabled=settings.metrics.unique_visitor_counting,
    )
    # Optional in-process rollup driver (off by default; prod uses an external
    # `fdp metrics rollup` cron). Without a scheduler raw never aggregates and
    # the dashboard stays empty — see MetricsSettings.rollup_in_process.
    app.state.metrics_rollup_scheduler = MetricsRollupScheduler(
        session_factory=app.state.session_factory,
        settings=settings.metrics,
    )
    app.state.audit_log = AuditLog(session_factory=app.state.session_factory)

    # Runtime index targets (ADR-0025): admin-managed rows unioned with the env
    # set. Built before the pinger, which reads them through the provider on
    # EVERY ping — so a deployment that boots with zero targets starts
    # announcing the moment the first target is added, with no restart.
    app.state.index_target_service = IndexTargetService(
        repository=IndexTargetRepository(session_factory=app.state.session_factory),
        settings=settings.index,
    )
    # Outbound Index ping (Phase 8.1): announce this FDP to the effective
    # targets so indexes can harvest it (reference wire protocol).
    app.state.index_pinger = IndexPinger(
        settings=settings.index,
        client_url=settings.resolved_identifier_base,
        http_client=http_client,
        targets_provider=app.state.index_target_service.effective_urls,
        on_results=app.state.index_target_service.record_results,
    )

    # Runtime settings repository — Postgres-backed; read on demand so admin
    # updates are visible without restart. Built before the label resolver and
    # autocomplete service, which both read from it.
    app.state.settings_repository = SettingsRepository(session_factory=app.state.session_factory)

    # Label resolver: knowledge-graph labels (per-(iri, lang) TTL cache), a
    # settings-backed inline source (6.1a) for vocabulary labels the graph
    # doesn't describe, and a third external source (Phase 21) that dereferences
    # allow-listed external IRIs (DOI/ORCID/SKOS …) over RDF and caches them in
    # Postgres. The external source is inert unless FDP_REMOTE_LABELS_* enable it.
    app.state.label_resolver = LabelResolver(
        adapter=app.state.triplestore,
        settings_repository=app.state.settings_repository,
        external_cache=ExternalLabelCache(session_factory=app.state.session_factory),
        external_fetcher=ExternalLabelFetcher(
            http_client=http_client, settings=settings.remote_labels
        ),
        remote_settings=settings.remote_labels,
    )

    # Dashboard service: SPARQL + audit-log + PDP composition. No state.
    app.state.dashboard_service = DashboardService(
        adapter=app.state.triplestore,
        session_factory=app.state.session_factory,
        pdp=app.state.pdp,
    )

    # Autocomplete service reads the same settings sources on every call.
    app.state.autocomplete_service = AutocompleteService(
        settings_repository=app.state.settings_repository,
        adapter=app.state.triplestore,
    )

    # Factory-reset coordinator (Phase 10.4). Truncates runtime settings and
    # force re-applies the bundled profile, then republishes the profile-derived
    # runtime state via the same hook auto-bootstrap uses, so the reset takes
    # effect without a restart.
    app.state.reset_service = ResetService(
        settings=settings,
        settings_repository=app.state.settings_repository,
        repository=app.state.metadata_repository,
        on_published=lambda sdoi, rd: _publish_runtime_state(app, sdoi, rd),
    )

    # Admin backup/restore jobs (ADR-0016 §5 amendment): in-process job registry
    # backing the admin-only /admin/backup endpoints (dump/restore over HTTP).
    app.state.backup_job_registry = BackupJobRegistry()

    # Search (Phase 7). The indexer (event subscriber) keeps metadata_search
    # current; the query service applies the ODRL+state visibility gate
    # (ADR-0010) and reads facet config from the runtime settings. Saved
    # queries are a thin owner-scoped CRUD.
    app.state.search_index_repository = SearchIndexRepository(
        session_factory=app.state.session_factory
    )
    app.state.search_indexer = SearchIndexer(
        records=app.state.metadata_repository,
        search=app.state.search_index_repository,
        pdp=app.state.pdp,
        language=settings.search.default_language,
        enabled=settings.search.enabled,
    )
    app.state.search_service = SearchService(
        repository=app.state.search_index_repository,
        state_gate=app.state.state_gate,
        settings_repository=app.state.settings_repository,
        settings=settings.search,
    )
    app.state.saved_query_service = SavedQueryService(
        repository=SavedQueryRepository(session_factory=app.state.session_factory)
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Bind event-bus subscribers at startup; tear down singletons on shutdown.

    Shared singletons are constructed in :func:`_build_shared_state` so
    routers can be mounted with their collaborators at app-build time;
    here we wire the runtime-only effects (bus subscriptions) and own
    the corresponding cleanup.
    """
    log.info("fdp_starting", version=__version__)

    app.state.metrics_pipeline.start(app.state.event_bus)
    app.state.metrics_rollup_scheduler.start()
    app.state.audit_log.start(app.state.event_bus)
    app.state.search_indexer.start(app.state.event_bus)
    app.state.index_pinger.start(app.state.event_bus)
    await _maybe_auto_bootstrap(app)
    await _warm_anonymous_authz_cache(app)
    await _verify_store_conformance(app)

    try:
        yield
    finally:
        log.info("fdp_stopping")
        # Drain in-flight metrics publishes BEFORE stopping their consumers and
        # disposing the engine: a fire-and-forget publish task killed by loop
        # shutdown abandons a checked-out asyncpg connection mid-transaction —
        # dropped metrics writes on SIGTERM in production, and flaky
        # ResourceWarnings under TestClient's short-lived loop in tests.
        pending_publishes = tuple(app.state.metrics_publish_tasks)
        if pending_publishes:
            await asyncio.gather(*pending_publishes, return_exceptions=True)
        app.state.search_indexer.stop()
        await app.state.index_pinger.stop()
        await app.state.backup_job_registry.shutdown()
        await app.state.label_resolver.shutdown()
        app.state.audit_log.stop()
        await app.state.metrics_rollup_scheduler.stop()
        app.state.metrics_pipeline.stop()
        app.state.geo.close()
        await app.state.triplestore.close()
        await app.state.engine.dispose()
        await app.state.http_client.aclose()


def create_app(
    *,
    triple_store_factory: TripleStoreFactory | None = None,
    extension_routers: Sequence[APIRouter] | None = None,
) -> FastAPI:
    """Construct the FastAPI app.

    Kept as a function (rather than a module-level instance) so tests can build
    isolated apps with different settings.

    Both keyword parameters are downstream composition seams (ADR-0023) and
    are public API:

    * ``triple_store_factory`` — replaces the internal
      ``TripleStoreAdapter.from_settings`` call, so a downstream can mediate
      every RDF read/write (telemetry, driver quirks, query budgets).
    * ``extension_routers`` — mounted after every reserved ``/fdp-api``
      router and immediately before the LDP catch-all: their paths take
      precedence over LDP resource resolution, but cannot shadow the FDP's
      own fixed API. Paths they do not claim fall through to LDP unchanged.
    """
    settings = get_settings()
    configure_logging(
        settings.environment,
        subject_salt=(
            settings.log_subject_salt.get_secret_value()
            if settings.log_subject_salt is not None
            else None
        ),
    )

    docs_on = _docs_enabled(settings.environment, settings.expose_api_docs)
    app = FastAPI(
        title="FAIR Data Point",
        version=__version__,
        description=f"FAIR-aligned metadata repository — see {RESERVED_API_PATH}/docs for the API.",
        lifespan=lifespan,
        debug=settings.environment == "development",
        # All fixed FDP endpoints live under the reserved API segment so the
        # root namespace is free for user-defined resource types.
        openapi_url=f"{RESERVED_API_PATH}/openapi.json",
        docs_url=f"{RESERVED_API_PATH}/docs" if docs_on else None,
        redoc_url=f"{RESERVED_API_PATH}/redoc" if docs_on else None,
        swagger_ui_oauth2_redirect_url=f"{RESERVED_API_PATH}/docs/oauth2-redirect",
    )

    register_exception_handlers(app)
    _build_shared_state(app, triple_store_factory=triple_store_factory)

    # Middleware order: FastAPI's add_middleware registers outermost-first,
    # so the LAST add_middleware call runs INNERMOST. Putting the request
    # observer second means it executes inside AuthenticationMiddleware, so
    # the RequestContext ContextVar is bound when the observer snapshots
    # it (architecture §11.1 / ADR-0002 anonymization boundary).
    app.add_middleware(
        AuthenticationMiddleware,
        oidc=settings.oidc,
        jwks_client_provider=lambda: app.state.jwks_client,
        api_key_authenticator_provider=lambda: app.state.api_key_service,
        principal_recorder_provider=lambda: app.state.subject_principal_repo,
    )
    # The pending-task set is shared with the lifespan: the middleware
    # fire-and-forgets one publish task per request, and shutdown drains the
    # set before tearing the pipeline and engine down (see ``lifespan``).
    app.state.metrics_publish_tasks = set()
    app.add_middleware(
        RequestObservationMiddleware,
        bus_provider=lambda: app.state.event_bus,
        pending=app.state.metrics_publish_tasks,
    )
    # Request limits (audit R-02) — added here so they sit just inside CORS and
    # outside auth: floods and oversize bodies are shed before the JWKS/auth work.
    # Per-instance defense-in-depth; the reverse proxy is the authoritative limiter.
    if settings.rate_limit.enabled:
        app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.rate_limit.max_body_bytes)
        app.add_middleware(
            RateLimitMiddleware,
            requests_per_window=settings.rate_limit.requests_per_window,
            window_seconds=settings.rate_limit.window_seconds,
            trust_forwarded_for=settings.rate_limit.trust_forwarded_for,
        )
    # Catch-all error envelope (audit R-08) — just inside CORS so unexpected
    # exceptions (incl. from the outer middleware layer) return the structured
    # envelope WITH CORS headers, instead of a bare 500. FDPErrors keep their
    # status; everything else becomes a generic 500 with the stack logged only.
    app.add_middleware(CatchAllExceptionMiddleware)
    # CORS must wrap everything else (outermost) so it can answer the browser's
    # preflight OPTIONS directly — before auth — and so the Access-Control-*
    # headers are attached even to error responses from the inner middleware.
    # Starlette's add_middleware inserts at the head of the stack, so the LAST
    # add_middleware call is the OUTERMOST layer; hence CORS is added last.
    # Without this, the SPA (a different origin) is blocked on every write and
    # reports the server as unreachable. See fdpneo_server.config.CORSSettings.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors.allow_origins,
        allow_credentials=settings.cors.allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["ETag", "Location", "Link"],
    )
    # Baseline security headers (audit F-05). Added last → outermost, so the
    # headers are attached to every response, including CORS preflights and
    # errors from inner layers.
    app.add_middleware(SecurityHeadersMiddleware)

    # Route-registration order matters: the LDP router catches /{path:path}
    # under every method, so anything that should NOT resolve as an LDP
    # resource must be added first. Reserved-path prefixes the LDP router
    # cannot serve as a consequence: /healthz, /readyz, /info, /config,
    # /labels, /me, /metrics, /data, /sparql, /settings, /search (Phase 7),
    # /admin (the factory-reset surface — Phase 10.4), /forms, /state and
    # /{record}/state (publication-state transitions — Phase 12),
    # /spec, /expanded, /page, /resource-definitions (the runtime
    # resource-definition catalog + admin surface — ADR-0009), /schemas
    # (runtime SHACL-shape admin — Phase 10.1), /policies and /licenses
    # (first-class ODRL policy + license documents — Phase 14 / ADR-0012),
    # /users (IdP user-management facade — ADR-0013), and
    # the per-type /{prefix}/spec, /{prefix}/{id}/spec,
    # /{prefix}/{id}/expanded, /{prefix}/{id}/page/{childPrefix}
    # extension routes.
    @app.get(f"{RESERVED_API_PATH}/healthz", tags=["internal"])
    async def healthz() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        """Liveness probe. Does not check downstream dependencies."""
        return {"status": "ok", "version": __version__}

    # Every fixed, FDP-specific router is mounted under the reserved API segment
    # (``/fdp-api``) so the root namespace is exclusively the LDP resource tree —
    # a user-defined resource type can claim any URL prefix except ``fdp-api``.
    # The LDP catch-all (below) stays at root and serves the records themselves.
    app.include_router(build_info_router(settings=settings), prefix=RESERVED_API_PATH)
    app.include_router(
        build_readiness_router(
            session_factory=app.state.session_factory,
            adapter=app.state.triplestore,
            http_client=app.state.http_client,
            issuer=str(settings.oidc.issuer),
        ),
        prefix=RESERVED_API_PATH,
    )

    # The bootstrap endpoint reports the applied profile but must not import the
    # metadata context (boundary rules). The composition root injects the read:
    # it owns the session factory and may touch metadata's ProfileStateRepository.
    async def read_active_profile() -> ProfileBootstrap | None:
        async with app.state.session_factory() as session:
            row = await ProfileStateRepository(session).current()
            return ProfileBootstrap(name=row.name, version=row.version) if row else None

    app.include_router(
        build_bootstrap_router(settings=settings, profile_reader=read_active_profile),
        prefix=RESERVED_API_PATH,
    )
    app.include_router(
        build_labels_router(
            resolver=app.state.label_resolver,
            max_wait_ms=settings.remote_labels.max_wait_ms,
        ),
        prefix=RESERVED_API_PATH,
    )
    app.include_router(
        build_dashboard_router(service=app.state.dashboard_service), prefix=RESERVED_API_PATH
    )
    app.include_router(
        build_api_keys_router(service=app.state.api_key_service), prefix=RESERVED_API_PATH
    )
    app.include_router(
        build_search_router(service=app.state.search_service), prefix=RESERVED_API_PATH
    )
    app.include_router(
        build_saved_queries_router(service=app.state.saved_query_service),
        prefix=RESERVED_API_PATH,
    )
    app.include_router(
        build_settings_router(repository=app.state.settings_repository), prefix=RESERVED_API_PATH
    )
    app.include_router(
        build_admin_router(service=app.state.reset_service), prefix=RESERVED_API_PATH
    )
    app.include_router(
        build_backup_admin_router(
            registry=app.state.backup_job_registry,
            adapter=app.state.triplestore,
            session_factory=app.state.session_factory,
            settings=settings,
        ),
        prefix=RESERVED_API_PATH,
    )
    app.include_router(
        build_autocomplete_router(service=app.state.autocomplete_service),
        prefix=RESERVED_API_PATH,
    )
    app.include_router(
        build_metrics_router(session_factory=app.state.session_factory), prefix=RESERVED_API_PATH
    )
    app.include_router(
        build_data_router(
            repository=app.state.metadata_repository,
            pdp=app.state.pdp,
            adapter=app.state.triplestore,
            settings=settings.data,
            base_url=settings.resolved_identifier_base,
            http_client=app.state.http_client,
            state_reader=app.state.state_reader,
        ),
        prefix=RESERVED_API_PATH,
    )
    app.include_router(
        build_sparql_router(
            pdp=app.state.pdp,
            adapter=app.state.triplestore,
            state_gate=app.state.state_gate,
            multigraph_safe_provider=lambda: app.state.sparql_multigraph_safe,
        ),
        prefix=RESERVED_API_PATH,
    )
    # LDP read-extensions — both the root forms (/fdp-api/spec, /fdp-api/expanded,
    # /fdp-api/page) and the per-resource forms (/fdp-api/{prefix}/{id}/spec, …).
    # The records they describe stay at root; only the extension *views* live
    # under the reserved segment. The cache provider reads
    # app.state.resource_definitions on every call, so a profile re-apply lights
    # up new types without a server restart.
    app.include_router(
        build_extensions_router(
            repo=app.state.metadata_repository,
            pdp=app.state.pdp,
            cache_provider=lambda: app.state.resource_definitions,
            base_url=settings.resolved_identifier_base,
            state_gate=app.state.state_gate,
            # /spec returns the merged shape closure (composed types, task 15.2).
            validator=app.state.shacl_validator,
        ),
        prefix=RESERVED_API_PATH,
    )
    # Publication-state transitions (Phase 12 / ADR-0010): POST /fdp-api/state and
    # POST /fdp-api/{prefix}/{id}/state.
    app.include_router(
        build_state_router(
            service=app.state.state_service,
            base_url=settings.resolved_identifier_base,
        ),
        prefix=RESERVED_API_PATH,
    )
    # Resource-definition catalog + admin (ADR-0009) at /fdp-api/resource-definitions.
    # Reads serve the current cache; admin mutations drive the service, whose
    # on_rebuilt republishes the cache.
    app.include_router(
        build_resource_definition_router(
            service=app.state.resource_definition_service,
            cache_provider=lambda: app.state.resource_definitions,
            base_url=settings.serving_base,
        ),
        prefix=RESERVED_API_PATH,
    )
    # Schema admin (Phase 10.1) — runtime SHACL-shape management at
    # /fdp-api/schemas; the shapes themselves are stored at {base}/fdp-api/schemas/{id}.
    app.include_router(
        build_schema_router(service=app.state.schema_service), prefix=RESERVED_API_PATH
    )
    # Runtime FDP-Index targets (ADR-0025) at /fdp-api/index/* — admin-gated
    # CRUD over the ping-target set plus a synchronous "ping now" action.
    app.include_router(
        build_index_targets_router(
            service=app.state.index_target_service, pinger=app.state.index_pinger
        ),
        prefix=RESERVED_API_PATH,
    )
    # PROF conformance profiles (ADR-0019) — read-only at /fdp-api/profiles.
    # Profiles are the dct:conformsTo target for records; they are provisioned
    # from schemas (1:1 wrapper), never edited directly, so no write surface.
    app.include_router(
        build_profile_router(service=app.state.profile_service), prefix=RESERVED_API_PATH
    )
    # Dynamic class-instance / subclass lookup at /fdp-api/instances and
    # /fdp-api/subclasses — backs the client's DASH reference editors. Reads are
    # visibility-gated (ODRL read + publication state), like every other read.
    app.include_router(
        build_instances_router(
            service=InstanceLookupService(
                adapter=app.state.triplestore,
                pdp=app.state.pdp,
                base_url=settings.resolved_identifier_base,
                state_gate=app.state.state_gate,
            )
        ),
        prefix=RESERVED_API_PATH,
    )
    # First-class ODRL policy + license admin (Phase 14 / ADR-0012) at
    # /fdp-api/policies and /fdp-api/licenses; the documents themselves are stored
    # at {base}/fdp-api/policies/{id} and {base}/fdp-api/licenses/{id} as public,
    # dereferenceable reference records.
    app.include_router(
        build_policy_router(service=app.state.policy_service), prefix=RESERVED_API_PATH
    )
    app.include_router(
        build_license_router(service=app.state.license_service), prefix=RESERVED_API_PATH
    )
    # User-management admin facade (ADR-0013) at /fdp-api/users. Admin-gated; 503
    # when the IdP-admin service account isn't configured.
    app.include_router(
        build_users_router(directory=app.state.user_directory, event_bus=app.state.event_bus),
        prefix=RESERVED_API_PATH,
    )
    # Downstream extension routers (ADR-0023): after every reserved router,
    # before the LDP catch-all — so extensions win the paths they claim and
    # everything else still resolves as an LDP resource.
    for extension_router in extension_routers or ():
        app.include_router(extension_router)
    # LDP last — its /{path:path} catch-all matches every method/URL not
    # already claimed above. The container registry is a lazy adapter
    # that reads app.state.resource_definitions on every call so the
    # cache populated by auto-bootstrap (after lifespan startup) is
    # picked up without rebuilding the router. SHACL-on-write is active:
    # POST validates the new member against its container's member shape,
    # PUT/PATCH validate the resource against its own type shape, and the
    # repository validates the meta graph against META_SHAPE_IRI.
    container_registry = _DynamicContainerRegistry(app)
    # ADR-0022 §4: the root FDP record advertises the API description in-band.
    # Relative-path references resolve against the request URL, so they are
    # correct on whichever serving host answers. service-doc is emitted only when
    # the interactive docs UIs are actually served (audit R-04).
    root_service_links = [
        Link(f"{RESERVED_API_PATH}/openapi.json", "service-desc"),
        Link(f"{RESERVED_API_PATH}/resource-definitions", REL_HAS_RESOURCE_DEFINITIONS),
    ]
    if docs_on:
        root_service_links.append(Link(f"{RESERVED_API_PATH}/docs", "service-doc"))
    app.include_router(
        build_ldp_router(
            repo=app.state.metadata_repository,
            pdp=app.state.pdp,
            validator=app.state.shacl_validator,
            containers=container_registry,
            # ADR-0019: the adapter lets the write path provision/stamp the
            # record's conformance profile (dct:conformsTo + validatedAgainst).
            triplestore=app.state.triplestore,
            event_bus=app.state.event_bus,
            state_gate=app.state.state_gate,
            # Maintain the parent's forward containment links (ldp:contains +
            # the typed DCAT relation) from each child's dct:isPartOf; the same
            # RD cache that classifies containers names the predicate.
            containment=ContainmentManager(
                repo=app.state.metadata_repository, resolver=container_registry
            ),
            # Persistent-identifier canonicalization (ADR-0014): map an inbound
            # request on the serving origin to the canonical IRI under the PID
            # base. Identity when identifier_base is unset (== serving origin).
            identifier_base=settings.resolved_identifier_base,
            serving_origins=[settings.serving_base],
            root_service_links=root_service_links,
            prefix="",
        )
    )

    _install_openapi(app)
    return app


def _install_openapi(app: FastAPI) -> None:
    """Override ``app.openapi`` to inject per-resource-definition paths.

    Static routes added by ``include_router`` and decorator handlers
    (``/metrics/*``, ``/sparql``, ``/data/*``, ``/healthz``, ``/ldp/*``)
    are still discovered by FastAPI's :func:`get_openapi` — we add the
    dynamic per-type surface on top. When the cache changes (a profile
    re-apply), the auto-bootstrap hook clears
    ``app.openapi_schema`` so the next request rebuilds.
    """

    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        spec = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
        )
        cache: ResourceDefinitionCache | None = getattr(app.state, "resource_definitions", None)
        if cache is not None:
            inject_resource_definition_paths(spec, cache)
        app.openapi_schema = spec
        return spec

    app.openapi = custom_openapi  # type: ignore[method-assign]


class _DynamicContainerRegistry:
    """``ContainerRegistry`` adapter that defers to ``app.state.resource_definitions``.

    The LDP router needs a registry at build time, but the
    :class:`ResourceDefinitionCache` is only populated after lifespan's
    auto-bootstrap runs. The adapter reads ``app.state`` on every call,
    falling back to a permissive default ("nothing is a container, no
    shape resolves") when the cache is not yet set.
    """

    __slots__ = ("_app",)

    def __init__(self, app: FastAPI) -> None:
        self._app = app

    def is_container(self, resource_iri: str) -> bool:
        cache = self._cache()
        return False if cache is None else cache.is_container(resource_iri)

    def member_shape(self, container_iri: str) -> str | None:
        cache = self._cache()
        return None if cache is None else cache.member_shape(container_iri)

    def shape_for(self, resource_iri: str) -> str | None:
        cache = self._cache()
        return None if cache is None else cache.shape_for(resource_iri)

    def containment_relation(self, parent_iri: str, child_iri: str) -> str | None:
        cache = self._cache()
        return None if cache is None else cache.containment_relation(parent_iri, child_iri)

    def member_relations(self, resource_iri: str) -> list[str]:
        cache = self._cache()
        return [] if cache is None else cache.member_relations(resource_iri)

    def url_prefix_for(self, resource_iri: str) -> str | None:
        cache = self._cache()
        return None if cache is None else cache.url_prefix_for(resource_iri)

    def child_prefixes(self, resource_iri: str) -> list[str]:
        cache = self._cache()
        return [] if cache is None else cache.child_prefixes(resource_iri)

    def _cache(self) -> ResourceDefinitionCache | None:
        return getattr(self._app.state, "resource_definitions", None)


async def _verify_store_conformance(app: FastAPI) -> None:
    """Run the named-graph isolation self-test and gate multi-graph reads (R-03).

    On failure (or when the store can't be probed), multi-graph SPARQL reads are
    disabled — the SPARQL router refuses any read whose authorized projection
    spans more than one named graph, rather than risk a leak.
    """
    settings = get_settings()
    # A factory-injected adapter has no settings block (None); run the probe
    # against it anyway — verification is about the store's behavior, and the
    # safe default is to check unless the operator explicitly opted out.
    ts_settings = settings.triplestore
    if ts_settings is not None and not ts_settings.verify_named_graph_isolation:
        log.info("named_graph_isolation_check_skipped")
        return
    safe = await verify_named_graph_isolation(app.state.triplestore)
    app.state.sparql_multigraph_safe = safe
    if safe:
        log.info("named_graph_isolation_verified")
    else:
        log.error("named_graph_isolation_unsafe_multigraph_reads_disabled")


async def _maybe_auto_bootstrap(app: FastAPI) -> None:
    """Apply the configured profile if both opt-in flags are set and the FDP is uninitialized.

    Failure here is fatal — per architecture §12.2 the FDP refuses to
    start if a configured profile fails to apply. Logging is structured
    so the operator can spot the cause in their startup log.
    """
    settings = get_settings()
    if not settings.profile.auto_apply:
        return

    bundle = (
        settings.profile.path if settings.profile.path is not None else bundled_default_profile()
    )
    log.info("profile_auto_bootstrap_start", bundle=str(bundle))
    profile = load_profile(bundle)

    async with app.state.session_factory() as session:
        state = ProfileStateRepository(session)
        if await state.is_applied():
            # Profile artifacts are already in the stores, but the runtime
            # caches (offer-resolver fallback, container registry, OpenAPI,
            # SHACL warm-up) live in `app.state` and are empty after a restart.
            # Re-derive them from the profile (no writes) and publish, so the
            # FDP is fully functional — without this, creating new records is
            # default-denied because the system-default offer is unset.
            log.info(
                "profile_auto_bootstrap_skipped",
                reason="already_applied",
                profile=profile.name,
            )
            # The resource-definition cache is a projection of the triple
            # store (ADR-0009), so rebuild it from the RD records written at
            # bootstrap — this is what makes runtime-added types survive a
            # restart. Fall back to the manifest-derived cache only if the
            # store has no RD records (a deployment bootstrapped before this
            # feature shipped). The system-default offer IRI stays a manifest
            # concept.
            system_default_offer_iri, manifest_cache = resolve_runtime_state(
                profile, settings=settings
            )
            store_cache = await build_cache_from_repository(
                app.state.triplestore, base_url=settings.resolved_identifier_base
            )
            resource_definitions = store_cache if store_cache.all() else manifest_cache
            await _publish_runtime_state(app, system_default_offer_iri, resource_definitions)
            return
        report = await apply_profile(
            profile,
            repository=app.state.metadata_repository,
            state=state,
            session=session,
            settings=settings,
            force=False,
        )

    # ADR-0019: make the seeded schemas + records self-describing on a fresh
    # bootstrap — provision the 1:1 profiles and stamp conformsTo/validatedAgainst.
    if report.resource_definitions is not None:
        await backfill_conformance(
            adapter=app.state.triplestore,
            repository=app.state.metadata_repository,
            cache=report.resource_definitions,
        )

    await _publish_runtime_state(app, report.system_default_offer_iri, report.resource_definitions)


async def _publish_runtime_state(
    app: FastAPI,
    system_default_offer_iri: str | None,
    resource_definitions: ResourceDefinitionCache | None,
) -> None:
    """Install profile-derived runtime state on ``app.state``.

    Shared by the fresh-apply and already-applied startup paths. Sets the
    offer-resolver fallback (architecture §8.3) and, when a cache is present,
    publishes it via :func:`_publish_resource_definitions`.
    """
    if system_default_offer_iri is not None:
        app.state.system_default_offer_iri = system_default_offer_iri
        await _verify_system_default_offer(app, system_default_offer_iri)
    if resource_definitions is not None:
        await _publish_resource_definitions(app, resource_definitions)
    # ADR-0018 G-05: ensure the root advertises its query endpoints. A fresh
    # bootstrap seeds this; here it also self-heals a deployment bootstrapped
    # before G-05 — on restart the root gains the advertisement (idempotent, no
    # destructive re-apply).
    settings = get_settings()
    await ensure_root_service_advertisement(
        app.state.metadata_repository,
        app.state.triplestore,
        base_url=settings.resolved_identifier_base,
        search_enabled=settings.search.enabled,
    )


async def _verify_system_default_offer(app: FastAPI, iri: str) -> None:
    """Warn loudly at startup if the system-default Offer can't be resolved.

    Records without their own ``dct:rights`` fall back to this Offer (§8.3); if
    its graph is missing or unparseable, every non-anonymous write default-denies
    with "policy denies modify on …" and no other signal. The usual cause is an
    upgrade across the ADR-0012 offer→managed-policy IRI change without a profile
    re-apply, so the stored Offer sits at the old intrinsic IRI while this points
    at ``{base}/policies/{id}``. Best-effort: never fails startup.
    """
    try:
        graph = await app.state.metadata_repository.get_graph(iri)
        if len(graph) == 0:
            log.warning(
                "system_default_offer_missing",
                iri=iri,
                hint="re-apply the profile (or factory-reset) to seed the offer at this IRI",
            )
            return
        parse_offer(graph, URIRef(iri))
    except SchemaViolation as err:
        log.warning("system_default_offer_unparseable", iri=iri, error=err.message)
    except Exception as err:  # pragma: no cover - diagnostics must not break startup
        log.warning("system_default_offer_check_failed", iri=iri, error=repr(err))


async def _publish_resource_definitions(app: FastAPI, cache: ResourceDefinitionCache) -> None:
    """Swap the resource-definition cache onto ``app.state`` and refresh derived state.

    The single publish path, shared by startup and the runtime admin
    mutation flow (the :class:`ResourceDefinitionService` ``on_rebuilt``
    callback). It:

    * swaps ``app.state.resource_definitions`` (read per-call by the LDP
      container registry and the OpenAPI generator);
    * drops the cached OpenAPI so the next ``/openapi.json`` rebuilds the
      per-type paths;
    * warms the SHACL validator for every instance shape plus the predefined
      RD shape, so the first write per type — and admin RD writes — skip a
      triple-store roundtrip;
    * warms the anonymous authz cache for each typed container, so the first
      public read of a freshly added type doesn't pay a cache miss.
    """
    app.state.resource_definitions = cache
    app.openapi_schema = None  # type: ignore[assignment]
    schema_iris = sorted({rd.schema_iri for rd in cache.all()} | {RD_SHAPE_IRI, LICENSE_SHAPE_IRI})
    try:
        await app.state.shacl_validator.bootstrap(schema_iris)
    except Exception as err:
        log.warning(
            "shacl_validator_bootstrap_failed",
            error=repr(err),
            shape_iris=schema_iris,
        )
    await _warm_authz(app, _container_targets(cache))
    log.info(
        "resource_definitions_published",
        resource_definitions=len(cache.all()),
        shapes_warmed=len(schema_iris),
    )


def _root_schema_iri(app: FastAPI) -> str | None:
    """The FDP root schema IRI from the live RD cache, or ``None`` before bootstrap.

    Feeds ``SchemaService``'s delete-guard so the root resource type's schema
    (``fdp:Repository`` in the default profile) is editable but not deletable.
    """
    cache: ResourceDefinitionCache | None = getattr(app.state, "resource_definitions", None)
    if cache is None:
        return None
    root = cache.root()
    return root.schema_iri if root is not None else None


def _container_targets(cache: ResourceDefinitionCache) -> list[str]:
    """Graph IRIs of the typed (non-root) collection containers."""
    base = cache.base_url
    return [
        str(record_graph_uri(f"{base}/{rd.url_prefix}")) for rd in cache.all() if not rd.is_root
    ]


async def _warm_authz(app: FastAPI, iris: list[str]) -> None:
    """Pre-populate the anonymous READ authz cache for ``iris``.

    The first anonymous request to a public resource otherwise pays a
    cache-miss roundtrip (offer resolution + Postgres write). Failures are
    logged and swallowed — a triple-store hiccup must not block startup or a
    mutation. The cost ceiling is small (typically under 10 containers).
    """
    if not iris:
        return
    anon_ctx = RequestContext.anonymous(trace_id="authz-warmup")
    start = time.perf_counter()
    warmed = 0
    for iri in iris:
        try:
            await app.state.pdp.authorize(anon_ctx, Action.READ, iri)
            warmed += 1
        except Exception as err:
            log.warning("authz_warmup_target_failed", resource=iri, error=repr(err))
    log.info(
        "authz_warmup_completed",
        targets=len(iris),
        warmed=warmed,
        elapsed_ms=int((time.perf_counter() - start) * 1000),
    )


async def _warm_anonymous_authz_cache(app: FastAPI) -> None:
    """Warm the anonymous READ authz cache for the FDP root at startup.

    The root is the universal anonymous landing page. Typed container
    prefixes are warmed by :func:`_publish_resource_definitions` (during
    auto-bootstrap and on every runtime mutation), so this only needs the
    root.
    """
    base = get_settings().resolved_identifier_base
    await _warm_authz(app, [str(record_graph_uri(base + "/"))])


app: FastAPI
"""The default application, built lazily on first attribute access (PEP 562).

``uvicorn fdpneo_server.main:app`` and ``fastapi dev`` resolve the attribute
through :func:`__getattr__` below, so they behave exactly as if the app were
bound at module level. A bare ``import fdpneo_server.main`` no longer
constructs an application — it needs no configuration, builds no engines or
HTTP clients, and is safe for tools that only want a symbol (ADR-0023).
"""


def __getattr__(name: str) -> FastAPI:
    """Build the default app on first access instead of at import time."""
    if name == "app":
        global app
        app = create_app()
        return app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
