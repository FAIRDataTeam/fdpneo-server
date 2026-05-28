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

import httpx
import structlog
from fastapi import FastAPI

from fdp import __version__
from fdp.config import get_settings
from fdp.data.router import build_data_router
from fdp.identity import AuthenticationMiddleware, build_jwks_client
from fdp.metadata.profiles import (
    apply_profile,
    load_profile,
    ProfileStateRepository,
)
from fdp.metadata.repository import MetadataRepository
from fdp.metrics.api import build_metrics_router
from fdp.metrics.geo import open_geo_lookup
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
    app.state.offer_resolver = GraphBackedOfferResolver(app.state.metadata_repository)
    app.state.pdp = RequestScopedPDP(
        session_factory=app.state.session_factory,
        offer_resolver=app.state.offer_resolver,
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
    await _maybe_auto_bootstrap(app)

    # TODO: warm authorization cache for anonymous
    # TODO: subscribe audit-log handler to the event bus

    try:
        yield
    finally:
        log.info("fdp_stopping")
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

    app.add_middleware(
        AuthenticationMiddleware,
        oidc=settings.oidc,
        jwks_client_provider=lambda: app.state.jwks_client,
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

    # TODO: mount LDP router (metadata.api)
    # TODO: mount SPARQL endpoint (access.api)
    # TODO: mount admin / health endpoints

    @app.get("/healthz", tags=["internal"])
    async def healthz() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        """Liveness probe. Does not check downstream dependencies."""
        return {"status": "ok", "version": __version__}

    return app


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
        await apply_profile(
            profile,
            repository=app.state.metadata_repository,
            state=state,
            session=session,
            settings=settings,
            force=False,
        )


app = create_app()
