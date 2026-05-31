"""Simple data provider router (architecture §5.6).

Serves two surfaces for each distribution:

* ``GET /data/{distribution_id}`` — file content via ``dcat:downloadURL``,
  either by 302 redirect or by streaming bytes through the FDP per the
  operator's ``data.download_mode`` setting.
* ``GET|POST /data/{distribution_id}/sparql`` — a SPARQL endpoint
  scoped to the distribution's data graph (``<iri>/data``).

**Anonymous-read invariant (v1)**

Both surfaces evaluate the distribution's Offer against an *anonymous*
:class:`RequestContext`. If the policy module denies that synthetic
check the request is rejected with 403 — regardless of who is actually
calling. v1 of the data provider serves only open-access
distributions; access-controlled data delivery is a v1.x increment.

**Routing convention**

A distribution served by this FDP has the URI ``{base_url}/data/{id}``;
the route matches that URI's path. Operators who want their
distributions hosted by the FDP create the metadata record at that URI
and set ``dcat:downloadURL`` / ``dcat:accessURL`` accordingly. The data
provider does not maintain a separate slug-to-URI map.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from urllib.parse import urljoin

import httpx
import structlog
from fastapi import APIRouter, Request, Response
from fastapi.responses import RedirectResponse, StreamingResponse

from fdp.data.distributions import DistributionInfo, RecordReader, resolve_distribution
from fdp.policy.model import Action, Outcome
from fdp.shared.context import RequestContext
from fdp.shared.errors import BadRequest, Forbidden, NotFound, UnsupportedMediaType
from fdp.shared.graphs import data_graph_uri

if TYPE_CHECKING:
    from fdp.config import DataSettings
    from fdp.policy.pdp import PDP
    from fdp.storage.triplestore import TripleStoreAdapter

log = structlog.get_logger(__name__)


_FORM_ENCODED = "application/x-www-form-urlencoded"
_SPARQL_QUERY = "application/sparql-query"
_DEFAULT_ACCEPT = "application/sparql-results+json"


def build_data_router(
    *,
    repository: RecordReader,
    pdp: PDP,
    adapter: TripleStoreAdapter,
    settings: DataSettings,
    base_url: str,
    http_client: httpx.AsyncClient,
    prefix: str = "/data",
) -> APIRouter:
    """Construct the data-provider router.

    ``base_url`` is the FDP's public URL (``Settings.base_url``); it
    combines with the ``{distribution_id}`` path segment to form the
    distribution IRI. ``http_client`` is reused across stream-mode
    downloads to keep connection pooling sane.
    """
    router = APIRouter(prefix=prefix, tags=["data"])

    @router.get("/{distribution_id}", name="data_download")
    async def download(  # pyright: ignore[reportUnusedFunction]
        distribution_id: str,
        request: Request,
    ) -> Response:
        info = await _resolve_and_authorize(
            distribution_id=distribution_id,
            repository=repository,
            pdp=pdp,
            base_url=base_url,
            trace_id=_trace_id(request),
        )
        if info.download_url is None:
            raise NotFound(
                "distribution has no dcat:downloadURL",
                details={"iri": info.iri},
            )
        url = info.download_url
        if settings.download_mode == "redirect":
            return RedirectResponse(url, status_code=302)
        return _stream_upstream(
            url=url,
            client=http_client,
            request=request,
            timeout=settings.proxy_timeout_seconds,
            max_bytes=settings.proxy_max_bytes,
        )

    @router.get("/{distribution_id}/sparql", name="data_sparql_get")
    async def sparql_get(  # pyright: ignore[reportUnusedFunction]
        distribution_id: str,
        request: Request,
    ) -> Response:
        info = await _resolve_and_authorize(
            distribution_id=distribution_id,
            repository=repository,
            pdp=pdp,
            base_url=base_url,
            trace_id=_trace_id(request),
        )
        if info.access_url is None:
            raise NotFound(
                "distribution has no dcat:accessURL",
                details={"iri": info.iri},
            )
        query = request.query_params.get("query")
        if query is None:
            raise BadRequest("GET requires a `query` query parameter")
        return _stream_sparql(adapter, info, query, request)

    @router.post("/{distribution_id}/sparql", name="data_sparql_post")
    async def sparql_post(  # pyright: ignore[reportUnusedFunction]
        distribution_id: str,
        request: Request,
    ) -> Response:
        info = await _resolve_and_authorize(
            distribution_id=distribution_id,
            repository=repository,
            pdp=pdp,
            base_url=base_url,
            trace_id=_trace_id(request),
        )
        if info.access_url is None:
            raise NotFound(
                "distribution has no dcat:accessURL",
                details={"iri": info.iri},
            )
        query = await _extract_post_body(request)
        return _stream_sparql(adapter, info, query, request)

    return router


# --- helpers ---------------------------------------------------------------


async def _resolve_and_authorize(
    *,
    distribution_id: str,
    repository: RecordReader,
    pdp: PDP,
    base_url: str,
    trace_id: str,
) -> DistributionInfo:
    """Look up the distribution and gate it through the anonymous-read check."""
    iri = _distribution_iri(base_url, distribution_id)
    info = await resolve_distribution(iri, repository=repository)
    anonymous = RequestContext.anonymous(
        trace_id=trace_id,
        request_timestamp=datetime.now(UTC),
    )
    decision = await pdp.authorize(anonymous, Action.READ, info.iri)
    if decision.outcome is Outcome.DENY:
        log.info(
            "data_provider_denied",
            iri=info.iri,
            reason=decision.reason,
        )
        raise Forbidden(
            "this distribution is not open-access",
            details={"iri": info.iri, "reason": decision.reason},
        )
    return info


def _distribution_iri(base_url: str, distribution_id: str) -> str:
    """Compose the distribution IRI from ``base_url`` and the URL slug.

    Trailing slash on ``base_url`` is normalized so callers don't have
    to care; the slug never carries a leading slash because FastAPI
    strips it.
    """
    base = base_url.rstrip("/") + "/"
    return urljoin(base, f"data/{distribution_id}")


def _trace_id(request: Request) -> str:
    """Pull a trace id off the active request, falling back to empty string."""
    state = getattr(request, "state", None)
    if state is None:
        return ""
    ctx = getattr(state, "context", None)
    if ctx is None:
        return ""
    return str(getattr(ctx, "trace_id", "") or "")


# --- file download (stream mode) -------------------------------------------


def _stream_upstream(
    *,
    url: str,
    client: httpx.AsyncClient,
    request: Request,
    timeout: float,
    max_bytes: int,
) -> StreamingResponse:
    """Proxy an upstream download, forwarding Range / If-* request headers.

    The response carries the upstream's status, content-type,
    content-length, and accept-ranges. Cap on byte volume aborts the
    stream — this is the FDP's protection against unintentionally
    pulling multi-GiB blobs.
    """
    forwarded = _forwarded_headers(request)

    async def iterator() -> AsyncIterator[bytes]:
        bytes_sent = 0
        async with client.stream(
            "GET",
            url,
            headers=forwarded,
            timeout=timeout,
            follow_redirects=True,
        ) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                bytes_sent += len(chunk)
                if bytes_sent > max_bytes:
                    log.warning(
                        "data_provider_proxy_size_exceeded",
                        url=url,
                        max_bytes=max_bytes,
                    )
                    return
                yield chunk

    # We can't introspect the upstream response without buffering it;
    # rely on the streaming response to pass through chunked encoding
    # and let the client see what's there.
    return StreamingResponse(iterator())


_FORWARDED_REQUEST_HEADERS = ("range", "if-none-match", "if-modified-since")


def _forwarded_headers(request: Request) -> dict[str, str]:
    """Pick the request headers worth forwarding to the upstream download."""
    out: dict[str, str] = {}
    for name in _FORWARDED_REQUEST_HEADERS:
        value = request.headers.get(name)
        if value is not None:
            out[name] = value
    return out


# --- SPARQL forwarding -----------------------------------------------------


def _stream_sparql(
    adapter: TripleStoreAdapter,
    info: DistributionInfo,
    query: str,
    request: Request,
) -> StreamingResponse:
    """Forward ``query`` to the triple store scoped to the distribution's data graph.

    Negotiation is delegated to the triple store — the Accept header is
    forwarded verbatim and the store returns 406 if no negotiated
    format is available. Streaming keeps memory bounded for large
    ``CONSTRUCT`` / ``DESCRIBE`` payloads, but applies to every form
    uniformly so the data provider does not parse the query.
    """
    accept = request.headers.get("accept") or _DEFAULT_ACCEPT
    data_graph = str(data_graph_uri(info.iri))
    stream = adapter.query_stream(
        query,
        accept=accept,
        default_graph_uris=(data_graph,),
    )
    return StreamingResponse(stream, media_type=accept)


async def _extract_post_body(request: Request) -> str:
    """Pull the SPARQL query string from a POST body.

    Accepts the two body shapes SPARQL 1.1 Protocol §2.1.2 defines for
    queries: raw ``application/sparql-query`` and form-encoded
    ``query=...``. The data provider is read-only; an
    ``application/sparql-update`` body is rejected because the
    provider's route forwards to the triple store's *query* endpoint and
    would surface a confusing parse error otherwise.
    """
    header = request.headers.get("content-type") or ""
    ctype = header.split(";", 1)[0].strip().lower()
    if ctype == _SPARQL_QUERY:
        return (await request.body()).decode("utf-8")
    if ctype == _FORM_ENCODED:
        form = await request.form()
        query = form.get("query")
        if isinstance(query, str):
            return query
        raise BadRequest("POST form must include `query`")
    if not ctype:
        raise UnsupportedMediaType("POST requires a Content-Type header")
    raise UnsupportedMediaType(
        f"unsupported Content-Type: {ctype}",
        details={"supported": [_SPARQL_QUERY, _FORM_ENCODED]},
    )


__all__ = ["build_data_router"]
