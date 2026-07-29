"""LDP read-extension endpoints (task 2.6).

Three extension surfaces per resource definition that the catch-all
LDP router cannot answer because each one means something the LDP spec
does not:

* ``GET /spec`` / ``GET /{urlPrefix}/spec`` / ``GET /{urlPrefix}/{id}/spec``
  — return the SHACL NodeShape graph that validates instances of the
  type. ``fdp-client`` uses this to render create / edit forms (its
  task 7.5). The type-level variant (no ``{id}``) is the one create
  forms need — no instance exists yet — and is the surface that the
  existing :mod:`fdpneo_server.metadata.openapi` advertises only at instance
  level.

* ``GET /expanded`` / ``GET /{urlPrefix}/{id}/expanded`` — the record
  graph merged with every ancestor reachable through ``dct:isPartOf``.
  Saves the client from doing the walk itself.

* ``GET /page/{childPrefix}`` /
  ``GET /{urlPrefix}/{id}/page/{childPrefix}`` — a paginated listing
  of the children of a given type. The current client lists via SPARQL
  ``GRAPH ?g``; this endpoint removes that named-graph-name coupling.

These routes must register **before** the LDP ``/{path:path}``
catch-all in :mod:`fdpneo_server.main`, otherwise the catch-all eats them and the
client gets 404/401.

Auth model:

* ``/spec`` is anonymous — the SHACL shape is metadata about the
  schema, not user data, and the client needs it to render an
  unauthenticated landing form.
* ``/expanded`` and ``/page`` enforce the standard read PDP decision on
  the underlying resource. Anonymous gets 401 when the resource is not
  publicly readable, mirroring the LDP GET behaviour.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import structlog
from fastapi import APIRouter, Depends, Query, Request, Response
from rdflib import Graph, URIRef
from rdflib.namespace import DCTERMS

from fdpneo_server.identity.deps import current_context
from fdpneo_server.metadata.shacl import UnknownShapeError
from fdpneo_server.metadata.signposting import Link, render_link_header
from fdpneo_server.policy.model import Action, Outcome
from fdpneo_server.shared.context import RequestContext
from fdpneo_server.shared.errors import (
    BadRequest,
    Forbidden,
    NotFound,
    PolicyViolation,
    Unauthenticated,
)
from fdpneo_server.shared.graphs import record_graph_uri
from fdpneo_server.shared.negotiation import SUPPORTED_TYPES, select_media_type, serialize

if TYPE_CHECKING:
    from fdpneo_server.metadata.lifecycle import StateGate
    from fdpneo_server.metadata.profiles.registry import ResourceDefinitionCache
    from fdpneo_server.metadata.repository import MetadataRepository
    from fdpneo_server.metadata.shacl import ShaclValidator
    from fdpneo_server.policy.pdp import PDP

log = structlog.get_logger(__name__)


# Hard cap so a single request cannot ask for unbounded children.
_PAGE_MAX_LIMIT = 1000
_PAGE_DEFAULT_LIMIT = 50

# Maximum depth of dct:isPartOf walk to prevent runaway recursion in the
# face of a malformed graph cycle. DCAT hierarchies are shallow in
# practice (Distribution → Dataset → Catalog → Repository = 4), so any
# legitimate /expanded request fits comfortably within this bound.
_EXPANDED_MAX_DEPTH = 16


def build_extensions_router(
    *,
    repo: MetadataRepository,
    pdp: PDP,
    cache_provider: Callable[[], ResourceDefinitionCache | None],
    base_url: str,
    state_gate: StateGate | None = None,
    validator: ShaclValidator | None = None,
) -> APIRouter:
    """Construct the read-extensions router.

    ``cache_provider`` is read on every request so a profile re-apply
    (which swaps ``app.state.resource_definitions``) is visible without
    rebuilding the router.

    ``base_url`` is used to mint absolute child / instance IRIs from
    the URL path segments. Trailing slash is stripped.
    """
    router = APIRouter(tags=["ldp-extensions"])
    base = base_url.rstrip("/")

    # --- /spec (anonymous; returns a type's SHACL NodeShape graph) -------

    async def _serve_shape(schema_iri: str, request: Request) -> Response:
        media = _negotiate(request)
        # Return the merged shape-graph *closure* (the type shape + every shape
        # it composes via sh:node) when a validator is wired, so the client's
        # form renderer sees inherited properties in one response (task 15.2).
        # Fall back to the single stored graph otherwise.
        if validator is not None:
            try:
                graph = await validator.shape_closure(schema_iri)
            except UnknownShapeError as err:
                raise NotFound(f"SHACL shape not found: {schema_iri}") from err
        else:
            graph = await repo.get_graph(schema_iri)
        if len(graph) == 0:
            raise NotFound(f"SHACL shape not found: {schema_iri}")
        body = serialize(graph, media)
        return Response(content=body, status_code=200, media_type=media)

    @router.get("/spec", name="ext_root_spec")
    async def root_spec(  # pyright: ignore[reportUnusedFunction]
        request: Request,
    ) -> Response:
        cache = cache_provider()
        rd = cache.root() if cache is not None else None
        if rd is None:
            raise NotFound("no root resource definition is registered")
        return await _serve_shape(rd.schema_iri, request)

    @router.get("/{url_prefix}/spec", name="ext_type_spec")
    async def type_spec(  # pyright: ignore[reportUnusedFunction]
        url_prefix: str,
        request: Request,
    ) -> Response:
        cache = cache_provider()
        rd = cache.by_prefix(url_prefix) if cache is not None else None
        if rd is None:
            raise NotFound(f"unknown resource type: {url_prefix}")
        return await _serve_shape(rd.schema_iri, request)

    @router.get("/{url_prefix}/{record_id}/spec", name="ext_instance_spec")
    async def instance_spec(  # pyright: ignore[reportUnusedFunction]
        url_prefix: str,
        record_id: str,  # noqa: ARG001  — present for routing; shape is per-type
        request: Request,
    ) -> Response:
        # Same response as type-level: shapes are per-type, not per-instance.
        # We keep the route to honour what openapi.py already documents.
        cache = cache_provider()
        rd = cache.by_prefix(url_prefix) if cache is not None else None
        if rd is None:
            raise NotFound(f"unknown resource type: {url_prefix}")
        return await _serve_shape(rd.schema_iri, request)

    # --- /expanded (PDP-gated; record + ancestors via dct:isPartOf) -----

    async def _read_authorised(ctx: RequestContext, resource_iri: str) -> None:
        decision = await pdp.authorize(ctx, Action.READ, resource_iri)
        if decision.outcome is not Outcome.PERMIT:
            if ctx.is_anonymous:
                raise Unauthenticated(f"authentication required for read on {resource_iri}")
            raise PolicyViolation(
                f"policy denies read on {resource_iri}",
                details={"action": "read", "resource": resource_iri},
            )
        # Publication-state gate (ADR-0010): a draft/archived record is not
        # visible to a non-owner even when ODRL permits read. Raises NotFound;
        # the ancestor/child walks below catch it so a hidden relative simply
        # drops out of the response instead of 404-ing the whole request.
        if state_gate is not None:
            await state_gate.ensure_visible(ctx, resource_iri)

    async def _serve_expanded(record_iri: str, ctx: RequestContext, request: Request) -> Response:
        await _read_authorised(ctx, record_iri)
        graph = await repo.get_graph(record_iri)
        if len(graph) == 0:
            raise NotFound(f"resource not found: {record_iri}")
        # Walk dct:isPartOf, authorising each ancestor before adding it.
        # An ancestor the caller cannot read silently drops out of the
        # response — anonymous probing of unreadable ancestors must not
        # leak via 200-with-content.
        visited: set[str] = {record_iri}
        result = Graph()
        for s, p, o in graph:
            result.add((s, p, o))
        frontier: list[str] = list(_extract_ancestors(graph, record_iri))
        depth = 0
        while frontier and depth < _EXPANDED_MAX_DEPTH:
            next_frontier: list[str] = []
            for parent_iri in frontier:
                if parent_iri in visited:
                    continue
                visited.add(parent_iri)
                try:
                    await _read_authorised(ctx, parent_iri)
                except (Unauthenticated, Forbidden, PolicyViolation, NotFound):
                    continue
                parent_graph = await repo.get_graph(parent_iri)
                for s, p, o in parent_graph:
                    result.add((s, p, o))
                next_frontier.extend(_extract_ancestors(parent_graph, parent_iri))
            frontier = next_frontier
            depth += 1
        media = _negotiate(request)
        body = serialize(result, media)
        return Response(content=body, status_code=200, media_type=media)

    @router.get("/expanded", name="ext_root_expanded")
    async def root_expanded(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        ctx: Annotated[RequestContext, Depends(current_context)],
    ) -> Response:
        cache = cache_provider()
        if cache is None or cache.root() is None:
            raise NotFound("no root resource is configured")
        return await _serve_expanded(base + "/", ctx, request)

    @router.get(
        "/{url_prefix}/{record_id}/expanded",
        name="ext_instance_expanded",
    )
    async def instance_expanded(  # pyright: ignore[reportUnusedFunction]
        url_prefix: str,
        record_id: str,
        request: Request,
        ctx: Annotated[RequestContext, Depends(current_context)],
    ) -> Response:
        cache = cache_provider()
        if cache is None or cache.by_prefix(url_prefix) is None:
            raise NotFound(f"unknown resource type: {url_prefix}")
        return await _serve_expanded(f"{base}/{url_prefix}/{record_id}", ctx, request)

    # --- /page (PDP-gated; paginated children listing) ------------------

    async def _serve_page(
        parent_iri: str,
        parent_rd_prefix: str,
        child_prefix: str,
        ctx: RequestContext,
        limit: int,
        offset: int,
        request: Request,
    ) -> Response:
        await _read_authorised(ctx, parent_iri)
        cache = cache_provider()
        if cache is None:
            raise NotFound("resource definitions are not loaded")
        parent_rd = cache.by_prefix(parent_rd_prefix)
        if parent_rd is None:
            raise NotFound(f"unknown resource type: {parent_rd_prefix}")
        link = next(
            (c for c in parent_rd.children if c.target_prefix == child_prefix),
            None,
        )
        if link is None:
            raise NotFound(f"{parent_rd.name} has no child link to '{child_prefix}'")
        # Normalize to the canonical record graph IRI: the repository root
        # arrives here as ``base + "/"`` but its triples are stored under the
        # slash-stripped subject (``record_graph_uri``), so querying the slashed
        # URIRef would match no forward links and report zero children.
        parent_subject = record_graph_uri(parent_iri)
        parent_graph = await repo.get_graph(str(parent_subject))
        children = sorted(
            str(o) for o in parent_graph.objects(parent_subject, URIRef(link.relation_uri))
        )
        total = len(children)
        page = children[offset : offset + limit]

        # Compose the response graph. For each child we serve back the
        # parent->child link triple plus the child's own type assertion
        # and dct:title (if present) so the client can render a list
        # without a follow-up call per item. Children the caller cannot
        # read are dropped from the page; the page may therefore be
        # shorter than ``limit``.
        result = Graph()
        parent_ref = parent_subject
        relation_ref = URIRef(link.relation_uri)
        target_type_ref = URIRef(link.target_schema_iri)
        for child_iri in page:
            try:
                await _read_authorised(ctx, child_iri)
            except (Unauthenticated, Forbidden, PolicyViolation, NotFound):
                continue
            child_ref = URIRef(child_iri)
            result.add((parent_ref, relation_ref, child_ref))
            child_graph = await repo.get_graph(child_iri)
            if len(child_graph) == 0:
                continue
            for title in child_graph.objects(child_ref, DCTERMS.title):
                result.add((child_ref, DCTERMS.title, title))
            # Echo target type so the client can render type chips
            # without consulting the registry.
            result.add(
                (
                    child_ref,
                    URIRef("http://www.w3.org/1999/02/22-rdf-syntax-ns#type"),
                    target_type_ref,
                )
            )

        media = _negotiate(request)
        body = serialize(result, media)
        # RFC 8288 navigation links (ADR-0022 §1) are the standard, client-generic
        # way to page. The legacy ``X-FDP-Page-*`` headers remain for one minor
        # release (deprecated; removal targeted v0.12.0) so existing callers keep
        # working while ``fdp-client`` migrates to ``rel="next"`` traversal.
        headers = {
            "X-FDP-Page-Total": str(total),
            "X-FDP-Page-Offset": str(offset),
            "X-FDP-Page-Limit": str(limit),
            "Link": render_link_header(
                _pagination_links(str(request.url), offset=offset, limit=limit, total=total)
            ),
        }
        return Response(content=body, status_code=200, media_type=media, headers=headers)

    @router.get(
        "/page/{child_prefix}",
        name="ext_root_page",
        description=(
            "Paginated listing of a container's children. Navigate with the "
            'RFC 8288 `Link` header (`rel="first"/"prev"/"next"/"last"`). '
            "The `X-FDP-Page-Total/Offset/Limit` headers are **deprecated** "
            "(removal targeted v0.12.0); prefer the `Link` relations."
        ),
    )
    async def root_page(  # pyright: ignore[reportUnusedFunction]
        child_prefix: str,
        request: Request,
        ctx: Annotated[RequestContext, Depends(current_context)],
        limit: Annotated[int, Query(ge=1, le=_PAGE_MAX_LIMIT)] = _PAGE_DEFAULT_LIMIT,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> Response:
        return await _serve_page(
            parent_iri=base + "/",
            parent_rd_prefix="",
            child_prefix=child_prefix,
            ctx=ctx,
            limit=limit,
            offset=offset,
            request=request,
        )

    @router.get(
        "/{url_prefix}/{record_id}/page/{child_prefix}",
        name="ext_instance_page",
    )
    async def instance_page(  # pyright: ignore[reportUnusedFunction]
        url_prefix: str,
        record_id: str,
        child_prefix: str,
        request: Request,
        ctx: Annotated[RequestContext, Depends(current_context)],
        limit: Annotated[int, Query(ge=1, le=_PAGE_MAX_LIMIT)] = _PAGE_DEFAULT_LIMIT,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> Response:
        return await _serve_page(
            parent_iri=f"{base}/{url_prefix}/{record_id}",
            parent_rd_prefix=url_prefix,
            child_prefix=child_prefix,
            ctx=ctx,
            limit=limit,
            offset=offset,
            request=request,
        )

    return router


# --- helpers ---------------------------------------------------------------


def _pagination_links(request_url: str, *, offset: int, limit: int, total: int) -> list[Link]:
    """RFC 8288 page-navigation links for a paged listing (ADR-0022 §1).

    ``first`` and ``last`` are always emitted; ``prev`` only when ``offset > 0``
    and ``next`` only when a further page exists (``offset + limit < total``).
    Each link URL is ``request_url`` with its ``offset`` query parameter rewritten
    to the target page; every other query parameter the caller sent (``limit``,
    negotiated ``format``, …) is preserved and re-encoded.
    """
    parts = urlsplit(request_url)
    preserved = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "offset"]

    def page_url(new_offset: int) -> str:
        query = urlencode([*preserved, ("offset", str(new_offset))])
        return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))

    # Offset of the final page: the largest multiple of ``limit`` still < total.
    last_offset = ((total - 1) // limit) * limit if total > 0 else 0

    links = [Link(page_url(0), "first")]
    if offset > 0:
        links.append(Link(page_url(max(offset - limit, 0)), "prev"))
    if offset + limit < total:
        links.append(Link(page_url(offset + limit), "next"))
    links.append(Link(page_url(last_offset), "last"))
    return links


def _negotiate(request: Request) -> str:
    """Pick the best supported RDF media type or raise ``BadRequest``."""
    media = select_media_type(request.headers.get("accept"))
    if media is None:
        raise BadRequest(
            "no acceptable RDF representation",
            details={"supported": list(SUPPORTED_TYPES)},
        )
    return media


def _extract_ancestors(graph: Graph, record_iri: str) -> list[str]:
    """Return the ``dct:isPartOf`` objects from ``record_iri`` in ``graph``."""
    subject = URIRef(record_iri)
    parents: list[str] = []
    for obj in graph.objects(subject, DCTERMS.isPartOf):
        if isinstance(obj, URIRef):
            parents.append(str(obj))
    return parents


__all__ = ["build_extensions_router"]
