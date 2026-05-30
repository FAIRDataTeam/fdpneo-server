"""FastAPI application entrypoint.

Composes the four bounded contexts behind two HTTP surfaces (LDP REST API and
SPARQL endpoint) and registers cross-cutting middleware (auth, request context,
structured logging, OpenTelemetry).

The HTTP routers are owned by the modules they belong to; this file only
imports and mounts them, plus the lifespan handler that sets up shared
dependencies (storage adapters, OIDC JWKS cache, event-bus subscribers).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import structlog
from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from fdp import __version__
from fdp.access.router import build_sparql_router
from fdp.config import get_settings
from fdp.data.router import build_data_router
from fdp.identity import AuthenticationMiddleware, build_jwks_client
from fdp.identity.bootstrap import build_bootstrap_router
from fdp.metadata.audit import AuditLog
from fdp.metadata.ldp.router import build_ldp_router
from fdp.metadata.openapi import inject_resource_definition_paths
from fdp.metadata.profiles import (
    ProfileStateRepository,
    ResourceDefinitionCache,
    apply_profile,
    load_profile,
)
from fdp.metadata.repository import MetadataRepository
from fdp.metadata.shacl import ShaclValidator
from fdp.metadata.shape_provider import MetadataShapeProvider
from fdp.metrics.api import build_metrics_router
from fdp.metrics.geo import open_geo_lookup
from fdp.metrics.middleware import RequestObservationMiddleware
from fdp.metrics.pipeline import MetricsPipeline
from fdp.metrics.salt import SaltRotator
from fdp.policy.resolver import GraphBackedOfferResolver
from fdp.policy.runtime import RequestScopedPDP
from fdp.shared.errors import register_exception_handlers
from fdp.shared.events import EventBus
from fdp.shared.logging import configure_logging
from fdp.storage.postgres.engine import build_engine, build_session_factory
from fdp.storage.triplestore.adapter import TripleStoreAdapter

log = structlog.get_logger(__name__)


def _build_shared_state(app: FastAPI) -> None:
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

    app.state.event_bus = EventBus()

    # Triple store adapter + metadata repository, shared across requests.
    # Each request that needs the adapter borrows the singleton; the
    # underlying httpx client pools connections.
    app.state.triplestore = TripleStoreAdapter.from_settings(settings.triplestore)
    app.state.metadata_repository = MetadataRepository(app.state.triplestore)

    # PDP wrapper that opens a fresh Postgres session per call — required
    # because CacheRepository binds to one session, and the data/SPARQL
    # routers are concurrent. See fdp.policy.runtime.
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

    # Resource-definition cache — populated by the profile applier (CLI
    # apply or lifespan auto-bootstrap). Initialised None so attribute
    # access always succeeds; the OpenAPI generator and the LDP
    # container registry both no-op when it's not yet set.
    app.state.resource_definitions = None

    # SHACL validator backed by shapes stored in the metadata triple
    # store (architecture §5.3). Bootstrap-warming happens after the
    # profile is applied so the validator's parsed-shape cache is
    # populated against the IRIs the profile declares.
    app.state.shacl_validator = ShaclValidator(
        MetadataShapeProvider(app.state.metadata_repository)
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
    app.state.audit_log = AuditLog(session_factory=app.state.session_factory)


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
    app.state.audit_log.start(app.state.event_bus)
    await _maybe_auto_bootstrap(app)

    # TODO: warm authorization cache for anonymous

    try:
        yield
    finally:
        log.info("fdp_stopping")
        app.state.audit_log.stop()
        app.state.metrics_pipeline.stop()
        app.state.geo.close()
        await app.state.triplestore.close()
        await app.state.engine.dispose()
        await app.state.http_client.aclose()


def create_app() -> FastAPI:
    """Construct the FastAPI app.

    Kept as a function (rather than a module-level instance) so tests can build
    isolated apps with different settings.
    """
    settings = get_settings()
    configure_logging(settings.environment)

    app = FastAPI(
        title="FAIR Data Point",
        version=__version__,
        description="FAIR-aligned metadata repository — see /docs for the API.",
        lifespan=lifespan,
        debug=settings.environment == "development",
    )

    register_exception_handlers(app)
    _build_shared_state(app)

    # Middleware order: FastAPI's add_middleware registers outermost-first,
    # so the LAST add_middleware call runs INNERMOST. Putting the request
    # observer second means it executes inside AuthenticationMiddleware, so
    # the RequestContext ContextVar is bound when the observer snapshots
    # it (architecture §11.1 / ADR-0002 anonymization boundary).
    app.add_middleware(
        AuthenticationMiddleware,
        oidc=settings.oidc,
        jwks_client_provider=lambda: app.state.jwks_client,
    )
    app.add_middleware(
        RequestObservationMiddleware,
        bus_provider=lambda: app.state.event_bus,
    )

    # Route-registration order matters: the LDP router catches /{path:path}
    # under every method, so anything that should NOT resolve as an LDP
    # resource must be added first. Reserved-path prefixes the LDP router
    # cannot serve as a consequence: /healthz, /config, /metrics, /data, /sparql.
    @app.get("/healthz", tags=["internal"])
    async def healthz() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        """Liveness probe. Does not check downstream dependencies."""
        return {"status": "ok", "version": __version__}

    app.include_router(
        build_bootstrap_router(
            settings=settings,
            session_factory=app.state.session_factory,
        )
    )
    app.include_router(
        build_metrics_router(session_factory=app.state.session_factory)
    )
    app.include_router(
        build_data_router(
            repository=app.state.metadata_repository,
            pdp=app.state.pdp,
            adapter=app.state.triplestore,
            settings=settings.data,
            base_url=str(settings.base_url),
            http_client=app.state.http_client,
        )
    )
    app.include_router(
        build_sparql_router(
            pdp=app.state.pdp,
            adapter=app.state.triplestore,
        )
    )
    # LDP last — its /{path:path} catch-all matches every method/URL not
    # already claimed above. The container registry is a lazy adapter
    # that reads app.state.resource_definitions on every call so the
    # cache populated by auto-bootstrap (after lifespan startup) is
    # picked up without rebuilding the router. The ShaclValidator hook
    # (#2 of the operational-readiness plan) is still TODO; SHACL on
    # write stays off until that lands.
    app.include_router(
        build_ldp_router(
            repo=app.state.metadata_repository,
            pdp=app.state.pdp,
            validator=app.state.shacl_validator,
            containers=_DynamicContainerRegistry(app),
            event_bus=app.state.event_bus,
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
        cache: ResourceDefinitionCache | None = getattr(
            app.state, "resource_definitions", None
        )
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

    def _cache(self) -> ResourceDefinitionCache | None:
        return getattr(self._app.state, "resource_definitions", None)


async def _maybe_auto_bootstrap(app: FastAPI) -> None:
    """Apply the configured profile if both opt-in flags are set and the FDP is uninitialized.

    Failure here is fatal — per architecture §12.2 the FDP refuses to
    start if a configured profile fails to apply. Logging is structured
    so the operator can spot the cause in their startup log.
    """
    settings = get_settings()
    if not settings.profile.auto_apply or settings.profile.path is None:
        return

    bundle = settings.profile.path
    log.info("profile_auto_bootstrap_start", bundle=str(bundle))
    profile = load_profile(bundle)

    async with app.state.session_factory() as session:
        state = ProfileStateRepository(session)
        if await state.is_applied():
            log.info(
                "profile_auto_bootstrap_skipped",
                reason="already_applied",
                profile=profile.name,
            )
            return
        report = await apply_profile(
            profile,
            repository=app.state.metadata_repository,
            state=state,
            session=session,
            settings=settings,
            force=False,
        )

    # Publish what auto-bootstrap learned so the runtime stops returning
    # the early-startup defaults:
    #
    # * ``system_default_offer_iri`` — read lazily by the offer resolver's
    #   inheritance fallback (architecture §8.3).
    # * ``resource_definitions`` — read by the LDP container registry
    #   and the OpenAPI generator.
    # * ``openapi_schema`` cleared so the next ``/openapi.json`` request
    #   rebuilds the spec with the typed paths.
    # * SHACL validator warmed against the declared schema IRIs so the
    #   first write per type doesn't pay a triple-store roundtrip.
    if report.system_default_offer_iri is not None:
        app.state.system_default_offer_iri = report.system_default_offer_iri
    if report.resource_definitions is not None:
        app.state.resource_definitions = report.resource_definitions
        app.openapi_schema = None  # type: ignore[assignment]
        schema_iris = sorted({rd.schema_iri for rd in report.resource_definitions.all()})
        try:
            await app.state.shacl_validator.bootstrap(schema_iris)
        except Exception as err:
            log.warning(
                "shacl_validator_bootstrap_failed",
                error=repr(err),
                shape_iris=schema_iris,
            )
        log.info(
            "profile_auto_bootstrap_completed",
            profile=profile.name,
            resource_definitions=len(report.resource_definitions.all()),
            shapes_warmed=len(schema_iris),
            system_default_offer=report.system_default_offer_iri,
        )


app = create_app()
