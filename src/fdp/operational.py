"""Operational endpoints: ``/info`` (task 13.1) and ``/readyz`` (task 13.2).

These are composition-level concerns — they cut across every bounded
context — so they live in the same composition layer as ``main.py``
rather than inside any one module.

* ``/healthz`` (in ``main.py``) stays liveness-only. It returns 200 as
  long as the process can serve HTTP, regardless of downstream state.
  Kubernetes uses this for restarts.
* ``/readyz`` performs a fan-out probe over Postgres, the triple store,
  and the OIDC discovery endpoint. It returns 503 if any check fails so
  the platform can route traffic away from a partially-broken pod.
* ``/info`` is a pure read of build/deployment metadata. The
  ``fdp-client`` footer surfaces ``version`` and ``commit`` from here.

The readiness response is always structured (``{status, checks:
{name: {status, ...}}}``) regardless of overall outcome, so the
client can render a meaningful "what's down" page without parsing
prose.
"""

from __future__ import annotations

import asyncio
import os
import platform
import time
from typing import TYPE_CHECKING, Literal

import httpx
import structlog
from fastapi import APIRouter, Response
from pydantic import BaseModel
from sqlalchemy import text

from fdp import __version__

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from fdp.config import Settings
    from fdp.storage.triplestore.adapter import TripleStoreAdapter

log = structlog.get_logger(__name__)


# How long each readiness probe is allowed to run before we declare the
# dependency unreachable. Kept short so Kubernetes-style probes don't
# pile up requests behind a hung backend.
_PROBE_TIMEOUT_SECONDS = 2.0


# --- response models -------------------------------------------------------


class BuildInfo(BaseModel):
    """Build-time metadata.

    All fields are optional because they are populated from environment
    variables the build pipeline sets (``FDP_BUILD_COMMIT``,
    ``FDP_BUILD_BUILT_AT``). A locally-developed checkout has no values
    and the client renders "(unknown build)".
    """

    commit: str | None = None
    built_at: str | None = None


class RuntimeInfo(BaseModel):
    """Runtime metadata: which Python is the server actually running on."""

    python_version: str


class AppInfo(BaseModel):
    """Response shape for ``GET /info``."""

    name: str
    version: str
    environment: str
    build: BuildInfo
    runtime: RuntimeInfo


class CheckOutcome(BaseModel):
    """One dependency's probe result."""

    status: Literal["ok", "fail"]
    latency_ms: int | None = None
    error: str | None = None


class ReadinessReport(BaseModel):
    """Response shape for ``GET /readyz``."""

    status: Literal["ready", "not_ready"]
    checks: dict[str, CheckOutcome]


# --- /info -----------------------------------------------------------------


def _build_info() -> BuildInfo:
    return BuildInfo(
        commit=os.environ.get("FDP_BUILD_COMMIT") or None,
        built_at=os.environ.get("FDP_BUILD_BUILT_AT") or None,
    )


def _runtime_info() -> RuntimeInfo:
    return RuntimeInfo(python_version=platform.python_version())


def build_info_router(*, settings: Settings) -> APIRouter:
    """Router exposing ``GET /info`` — deployment self-description.

    No I/O. Reads environment + settings only. Unauthenticated so the
    client can render a footer pre-login. No secrets — anything
    deployment-sensitive (database URLs, signing keys) must not be
    added here.
    """
    router = APIRouter(tags=["internal"])

    @router.get("/info", response_model=AppInfo, name="app_info")
    async def app_info() -> AppInfo:  # pyright: ignore[reportUnusedFunction]
        return AppInfo(
            name="fdp-server",
            version=__version__,
            environment=settings.environment,
            build=_build_info(),
            runtime=_runtime_info(),
        )

    return router


# --- /readyz ---------------------------------------------------------------


async def _check_postgres(
    session_factory: async_sessionmaker[AsyncSession],
) -> CheckOutcome:
    start = time.perf_counter()
    try:
        async with session_factory() as session:
            await asyncio.wait_for(
                session.execute(text("SELECT 1")), timeout=_PROBE_TIMEOUT_SECONDS
            )
    except Exception as exc:
        return CheckOutcome(status="fail", error=_short_error(exc))
    return CheckOutcome(status="ok", latency_ms=int((time.perf_counter() - start) * 1000))


async def _check_triplestore(adapter: TripleStoreAdapter) -> CheckOutcome:
    start = time.perf_counter()
    try:
        await asyncio.wait_for(adapter.ask("ASK { ?s ?p ?o }"), timeout=_PROBE_TIMEOUT_SECONDS)
    except Exception as exc:
        return CheckOutcome(status="fail", error=_short_error(exc))
    return CheckOutcome(status="ok", latency_ms=int((time.perf_counter() - start) * 1000))


async def _check_oidc(
    http_client: httpx.AsyncClient,
    issuer: str,
) -> CheckOutcome:
    """Probe the OIDC discovery endpoint.

    We don't need to validate the discovery document's contents here
    (the auth middleware does that on every request) — we just confirm
    the IdP is reachable and returning a non-error status.
    """
    start = time.perf_counter()
    url = f"{issuer.rstrip('/')}/.well-known/openid-configuration"
    try:
        response = await asyncio.wait_for(http_client.get(url), timeout=_PROBE_TIMEOUT_SECONDS)
    except Exception as exc:
        return CheckOutcome(status="fail", error=_short_error(exc))
    if response.status_code >= 400:
        return CheckOutcome(status="fail", error=f"discovery returned HTTP {response.status_code}")
    return CheckOutcome(status="ok", latency_ms=int((time.perf_counter() - start) * 1000))


def _short_error(exc: BaseException) -> str:
    """Render an exception for the readiness response body.

    Returns at most one short line. We do not want stack traces leaking
    through readiness probes; the structured log records the full
    detail.
    """
    name = type(exc).__name__
    message = str(exc).splitlines()[0] if str(exc) else ""
    return f"{name}: {message}" if message else name


def build_readiness_router(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    adapter: TripleStoreAdapter,
    http_client: httpx.AsyncClient,
    issuer: str,
) -> APIRouter:
    """Router exposing ``GET /readyz`` — Kubernetes-style readiness probe.

    Runs the three dependency checks concurrently, returns 200 if all
    pass and 503 otherwise. The response body is structured either
    way so a client can render which dependency is down.
    """
    router = APIRouter(tags=["internal"])

    @router.get(
        "/readyz",
        response_model=ReadinessReport,
        responses={503: {"model": ReadinessReport}},
        name="readiness",
    )
    async def readyz(response: Response) -> ReadinessReport:  # pyright: ignore[reportUnusedFunction]
        postgres_check, triplestore_check, oidc_check = await asyncio.gather(
            _check_postgres(session_factory),
            _check_triplestore(adapter),
            _check_oidc(http_client, issuer),
        )
        checks = {
            "postgres": postgres_check,
            "triplestore": triplestore_check,
            "oidc": oidc_check,
        }
        all_ok = all(c.status == "ok" for c in checks.values())
        report = ReadinessReport(
            status="ready" if all_ok else "not_ready",
            checks=checks,
        )
        if not all_ok:
            response.status_code = 503
            log.warning(
                "readiness_probe_failed",
                checks={name: c.error for name, c in checks.items() if c.status == "fail"},
            )
        return report

    return router


__all__ = [
    "AppInfo",
    "BuildInfo",
    "CheckOutcome",
    "ReadinessReport",
    "RuntimeInfo",
    "build_info_router",
    "build_readiness_router",
]
