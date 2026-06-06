"""License admin API (TASKS Phase 14.3, ADR-0012) — managed license documents.

The second of the two ODRL subsystems (the first is :mod:`fdp.metadata.policies`).
Licenses are **descriptive** rights statements referenced via ``dct:license`` —
the PEP never evaluates them. They are managed as ordinary metadata records at
the stable, dereferenceable IRI ``{base_url}/licenses/{id}`` so an FDP can serve
a curated, reusable set of licenses (CC0, CC BY 4.0, …) that records — and other
FDPs — reference.

Validation is intentionally lighter than the enforced ``/policies`` profile:
a license document must parse and describe a license at its stable IRI (a
recognised license type, or at least a ``dct:title``). A full license SHACL
shape can be layered in later behind this same seam.

Surfaces mirror :mod:`fdp.metadata.policies` / :mod:`fdp.metadata.schemas`:

* ``GET  /licenses``        — list managed licenses (public).
* ``GET  /licenses/{id}``   — the license document as Turtle (public).
* ``PUT  /licenses/{id}``   — create/replace (admin).
* ``DELETE /licenses/{id}`` — remove (admin; refused if referenced via ``dct:license``).
* ``POST /licenses/{id}/validate`` — dry-run the document body (authenticated).
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Final

import structlog
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel
from rdflib import Graph, URIRef

from fdp.identity.deps import current_context, require_auth
from fdp.metadata.events import RecordDeleted, RecordModified
from fdp.metadata.states import MetadataState
from fdp.shared.context import RequestContext
from fdp.shared.errors import BadRequest, Conflict, Forbidden, NotFound
from fdp.shared.graphs import license_graph_uri, meta_graph_uri
from fdp.shared.namespaces import DCT, FDP_METADATA_STATE, ODRL, OWL

if TYPE_CHECKING:
    from fdp.metadata.repository import MetadataRepository
    from fdp.shared.events import Event, EventBus
    from fdp.storage.triplestore import TripleStoreAdapter

log = structlog.get_logger(__name__)

_ADMIN_ROLE: Final = "admin"
_SLUG_RE: Final = re.compile(r"^[A-Za-z0-9._-]+$")
_TURTLE: Final = "text/turtle"
_SPARQL_JSON: Final = "application/sparql-results+json"

# Recognised license document types. A managed license either carries one of
# these rdf:types at its stable IRI or, failing that, at least a dct:title.
_LICENSE_TYPES: Final = (
    DCT.LicenseDocument,
    ODRL.Set,
    ODRL.Policy,
    URIRef("http://creativecommons.org/ns#License"),
)


# --- response models -------------------------------------------------------


class LicenseInfo(BaseModel):
    """Summary of a managed license document."""

    id: str
    iri: str
    title: str | None = None
    state: str | None = None
    version: int | None = None


class LicenseListView(BaseModel):
    licenses: list[LicenseInfo]


class ValidationResultView(BaseModel):
    conforms: bool
    violations: list[dict[str, str | None]]


# --- service ---------------------------------------------------------------


class LicenseService:
    """Runtime license-document storage. Descriptive only — no PDP coupling."""

    def __init__(
        self,
        *,
        repository: MetadataRepository,
        adapter: TripleStoreAdapter,
        base_url: str,
        event_bus: EventBus | None = None,
    ) -> None:
        self._repo = repository
        self._adapter = adapter
        self._base = base_url.rstrip("/")
        self._event_bus = event_bus

    def iri(self, license_id: str) -> str:
        return str(license_graph_uri(self._base, license_id))

    async def put(self, license_id: str, turtle: str, *, subject: str | None) -> LicenseInfo:
        _check_slug(license_id)
        iri = self.iri(license_id)
        graph = _parse_license_document(turtle, iri)
        etag = await self._repo.put_graph(iri, graph, subject=subject)
        await self._publish(
            RecordModified(record_iri=iri, subject=subject, etag=etag, timestamp=datetime.now(UTC))
        )
        log.info("license_published", iri=iri, subject=subject)
        return _info(license_id, iri, graph=graph, version=await self._version(iri))

    async def get_turtle(self, license_id: str) -> str:
        _check_slug(license_id)
        iri = self.iri(license_id)
        graph = await self._repo.get_graph(iri)
        if len(graph) == 0:
            raise NotFound(f"no license: {license_id}")
        return graph.serialize(format="turtle")

    async def delete(self, license_id: str) -> None:
        _check_slug(license_id)
        iri = self.iri(license_id)
        if await self._is_referenced(iri):
            raise Conflict(
                "license is referenced by a record via dct:license; repoint those records first",
                details={"license": iri},
            )
        if not await self._exists(iri):
            raise NotFound(f"no license: {license_id}")
        await self._repo.delete_graph(iri)
        await self._publish(
            RecordDeleted(record_iri=iri, subject=None, timestamp=datetime.now(UTC))
        )
        log.info("license_deleted", iri=iri)

    async def validate_body(self, license_id: str, turtle: str) -> ValidationResultView:
        _check_slug(license_id)
        iri = self.iri(license_id)
        try:
            _parse_license_document(turtle, iri)
        except BadRequest as err:
            return ValidationResultView(conforms=False, violations=[{"message": err.message}])
        return ValidationResultView(conforms=True, violations=[])

    async def list_licenses(self, *, published_only: bool = False) -> list[LicenseInfo]:
        """List managed licenses with their publication state.

        ``published_only`` drops drafts/archived — the catalog the `dct:license`
        picker consumes (ADR-0012 §4). Admins pass ``False`` to manage drafts.
        """
        query = (
            "SELECT ?g (SAMPLE(?t) AS ?title) (SAMPLE(?s) AS ?state) WHERE { GRAPH ?g {"
            f" ?g <{DCT.title}> ?t }}"
            " OPTIONAL {"
            f" GRAPH ?m {{ ?g <{FDP_METADATA_STATE}> ?s }}"
            ' FILTER(?m = IRI(CONCAT(STR(?g), "/meta"))) }'
            f' FILTER(STRSTARTS(STR(?g), "{self._base}/licenses/")) }} GROUP BY ?g'
        )
        rows = await self._select(query)
        items: list[LicenseInfo] = []
        for row in rows:
            iri = row.get("g", {}).get("value")
            if not iri:
                continue
            state = row.get("state", {}).get("value")
            if published_only and state != MetadataState.PUBLISHED.value:
                continue
            items.append(
                LicenseInfo(
                    id=iri.rsplit("/", 1)[-1],
                    iri=iri,
                    title=row.get("title", {}).get("value"),
                    state=state,
                )
            )
        items.sort(key=lambda lic: lic.id)
        return items

    # --- internals ---------------------------------------------------------

    async def _version(self, iri: str) -> int | None:
        meta = meta_graph_uri(iri)
        rows = await self._select(
            f"SELECT ?v WHERE {{ GRAPH <{meta}> {{ <{iri}> <{OWL.versionInfo}> ?v }} }}"
        )
        for row in rows:
            raw = row.get("v", {}).get("value")
            if raw is not None:
                try:
                    return int(raw)
                except ValueError:
                    return None
        return None

    async def _exists(self, iri: str) -> bool:
        return await self._adapter.ask(f"ASK {{ GRAPH <{iri}> {{ <{iri}> ?p ?o }} }}")

    async def _is_referenced(self, iri: str) -> bool:
        return await self._adapter.ask(f"ASK {{ GRAPH ?g {{ ?r <{DCT.license}> <{iri}> }} }}")

    async def _select(self, query: str) -> list[dict[str, dict[str, str]]]:
        body = await self._adapter.query(query, accept=_SPARQL_JSON)
        payload = json.loads(body)
        return payload.get("results", {}).get("bindings", [])

    async def _publish(self, event: Event) -> None:
        if self._event_bus is None:
            return
        await self._event_bus.publish(event)


def _check_slug(license_id: str) -> None:
    if not _SLUG_RE.match(license_id):
        raise BadRequest(
            "license id must be a slug of letters, digits, '.', '_' or '-'",
            details={"id": license_id},
        )


def _parse_license_document(turtle: str, iri: str) -> Graph:
    """Parse + structurally validate a license document at the stable ``iri``.

    Turtle is parsed with ``iri`` as the base so a relative ``<>`` resolves to
    the stable IRI. The document must describe a license at ``<iri>`` — either a
    recognised license ``rdf:type`` or, at minimum, a ``dct:title``.
    """
    graph = Graph()
    try:
        graph.parse(data=turtle, format="turtle", publicID=iri)
    except Exception as err:
        raise BadRequest(f"body is not valid Turtle: {err}") from err
    subject = URIRef(iri)
    has_type = any((subject, None, t) in graph for t in _LICENSE_TYPES)
    has_title = next(iter(graph.objects(subject, DCT.title)), None) is not None
    if not (has_type or has_title):
        raise BadRequest(
            "not a license document: expected a recognised license type "
            "(dct:LicenseDocument / odrl:Set / odrl:Policy / cc:License) or a "
            f"dct:title on <{iri}>",
            details={"iri": iri},
        )
    return graph


def _info(license_id: str, iri: str, *, graph: Graph, version: int | None) -> LicenseInfo:
    title = next(iter(graph.objects(URIRef(iri), DCT.title)), None)
    return LicenseInfo(
        id=license_id,
        iri=iri,
        title=str(title) if title is not None else None,
        version=version,
    )


# --- router ----------------------------------------------------------------


def build_license_router(*, service: LicenseService, prefix: str = "/licenses") -> APIRouter:
    """Build the license-admin router. Reads are public; writes require admin."""
    router = APIRouter(prefix=prefix, tags=["licenses"])

    def _require_admin(ctx: RequestContext) -> None:
        if _ADMIN_ROLE not in ctx.roles:
            raise Forbidden(
                "admin role required to manage licenses",
                details={"required_role": _ADMIN_ROLE},
            )

    @router.get("", response_model=LicenseListView, name="license_list")
    async def list_licenses(  # pyright: ignore[reportUnusedFunction]
        ctx: Annotated[RequestContext, Depends(current_context)],
    ) -> LicenseListView:
        # Anonymous/non-admin callers see only PUBLISHED licenses; admins see all.
        published_only = _ADMIN_ROLE not in ctx.roles
        return LicenseListView(licenses=await service.list_licenses(published_only=published_only))

    @router.get("/{license_id}", name="license_get")
    async def get_license(license_id: str) -> Response:  # pyright: ignore[reportUnusedFunction]
        turtle = await service.get_turtle(license_id)
        return Response(content=turtle, media_type=_TURTLE)

    @router.put("/{license_id}", response_model=LicenseInfo, name="license_put")
    async def put_license(  # pyright: ignore[reportUnusedFunction]
        license_id: str,
        request: Request,
        ctx: Annotated[RequestContext, Depends(require_auth)],
    ) -> LicenseInfo:
        """Publish (create or replace) a license document (admin only)."""
        _require_admin(ctx)
        body = (await request.body()).decode("utf-8")
        if not body.strip():
            raise BadRequest("PUT /licenses requires a non-empty Turtle body")
        return await service.put(license_id, body, subject=ctx.subject)

    @router.delete("/{license_id}", status_code=204, name="license_delete")
    async def delete_license(  # pyright: ignore[reportUnusedFunction]
        license_id: str,
        ctx: Annotated[RequestContext, Depends(require_auth)],
    ) -> None:
        _require_admin(ctx)
        await service.delete(license_id)

    @router.post(
        "/{license_id}/validate", response_model=ValidationResultView, name="license_validate"
    )
    async def validate_license(  # pyright: ignore[reportUnusedFunction]
        license_id: str,
        request: Request,
        ctx: Annotated[RequestContext, Depends(require_auth)],
    ) -> ValidationResultView:
        del ctx  # any authenticated user may preview; no mutation occurs
        body = (await request.body()).decode("utf-8")
        if not body.strip():
            raise BadRequest("validate requires a non-empty Turtle body")
        return await service.validate_body(license_id, body)

    return router


__all__ = [
    "LicenseInfo",
    "LicenseListView",
    "LicenseService",
    "ValidationResultView",
    "build_license_router",
]
