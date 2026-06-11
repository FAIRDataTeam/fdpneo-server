"""LDP server router — GET/HEAD/POST/PUT/PATCH/DELETE/OPTIONS on records.

Responsibilities:

* Path → resource IRI mapping (the IRI is the absolute request URL).
* Content negotiation across Turtle, JSON-LD, RDF/XML, N-Triples.
* ETag computation and ``If-Match`` enforcement.
* LDP ``Link``, ``Allow``, ``Accept-Post`` and ``Accept-Patch`` headers.
* The per-method policy enforcement point — each handler calls into
  :class:`PDP.authorize` before any storage I/O.
* PATCH pipeline: parse → simulate locally → SHACL the post-update state →
  commit + meta refresh → publish ``RecordModified``. The simulation lives
  in :func:`fdp.metadata.patch.simulate_update`.

SHACL hooks are pluggable through :class:`ContainerRegistry`:

* :meth:`ContainerRegistry.member_shape` — shape for *new members* of a
  container (used by POST).
* :meth:`ContainerRegistry.shape_for` — shape a *resource itself* must
  satisfy (used by PATCH; PUT-to-replace is added in ticket 2.5).

Still deferred to later tickets:

* PROV ``Activity`` blank-node in meta-metadata — ticket 2.5.
* Meta-metadata schema validation — ticket 2.5.
* Parser-level SPARQL classification (read/update, referenced graphs,
  SERVICE detection) — ticket 3.1.

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
from fdp.metadata.events import RecordCreated, RecordDeleted, RecordModified
from fdp.metadata.graphs import record_graph_uri
from fdp.metadata.ldp.negotiation import (
    SPARQL_UPDATE,
    SUPPORTED_TYPES,
    TURTLE,
    normalize_content_type,
    parse as parse_rdf,
    select_media_type,
    serialize,
)
from fdp.metadata.patch import simulate_update
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
    from fdp.metadata.containment import ContainmentManager
    from fdp.metadata.lifecycle import StateGate
    from fdp.metadata.repository import MetadataRepository
    from fdp.metadata.shacl import ShaclValidator
    from fdp.policy.pdp import PDP
    from fdp.shared.events import Event, EventBus

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
    """Resolves a resource IRI to its LDP container kind and bound shapes.

    The default implementation treats nothing as a container and binds no
    shapes. Deployments that want POST + SHACL-on-write provide their own —
    typically sourced from container records' ``ldp:constrainedBy`` triples.

    ``member_shape`` returns the shape that *new members* of the container
    must satisfy; ``shape_for`` returns the shape that the *resource itself*
    must satisfy (in practice resolved by walking up to the resource's
    parent container's ``ldp:constrainedBy``).
    """

    def is_container(self, resource_iri: str) -> bool: ...

    def member_shape(self, container_iri: str) -> str | None: ...

    def shape_for(self, resource_iri: str) -> str | None: ...

    def containment_relation(self, parent_iri: str, child_iri: str) -> str | None: ...


class DefaultContainerRegistry:
    """Skeleton default — nothing is a container, no shape is bound."""

    def is_container(self, resource_iri: str) -> bool:  # noqa: ARG002
        return False

    def member_shape(self, container_iri: str) -> str | None:  # noqa: ARG002
        return None

    def shape_for(self, resource_iri: str) -> str | None:  # noqa: ARG002
        return None

    def containment_relation(self, parent_iri: str, child_iri: str) -> str | None:  # noqa: ARG002
        return None


def build_ldp_router(
    *,
    repo: MetadataRepository,
    pdp: PDP,
    validator: ShaclValidator | None = None,
    containers: ContainerRegistry | None = None,
    event_bus: EventBus | None = None,
    state_gate: StateGate | None = None,
    containment: ContainmentManager | None = None,
    prefix: str = "/ldp",
) -> APIRouter:
    """Build the LDP router wired with ``repo`` + ``pdp`` + optional helpers.

    ``event_bus`` is optional. When supplied, successful PATCH commits
    publish :class:`RecordModified`. PUT/POST/DELETE will gain events in
    ticket 2.5 along with the meta-metadata schema work.

    ``state_gate`` is optional (Phase 12 / ADR-0010). When supplied, ``GET`` /
    ``HEAD`` additionally enforce publication-state visibility *after* the ODRL
    read decision: a ``DRAFT``/``ARCHIVED`` record is 404 to anyone but its
    owner or an admin. Writes are unaffected — an owner edits their own drafts.
    """
    router = APIRouter(prefix=prefix, tags=["ldp"])
    registry: ContainerRegistry = containers or DefaultContainerRegistry()

    async def _publish(event: Event) -> None:
        if event_bus is None:
            return
        await event_bus.publish(event)

    async def _enforce(ctx: RequestContext, action: Action, resource_iri: str) -> None:
        # Authorize against the canonical resource IRI. Storage normalizes the
        # trailing slash via `record_graph_uri`, but the request URL for the
        # repository root arrives as ".../" — without this the offer resolver
        # looks for `dct:rights` at the slashed IRI, finds nothing, and
        # default-denies writes to the root even for stewards.
        resource_iri = str(record_graph_uri(resource_iri))
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

    async def _validate_against_resource_shape(graph: Graph, resource_iri: str) -> None:
        if validator is None:
            return
        shape_iri = registry.shape_for(resource_iri)
        if shape_iri is None:
            return
        report = await validator.validate_against(graph, shape_iri)
        report.raise_if_failed()

    async def _maintain_membership(
        *,
        child_iri: str,
        ctx: RequestContext,
        new_graph: Graph,
        old_graph: Graph,
        prior_exists: bool,
        is_create: bool,
    ) -> list[RecordModified]:
        """Write the parent's forward links, compensating the child write on failure.

        The child graph is already persisted; maintaining the parent's
        ``ldp:contains`` + typed relation is a second graph write the SPARQL 1.1
        Protocol can't bind into one transaction (ADR-0005). If it fails we undo
        the child write — restore its prior content, or delete it on a fresh
        create — so the two directions never disagree, and re-raise.
        """
        if containment is None:
            return []
        try:
            if is_create:
                return await containment.reconcile_create(
                    child_iri, new_graph, subject=ctx.subject, timestamp=ctx.request_timestamp
                )
            return await containment.reconcile_update(
                child_iri,
                old_graph,
                new_graph,
                subject=ctx.subject,
                timestamp=ctx.request_timestamp,
            )
        except Exception:
            if prior_exists:
                await repo.put_graph(child_iri, old_graph, subject=ctx.subject)
            else:
                await repo.delete_graph(child_iri)
            log.error("containment_reconcile_failed_compensated", child=child_iri)
            raise

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
        if state_gate is not None:
            await state_gate.ensure_visible(ctx, iri)
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
        if state_gate is not None:
            await state_gate.ensure_visible(ctx, iri)
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
        new_graph = _parse_body(request, await request.body(), base=iri)
        # PUT replaces the resource itself, so validate against the resource's
        # own type shape (resolved by its URL prefix) — not the container's
        # member shape, which only applies to POST and resolves to None for a
        # leaf member IRI (leaving the write unvalidated).
        await _validate_against_resource_shape(new_graph, iri)
        etag = await repo.put_graph(iri, new_graph, subject=ctx.subject)
        # Maintain the parent's forward containment links from the child's
        # dct:isPartOf (compensates the child write on failure).
        parent_events = await _maintain_membership(
            child_iri=iri,
            ctx=ctx,
            new_graph=new_graph,
            old_graph=existing,
            prior_exists=resource_exists,
            is_create=not resource_exists,
        )
        status_code = 200 if resource_exists else 201
        headers = _response_headers(etag, registry.is_container(iri))
        if not resource_exists:
            headers["Location"] = iri
            await _publish(
                RecordCreated(
                    record_iri=iri,
                    subject=ctx.subject,
                    etag=etag,
                    timestamp=ctx.request_timestamp,
                )
            )
        else:
            await _publish(
                RecordModified(
                    record_iri=iri,
                    subject=ctx.subject,
                    etag=etag,
                    timestamp=ctx.request_timestamp,
                )
            )
        for event in parent_events:
            await _publish(event)
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
        # Mint the member IRI first so a relative ``<>`` in the body resolves to
        # the new member's URI (LDP), not to the container or a file:// base.
        member_iri = _mint_member_iri(container_iri, request.headers.get("slug"))
        member_graph = _parse_body(request, await request.body(), base=member_iri)
        await _validate_member(member_graph, container_iri)
        etag = await repo.put_graph(member_iri, member_graph, subject=ctx.subject)
        parent_events = await _maintain_membership(
            child_iri=member_iri,
            ctx=ctx,
            new_graph=member_graph,
            old_graph=Graph(),
            prior_exists=False,
            is_create=True,
        )
        await _publish(
            RecordCreated(
                record_iri=member_iri,
                subject=ctx.subject,
                etag=etag,
                timestamp=ctx.request_timestamp,
            )
        )
        for event in parent_events:
            await _publish(event)
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
        new_graph = simulate_update(existing, body, iri)
        await _validate_against_resource_shape(new_graph, iri)
        etag = await repo.put_graph(iri, new_graph, subject=ctx.subject)
        await _publish(
            RecordModified(
                record_iri=iri,
                subject=ctx.subject,
                etag=etag,
                timestamp=ctx.request_timestamp,
            )
        )
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
        # Strip the parent's forward links to this child; restore the child on
        # failure so the two directions never disagree (ADR-0005 compensation).
        parent_events: list[RecordModified] = []
        if containment is not None:
            try:
                parent_events = await containment.reconcile_delete(
                    iri, existing, subject=ctx.subject, timestamp=ctx.request_timestamp
                )
            except Exception:
                await repo.put_graph(iri, existing, subject=ctx.subject)
                log.error("containment_reconcile_failed_compensated", child=iri)
                raise
        await _publish(
            RecordDeleted(
                record_iri=iri,
                subject=ctx.subject,
                timestamp=ctx.request_timestamp,
            )
        )
        for event in parent_events:
            await _publish(event)
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


def _parse_body(request: Request, body: bytes, *, base: str) -> Graph:
    if not body:
        raise BadRequest("request body is empty")
    ctype = normalize_content_type(request.headers.get("content-type")) or TURTLE
    if ctype not in SUPPORTED_TYPES:
        raise UnsupportedMediaType(
            f"Content-Type {ctype} is not a supported RDF serialization",
            details={"supported": list(SUPPORTED_TYPES)},
        )
    try:
        # ``base`` is the resource's own URI so a relative ``<>`` in the body
        # resolves to it (LDP), rather than to an rdflib-invented file:// base.
        return parse_rdf(body, ctype, base=base)
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
