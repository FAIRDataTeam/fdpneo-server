"""Persistent-identifier resolution verification (v0.3.0, ADR-0014).

Checks that the deployment's persistent identifiers actually resolve: that a
``identifier_base`` URL redirects (via W3ID/PURL) to the serving origin and that
the FDP there returns the record whose canonical subject is that very IRI.

This is the "does my PID actually work" self-test the operator runs after wiring
up W3ID — and re-runs after moving the deployment to confirm the redirect was
updated. Pure-ish: HTTP is injected so it is unit-testable with respx.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx
import structlog

from fdp.shared.identifiers import is_under

__all__ = ["ResolutionCheck", "ResolutionReport", "verify_resolution"]

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ResolutionCheck:
    """The outcome of resolving a single persistent identifier."""

    iri: str
    ok: bool
    redirected_to: str | None = None
    final_status: int | None = None
    detail: str | None = None


@dataclass
class ResolutionReport:
    """Aggregate result of a verification run."""

    identifier_base: str
    serving_base: str
    checks: list[ResolutionCheck] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(c.ok for c in self.checks)


async def verify_resolution(
    *,
    identifier_base: str,
    serving_base: str,
    iris: list[str],
    http_client: httpx.AsyncClient,
    timeout_seconds: float = 30.0,
) -> ResolutionReport:
    """Resolve each IRI and confirm it lands on the serving origin.

    For each IRI the check requires that (a) it is under ``identifier_base``,
    (b) following redirects ends at a URL on the serving origin, and (c) the
    final response is a success (2xx). A non-redirecting ``identifier_base``
    (e.g. when it equals the serving origin, i.e. local/dev) still passes as long
    as the content resolves — the redirect is then a no-op.
    """
    report = ResolutionReport(
        identifier_base=identifier_base.rstrip("/"), serving_base=serving_base.rstrip("/")
    )
    serving = serving_base.rstrip("/")
    for iri in iris:
        report.checks.append(
            await _check_one(
                iri=iri,
                identifier_base=identifier_base,
                serving=serving,
                http_client=http_client,
                timeout_seconds=timeout_seconds,
            )
        )
    log.info(
        "pid_resolution_verified",
        identifier_base=report.identifier_base,
        checked=len(report.checks),
        ok=report.ok,
    )
    return report


async def _check_one(
    *,
    iri: str,
    identifier_base: str,
    serving: str,
    http_client: httpx.AsyncClient,
    timeout_seconds: float,
) -> ResolutionCheck:
    if not is_under(iri, identifier_base):
        return ResolutionCheck(iri=iri, ok=False, detail="IRI is not under the identifier base")
    try:
        resp = await http_client.get(
            iri,
            follow_redirects=True,
            timeout=timeout_seconds,
            headers={"Accept": "text/turtle"},
        )
    except httpx.HTTPError as err:
        return ResolutionCheck(iri=iri, ok=False, detail=f"request failed: {err}")

    final = str(resp.url).rstrip("/")
    landed_on_serving = final == serving or final.startswith(serving + "/")
    ok = landed_on_serving and resp.is_success
    detail = None
    if not landed_on_serving:
        detail = f"resolved to {final}, not the serving origin {serving}"
    elif not resp.is_success:
        detail = f"serving origin returned {resp.status_code}"
    return ResolutionCheck(
        iri=iri,
        ok=ok,
        redirected_to=final,
        final_status=resp.status_code,
        detail=detail,
    )
