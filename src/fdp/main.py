"""FastAPI application entrypoint.

Composes the four bounded contexts behind two HTTP surfaces (LDP REST API and
SPARQL endpoint) and registers cross-cutting middleware (auth, request context,
structured logging, OpenTelemetry).

The HTTP routers are owned by the modules they belong to; this file only
imports and mounts them, plus the lifespan handler that sets up shared
dependencies (storage adapters, OIDC JWKS cache, event-bus subscribers).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import structlog
from fastapi import FastAPI

from fdp import __version__
from fdp.config import Settings, get_settings
from fdp.shared.errors import register_exception_handlers
from fdp.shared.logging import configure_logging

if TYPE_CHECKING:
    pass

log = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan: initialize shared resources, yield, then clean up.

    Initialization order matters. Storage adapters come first because everything
    else depends on them. The PDP warms its authorization cache after storage is
    ready. The metrics subscriber binds to the event bus last so it captures
    events from startup operations.
    """
    settings: Settings = get_settings()
    log.info("fdp_starting", version=__version__, env=settings.environment)

    # TODO: initialize storage adapters (triple store + Postgres pool)
    # TODO: initialize OIDC JWKS cache
    # TODO: initialize PDP and warm authorization cache for anonymous
    # TODO: subscribe metrics handler to the event bus
    # TODO: subscribe audit-log handler to the event bus

    try:
        yield
    finally:
        log.info("fdp_stopping")
        # TODO: close storage connections, flush metrics buckets


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

    # TODO: register auth middleware (identity.middleware)
    # TODO: register request-context middleware (shared.context)
    # TODO: mount LDP router (metadata.api)
    # TODO: mount SPARQL endpoint (access.api)
    # TODO: mount metrics dashboard API (metrics.api)
    # TODO: mount data provider API (data.api)
    # TODO: mount admin / health endpoints

    @app.get("/healthz", tags=["internal"])
    async def healthz() -> dict[str, str]:
        """Liveness probe. Does not check downstream dependencies."""
        return {"status": "ok", "version": __version__}

    return app


app = create_app()
