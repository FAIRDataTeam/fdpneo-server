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
from typing import TYPE_CHECKING

import httpx
import structlog
from fastapi import FastAPI

from fdp import __version__
from fdp.config import Settings, get_settings
from fdp.identity import AuthenticationMiddleware, build_jwks_client
from fdp.metrics.geo import open_geo_lookup
from fdp.metrics.pipeline import MetricsPipeline
from fdp.metrics.salt import SaltRotator
from fdp.shared.errors import register_exception_handlers
from fdp.shared.events import EventBus
from fdp.shared.logging import configure_logging
from fdp.storage.postgres.engine import build_engine, build_session_factory

if TYPE_CHECKING:
    pass

log = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan: initialize shared resources, yield, then clean up.

    Initialization order matters. Storage adapters come first because everything
    else depends on them. The PDP warms its authorization cache after storage is
    ready. The metrics subscriber binds to the event bus last so it captures
    events from startup operations.
    """
    settings: Settings = get_settings()
    log.info("fdp_starting", version=__version__, env=settings.environment)

    http_client = httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=2.0))
    jwks_client = build_jwks_client(settings.oidc, http_client)
    app.state.http_client = http_client
    app.state.jwks_client = jwks_client

    # Postgres engine + async session factory; shared by every Postgres-backed
    # module via app.state.session_factory.
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    app.state.engine = engine
    app.state.session_factory = session_factory

    # In-process event bus; producers (metadata, identity, access, data) publish
    # here, subscribers (metrics, audit log) bind on startup.
    bus = EventBus()
    app.state.event_bus = bus

    # Metrics pipeline owns the geo lookup and salt rotator. open_geo_lookup
    # degrades to a no-op if the GeoLite2 DB is absent, so dev startup never
    # hard-fails on this.
    geo = open_geo_lookup(settings.metrics.geoip_database_path)
    salt_rotator = SaltRotator()
    metrics_pipeline = MetricsPipeline(
        session_factory=session_factory,
        geo=geo,
        salt_rotator=salt_rotator,
        enabled=settings.metrics.enabled,
        counting_enabled=settings.metrics.unique_visitor_counting,
    )
    metrics_pipeline.start(bus)
    app.state.metrics_pipeline = metrics_pipeline

    # TODO: initialize triple store adapter
    # TODO: initialize PDP and warm authorization cache for anonymous
    # TODO: subscribe audit-log handler to the event bus

    try:
        yield
    finally:
        log.info("fdp_stopping")
        metrics_pipeline.stop()
        geo.close()
        await engine.dispose()
        await http_client.aclose()


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

    app.add_middleware(
        AuthenticationMiddleware,
        oidc=settings.oidc,
        jwks_client_provider=lambda: app.state.jwks_client,
    )

    # TODO: mount LDP router (metadata.api)
    # TODO: mount SPARQL endpoint (access.api)
    # TODO: mount metrics dashboard API (metrics.api)
    # TODO: mount data provider API (data.api)
    # TODO: mount admin / health endpoints

    @app.get("/healthz", tags=["internal"])
    async def healthz() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        """Liveness probe. Does not check downstream dependencies."""
        return {"status": "ok", "version": __version__}

    return app


app = create_app()
