"""SPARQL endpoint router (architecture §9; ADR-0004).

Composes the parser → rewriter → triple-store-adapter pipeline behind a
single FastAPI route at the configured prefix (default ``/sparql``).

The endpoint accepts the three request forms documented in SPARQL 1.1
Protocol:

* ``GET /sparql?query=…`` — read queries via the query string.
* ``POST /sparql`` with ``Content-Type: application/sparql-query`` —
  raw read body.
* ``POST /sparql`` with ``Content-Type: application/sparql-update`` —
  raw update body.
* ``POST /sparql`` with ``Content-Type: application/x-www-form-urlencoded``
  — either ``query=…`` or ``update=…`` form field.

**Authorization pipeline**

1. Extract SPARQL text from the request (form / raw body / query param).
2. :func:`fdp.access.parser.parse` classifies it.
3. Anonymous updates → 401.
4. Reads → :func:`fdp.access.rewriter.rewrite_read` against
   ``pdp.authorized_graphs(ctx, Action.READ)``; protocol-level dataset
   override is forwarded to the triple store.
5. Updates → :func:`fdp.access.rewriter.authorize_update` against
   ``pdp.authorized_graphs(ctx, Action.MODIFY)``; the WHERE clauses of
   the update are additionally scoped to the user's authorized read set
   via ``using-named-graph-uri`` so a write to an authorized target
   cannot leak data from outside that set.
6. ``CONSTRUCT`` / ``DESCRIBE`` responses stream from the adapter
   through to the client; ``SELECT`` / ``ASK`` answers are small JSON /
   XML / CSV / TSV documents and are returned whole.

The PEP for reads is the rewriter itself — there is no single resource
to authorize against; the dataset projection IS the enforcement (§9).
For updates the per-target check in
:func:`fdp.access.rewriter.authorize_update` plays that role.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

import structlog
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import StreamingResponse

from fdp.access.parser import ParsedRead, ParsedUpdate, QueryForm, parse
from fdp.access.results import select_result_media_type
from fdp.access.rewriter import RewrittenRead, authorize_update, rewrite_read
from fdp.identity.deps import current_context
from fdp.policy.model import Action
from fdp.shared.context import RequestContext
from fdp.shared.errors import (
    BadRequest,
    MethodNotAllowed,
    NotAcceptable,
    ServiceUnavailable,
    Unauthenticated,
    UnsupportedMediaType,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from fdp.metadata.lifecycle import StateGate
    from fdp.policy.pdp import PDP
    from fdp.storage.triplestore import TripleStoreAdapter

log = structlog.get_logger(__name__)


_FORM_ENCODED = "application/x-www-form-urlencoded"
_SPARQL_QUERY = "application/sparql-query"
_SPARQL_UPDATE = "application/sparql-update"


def build_sparql_router(
    *,
    pdp: PDP,
    adapter: TripleStoreAdapter,
    state_gate: StateGate | None = None,
    multigraph_safe_provider: Callable[[], bool] = lambda: True,
    prefix: str = "/sparql",
) -> APIRouter:
    """Build the SPARQL endpoint router wired with ``pdp`` + ``adapter``.

    ``state_gate`` is optional (Phase 12 / ADR-0010). When supplied, the read
    projection is the publication-state-visible subset of the ODRL read set
    (anonymous sees only ``PUBLISHED`` graphs); updates are unaffected — an
    authenticated writer's WHERE still observes their full authorized-read set.

    ``multigraph_safe_provider`` reports whether the triple store passed the
    named-graph isolation self-test (audit R-03). When it returns ``False``, a
    read whose authorized projection spans more than one named graph is refused
    (fail closed) rather than risk the store leaking unauthorized graphs.
    """
    router = APIRouter(prefix=prefix, tags=["sparql"])

    async def _serve(
        request: Request,
        ctx: RequestContext,
        sparql: str,
    ) -> Response:
        parsed = parse(sparql)
        if isinstance(parsed, ParsedUpdate):
            return await _handle_update(ctx, sparql, parsed)
        return await _handle_read(request, ctx, sparql, parsed)

    async def _handle_update(ctx: RequestContext, sparql: str, parsed: ParsedUpdate) -> Response:
        if ctx.is_anonymous:
            raise Unauthenticated("authentication required for SPARQL updates")
        authorized_modify = await pdp.authorized_graphs(ctx, Action.MODIFY)
        authorize_update(parsed, authorized_modify=authorized_modify)
        authorized_read = await pdp.authorized_graphs(ctx, Action.READ)
        log.info(
            "sparql_update",
            subject=ctx.subject,
            targets=list(parsed.targets),
        )
        await adapter.update(
            sparql,
            using_named_graph_uris=tuple(sorted(authorized_read)),
        )
        return Response(status_code=204)

    async def _handle_read(
        request: Request,
        ctx: RequestContext,
        sparql: str,
        parsed: ParsedRead,
    ) -> Response:
        media = select_result_media_type(parsed.form, request.headers.get("accept"))
        if media is None:
            raise NotAcceptable(
                f"no supported result media type for {parsed.form.value}",
                details={"form": parsed.form.value},
            )
        authorized_read = (
            await state_gate.visible_read_graphs(ctx)
            if state_gate is not None
            else await pdp.authorized_graphs(ctx, Action.READ)
        )
        rewritten = rewrite_read(parsed, authorized_read=authorized_read)
        if len(rewritten.named_graph_uris) > 1 and not multigraph_safe_provider():
            # The store failed the named-graph isolation self-test, so projecting
            # multiple graphs via repeated named-graph-uri could leak unauthorized
            # graphs. Fail closed (audit R-03). Single-graph reads remain allowed.
            raise ServiceUnavailable(
                "multi-graph SPARQL reads are disabled: the triple store failed "
                "the named-graph isolation self-test; use a conformant store "
                "(GraphDB/Fuseki)",
                details={"named_graphs": len(rewritten.named_graph_uris)},
            )
        if parsed.form in (QueryForm.CONSTRUCT, QueryForm.DESCRIBE):
            return _stream_graph_response(sparql, parsed, rewritten, media)
        body = await adapter.query(
            sparql,
            accept=media,
            default_graph_uris=rewritten.default_graph_uris,
            named_graph_uris=rewritten.named_graph_uris,
        )
        return Response(content=body, media_type=media)

    def _stream_graph_response(
        sparql: str,
        parsed: ParsedRead,
        rewritten: RewrittenRead,
        media: str,
    ) -> StreamingResponse:
        del parsed  # CONSTRUCT/DESCRIBE chosen at the caller; param kept for clarity
        stream = adapter.query_stream(
            sparql,
            accept=media,
            default_graph_uris=rewritten.default_graph_uris,
            named_graph_uris=rewritten.named_graph_uris,
        )
        return StreamingResponse(stream, media_type=media)

    @router.get("", name="sparql_get")
    async def sparql_get(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        ctx: Annotated[RequestContext, Depends(current_context)],
    ) -> Response:
        query = request.query_params.get("query")
        if query is None:
            raise BadRequest("GET /sparql requires a `query` query parameter")
        return await _serve(request, ctx, query)

    @router.post("", name="sparql_post")
    async def sparql_post(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        ctx: Annotated[RequestContext, Depends(current_context)],
    ) -> Response:
        sparql = await _extract_post_body(request)
        return await _serve(request, ctx, sparql)

    return router


async def _extract_post_body(request: Request) -> str:
    """Pull the SPARQL string out of a POST per Protocol §2.1.2 / §2.2.2."""
    header = request.headers.get("content-type") or ""
    ctype = header.split(";", 1)[0].strip().lower()
    if ctype in (_SPARQL_QUERY, _SPARQL_UPDATE):
        return (await request.body()).decode("utf-8")
    if ctype == _FORM_ENCODED:
        form = await request.form()
        update = form.get("update")
        if isinstance(update, str):
            return update
        query = form.get("query")
        if isinstance(query, str):
            return query
        raise BadRequest("POST form must include `query` or `update`")
    if not ctype:
        raise UnsupportedMediaType("POST /sparql requires a Content-Type header")
    raise UnsupportedMediaType(
        f"unsupported Content-Type: {ctype}",
        details={
            "supported": [_SPARQL_QUERY, _SPARQL_UPDATE, _FORM_ENCODED],
        },
    )


# Re-export so callers importing the module's symbols don't have to know
# which exception types the router raises.
_ = MethodNotAllowed


__all__ = ["build_sparql_router"]
