"""LDP server router — GET/HEAD/POST/PUT/PATCH/DELETE/OPTIONS on records.

This is the skeleton for ticket 2.3. It owns:

* Path → resource IRI mapping (the IRI is the absolute request URL).
* Content negotiation across Turtle, JSON-LD, RDF/XML, N-Triples.
* ETag computation and ``If-Match`` enforcement.
* LDP ``Link``, ``Allow``, ``Accept-Post`` and ``Accept-Patch`` headers.
* The per-method policy enforcement point — each handler calls into
  :class:`PDP.authorize` before any storage I/O.

It deliberately delegates the deeper semantics to later tickets:

* SHACL validation is wired through an optional :class:`ContainerRegistry`
  whose :meth:`member_shape` maps a container IRI to the shape IRI its
  members must satisfy. If no registry / shape / validator is supplied the
  router happily skips validation (ticket 2.4 + 2.5 tighten this).
* PATCH simulates against the live graph by calling
  :meth:`MetadataRepository.patch_graph` directly; the full
  *parse → simulate → SHACL → commit* pipeline is ticket 2.4.
* Meta-metadata schema validation is ticket 2.5.

The router is built by :func:`build_ldp_router`. The factory takes its
dependencies explicitly so tests can wire fakes without going through
``app.state``.
"""

from __future__ import annotations

import re
import uuid
from typing import TYPE_CHECKING, Annotated, Protocol

import structlog
from fastapi import APIRouter, Depends, Request, Response
from rdflib import Graph

from fdp.identity.deps import current_context
from fdp.metadata.etag import compute_etag
from fdp.metadata.ldp.negotiation import (
    SPARQL_UPDATE,
    SUPPORTED_TYPES,
    TURTLE,
    normalize_content_type,
    parse as parse_rdf,
    select_media_type,
    serialize,
)
from fdp.policy.model import Action, Outcome
from fdp.shared.context import RequestContext
from fdp.shared.errors import (
    BadRequest,
    MethodNotAllowed,
    NotAcceptable,
    NotFound,
    PolicyViolation,
    PreconditionFailed,
    PreconditionRequired,
    SchemaViolation,
    Unauthenticated,
    UnsupportedMediaType,
)

if TYPE_CHECKING:
    from fdp.metadata.repository import MetadataRepository
    from fdp.metadata.shacl import ShaclValidator
    from fdp.policy.pdp import PDP

log = structlog.get_logger(__name__)


LDP_RESOURCE = "http://www.w3.org/ns/ldp#Resource"
LDP_RDF_SOURCE = "http://www.w3.org/ns/ldp#RDFSource"
LDP_DIRECT_CONTAINER = "http://www.w3.org/ns/ldp#DirectContainer"

_ALLOWED_METHODS_RESOURCE = "GET, HEAD, OPTIONS, PUT, PATCH, DELETE"
_ALLOWED_METHODS_CONTAINER = "GET, HEAD, OPTIONS, POST, PUT, PATCH, DELETE"
_ACCEPT_POST = ", ".join(SUPPORTED_TYPES)
_ACCEPT_PATCH = SPARQL_UPDATE
_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


class ContainerRegistry(Protocol):
    """Resolves a resource IRI to its LDP container kind and member shape.

    The default implementation treats nothing as a container. Deployments
    that want POST and SHACL-on-write provide their own — typically
    sourced from container records' ``ldp:constrainedBy`` triples.
    """

    def is_container(self, resource_iri: str) -> bool: ...

    def member_shape(self, container_iri: str) -> str | None: ...


class DefaultContainerRegistry:
    """Skeleton default — nothing is a container, no shape is bound."""

    def is_container(self, resource_iri: str) -> bool:  # noqa: ARG002
        return False

    def member_shape(self, container_iri: str) -> str | None:  # noqa: ARG002
        return None


def build_ldp_router(
    *,
    repo: MetadataRepository,
    pdp: PDP,
    validator: ShaclValidator | None = None,
    containers: ContainerRegistry | None = None,
    prefix: str = "/ldp",
) -> APIRouter:
    """Build the LDP router wired with ``repo`` + ``pdp`` + optional helpers."""
    router = APIRouter(prefix=prefix, tags=["ldp"])
    registry: ContainerRegistry = containers or DefaultContainerRegistry()

    async def _enforce(ctx: RequestContext, action: Action, resource_iri: str) -> None:
        decision = await pdp.authorize(ctx, action, resource_iri)
        if decision.outcome is Outcome.PERMIT:
            return
        log.info(
            "ldp_authz_denied",
            action=action.value,
            resource=resource_iri,
            anonymous=ctx.is_anonymous,
        )
        if ctx.is_anonymous:
            raise Unauthenticated(f"authentication required for {action.value} on {resource_iri}")
        raise PolicyViolation(
            f"policy denies {action.value} on {resource_iri}",
            details={"action": action.value, "resource": resource_iri},
        )

    async def _validate_member(graph: Graph, container_iri: str) -> None:
        if validator is None:
            return
        shape_iri = registry.member_shape(container_iri)
        if shape_iri is None:
            return
        report = await validator.validate_against(graph, shape_iri)
        report.raise_if_failed()

    @router.get("/{path:path}", name="ldp_get")
    async def http_get(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        path: str,  # noqa: ARG001
        ctx: Annotated[RequestContext, Depends(current_context)],
    ) -> Response:
        iri = _resource_iri(request)
        await _enforce(ctx, Action.READ, iri)
        media = _negotiate(request)
        graph = await repo.get_graph(iri)
        if len(graph) == 0:
            raise NotFound(f"resource not found: {iri}")
        body = serialize(graph, media)
        etag = compute_etag(graph)
        headers = _response_headers(etag, registry.is_container(iri))
        headers["Content-Type"] = media
        return Response(content=body, status_code=200, headers=headers)

    @router.head("/{path:path}", name="ldp_head")
    async def http_head(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        path: str,  # noqa: ARG001
        ctx: Annotated[RequestContext, Depends(current_context)],
    ) -> Response:
        iri = _resource_iri(request)
        await _enforce(ctx, Action.READ, iri)
        media = _negotiate(request)
        graph = await repo.get_graph(iri)
        if len(graph) == 0:
            raise NotFound(f"resource not found: {iri}")
        etag = compute_etag(graph)
        headers = _response_headers(etag, registry.is_container(iri))
        headers["Content-Type"] = media
        return Response(status_code=200, headers=headers)

    @router.put("/{path:path}", name="ldp_put")
    async def http_put(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        path: str,  # noqa: ARG001
        ctx: Annotated[RequestContext, Depends(current_context)],
    ) -> Response:
        iri = _resource_iri(request)
        await _enforce(ctx, Action.MODIFY, iri)
        existing = await repo.get_graph(iri)
        resource_exists = len(existing) > 0
        _enforce_if_match(request, existing, required=resource_exists)
        new_graph = _parse_body(request, await request.body())
        await _validate_member(new_graph, iri)
        etag = await repo.put_graph(iri, new_graph, creator=ctx.subject)
        status_code = 200 if resource_exists else 201
        headers = _response_headers(etag, registry.is_container(iri))
        if not resource_exists:
            headers["Location"] = iri
        return Response(status_code=status_code, headers=headers)

    @router.post("/{path:path}", name="ldp_post")
    async def http_post(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        path: str,  # noqa: ARG001
        ctx: Annotated[RequestContext, Depends(current_context)],
    ) -> Response:
        container_iri = _resource_iri(request)
        if not registry.is_container(container_iri):
            raise MethodNotAllowed(f"{container_iri} is not a container")
        await _enforce(ctx, Action.MODIFY, container_iri)
        member_graph = _parse_body(request, await request.body())
        await _validate_member(member_graph, container_iri)
        member_iri = _mint_member_iri(container_iri, request.headers.get("slug"))
        etag = await repo.put_graph(member_iri, member_graph, creator=ctx.subject)
        headers = _response_headers(etag, is_container=False)
        headers["Location"] = member_iri
        return Response(status_code=201, headers=headers)

    @router.patch("/{path:path}", name="ldp_patch")
    async def http_patch(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        path: str,  # noqa: ARG001
        ctx: Annotated[RequestContext, Depends(current_context)],
    ) -> Response:
        iri = _resource_iri(request)
        ctype = normalize_content_type(request.headers.get("content-type"))
        if ctype != SPARQL_UPDATE:
            raise UnsupportedMediaType(
                f"PATCH requires Content-Type: {SPARQL_UPDATE}",
            )
        await _enforce(ctx, Action.MODIFY, iri)
        existing = await repo.get_graph(iri)
        if len(existing) == 0:
            raise NotFound(f"resource not found: {iri}")
        _enforce_if_match(request, existing, required=True)
        body = (await request.body()).decode("utf-8")
        if not body.strip():
            raise BadRequest("PATCH body must be a non-empty SPARQL Update")
        etag = await repo.patch_graph(iri, body, creator=ctx.subject)
        headers = _response_headers(etag, registry.is_container(iri))
        return Response(status_code=204, headers=headers)

    @router.delete("/{path:path}", name="ldp_delete")
    async def http_delete(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        path: str,  # noqa: ARG001
        ctx: Annotated[RequestContext, Depends(current_context)],
    ) -> Response:
        iri = _resource_iri(request)
        await _enforce(ctx, Action.DELETE, iri)
        existing = await repo.get_graph(iri)
        if len(existing) == 0:
            raise NotFound(f"resource not found: {iri}")
        _enforce_if_match(request, existing, required=True)
        await repo.delete_graph(iri)
        return Response(status_code=204)

    @router.options("/{path:path}", name="ldp_options")
    async def http_options(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        path: str,  # noqa: ARG001
    ) -> Response:
        iri = _resource_iri(request)
        is_container = registry.is_container(iri)
        headers = _response_headers(etag=None, is_container=is_container)
        return Response(status_code=204, headers=headers)

    return router


def _resource_iri(request: Request) -> str:
    """Resource IRI = the absolute request URL without query string."""
    url = str(request.url)
    return url.split("?", 1)[0]


def _negotiate(request: Request) -> str:
    media = select_media_type(request.headers.get("accept"))
    if media is None:
        raise NotAcceptable(
            "no supported media type in Accept",
            details={"supported": list(SUPPORTED_TYPES)},
        )
    return media


def _parse_body(request: Request, body: bytes) -> Graph:
    if not body:
        raise BadRequest("request body is empty")
    ctype = normalize_content_type(request.headers.get("content-type")) or TURTLE
    if ctype not in SUPPORTED_TYPES:
        raise UnsupportedMediaType(
            f"Content-Type {ctype} is not a supported RDF serialization",
            details={"supported": list(SUPPORTED_TYPES)},
        )
    try:
        return parse_rdf(body, ctype)
    except Exception as err:  # rdflib raises a variety of parse errors
        raise BadRequest(f"could not parse {ctype} body: {err}") from err


def _enforce_if_match(request: Request, current: Graph, *, required: bool) -> None:
    header = request.headers.get("if-match")
    if header is None:
        if required:
            raise PreconditionRequired("If-Match header is required for this method")
        return
    if header.strip() == "*":
        if len(current) == 0:
            raise PreconditionFailed("If-Match: * but resource does not exist")
        return
    current_etag = compute_etag(current)
    if _strip_etag(header) != current_etag:
        raise PreconditionFailed(
            "If-Match does not match the current resource ETag",
            details={"current_etag": current_etag},
        )


def _strip_etag(value: str) -> str:
    cleaned = value.strip()
    if cleaned.startswith("W/"):
        cleaned = cleaned[2:].strip()
    return cleaned.strip('"')


def _mint_member_iri(container_iri: str, slug: str | None) -> str:
    base = container_iri.rstrip("/")
    if slug:
        cleaned = _SLUG_RE.sub("-", slug).strip("-")
        if cleaned:
            return f"{base}/{cleaned}"
    return f"{base}/{uuid.uuid4()}"


def _response_headers(etag: str | None, is_container: bool) -> dict[str, str]:
    link_parts = [f'<{LDP_RESOURCE}>; rel="type"', f'<{LDP_RDF_SOURCE}>; rel="type"']
    allow = _ALLOWED_METHODS_RESOURCE
    if is_container:
        link_parts.append(f'<{LDP_DIRECT_CONTAINER}>; rel="type"')
        allow = _ALLOWED_METHODS_CONTAINER
    headers: dict[str, str] = {
        "Link": ", ".join(link_parts),
        "Allow": allow,
        "Accept-Post": _ACCEPT_POST,
        "Accept-Patch": _ACCEPT_PATCH,
    }
    if etag is not None:
        headers["ETag"] = f'"{etag}"'
    return headers


# Re-export for convenience: SchemaViolation is raised by validator and
# the LDP layer surfaces it; making the import explicit here keeps the
# module's public surface obvious to readers.
_ = SchemaViolation


__all__ = [
    "ContainerRegistry",
    "DefaultContainerRegistry",
    "build_ldp_router",
]
