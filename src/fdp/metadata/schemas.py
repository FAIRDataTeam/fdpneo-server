"""Schema admin API (TASKS Phase 10.1) — runtime SHACL shape management.

Lets an admin publish SHACL shapes at runtime instead of only via the
deployment profile bundle, closing the gap that registering a resource
definition (ADR-0009) requires a *published* shape but there was no
first-class way to publish one.

A shape is stored as an ordinary metadata record at
``{base_url}/schemas/{id}`` through the LDP/metadata layer, so it inherits
meta-metadata — including ``owl:versionInfo``, which is bumped on every write,
giving versioning for free without minting a new IRI per version (a stable IRI
keeps resource-definition ``schema`` references valid across edits). On every
write the validator's parsed-shape cache is invalidated and re-warmed.

Surfaces (mirrors the resource-definition admin split):

* ``GET  /schemas``            — list published shapes (public).
* ``GET  /schemas/{id}``       — the shape as Turtle (public).
* ``PUT  /schemas/{id}``       — create/replace a shape (admin; Turtle body).
* ``DELETE /schemas/{id}``     — remove a shape (admin; refused if a resource
                                 definition still references it).
* ``POST /schemas/{id}/validate`` — dry-run a sample record against the shape
                                 and return the SHACL report (authenticated).

Authorization mirrors runtime-settings / resource-definitions: shapes are
deployment configuration, so writes require the ``admin`` role; reads are
public so the client can populate schema pickers and the SHACL editor.

Draft/release lifecycle and version *history* browsing are deferred — they
overlap the metadata publication-state work (Phase 12); a published shape here
is immediately usable.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Annotated, Final, cast

import pyshacl  # type: ignore[import-untyped]
import structlog
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel
from rdflib import Graph
from rdflib.namespace import RDF

from fdp.identity.deps import require_auth
from fdp.shared.context import RequestContext
from fdp.shared.errors import BadRequest, Conflict, Forbidden, NotFound
from fdp.shared.graphs import (
    meta_graph_uri,
    record_graph_uri,
    schema_graph_uri,
    schema_namespace,
)
from fdp.shared.namespaces import FDP_RESOURCE_DEFINITION, LDP, OWL, SH

if TYPE_CHECKING:
    from fdp.metadata.repository import MetadataRepository
    from fdp.metadata.shacl import ShaclValidator
    from fdp.storage.triplestore import TripleStoreAdapter

log = structlog.get_logger(__name__)

_ADMIN_ROLE: Final = "admin"
_SLUG_RE: Final = re.compile(r"^[A-Za-z0-9._-]+$")
_TURTLE: Final = "text/turtle"
_SPARQL_JSON: Final = "application/sparql-results+json"


# --- response models -------------------------------------------------------


class SchemaInfo(BaseModel):
    """Summary of a published shape."""

    id: str
    iri: str
    target_class: str | None = None
    version: int | None = None


class SchemaListView(BaseModel):
    schemas: list[SchemaInfo]


class ValidationResultView(BaseModel):
    """The outcome of a dry-run validation against a shape."""

    conforms: bool
    violations: list[dict[str, str | None]]


# --- service ---------------------------------------------------------------


class SchemaService:
    """Runtime SHACL-shape storage + validator-cache coherence.

    Stateless aside from its collaborators; safe to share across requests.
    """

    def __init__(
        self,
        *,
        repository: MetadataRepository,
        adapter: TripleStoreAdapter,
        validator: ShaclValidator,
        base_url: str,
    ) -> None:
        self._repo = repository
        self._adapter = adapter
        self._validator = validator
        self._base = base_url.rstrip("/")

    def iri(self, schema_id: str) -> str:
        return str(schema_graph_uri(self._base, schema_id))

    async def put(self, schema_id: str, turtle: str, *, subject: str | None) -> SchemaInfo:
        """Create or replace the shape ``schema_id`` and refresh the validator."""
        _check_slug(schema_id)
        iri = self.iri(schema_id)
        graph = _parse_shape(turtle)
        await self._repo.put_graph(iri, graph, subject=subject)
        # Drop any stale compiled shape, then re-warm so the first validation
        # (and the RD-create existence check) doesn't pay a refetch.
        self._validator.invalidate(iri)
        try:
            await self._validator.bootstrap([iri])
        except Exception as err:  # pragma: no cover - defensive
            log.warning("schema_warm_failed", iri=iri, error=repr(err))
        log.info("schema_published", iri=iri, subject=subject)
        return await self._info(schema_id, iri, graph=graph)

    async def get_turtle(self, schema_id: str) -> str:
        _check_slug(schema_id)
        iri = self.iri(schema_id)
        graph = await self._repo.get_graph(iri)
        if len(graph) == 0:
            raise NotFound(f"no schema: {schema_id}")
        return graph.serialize(format="turtle")

    async def delete(self, schema_id: str) -> None:
        _check_slug(schema_id)
        iri = self.iri(schema_id)
        if await self._is_referenced(iri):
            raise Conflict(
                "schema is referenced by a resource definition; delete or repoint that type first",
                details={"schema": iri},
            )
        if not await self._exists(iri):
            raise NotFound(f"no schema: {schema_id}")
        await self._repo.delete_graph(iri)
        self._validator.invalidate(iri)
        log.info("schema_deleted", iri=iri)

    async def validate_sample(self, schema_id: str, sample_turtle: str) -> ValidationResultView:
        _check_slug(schema_id)
        iri = self.iri(schema_id)
        if not await self._exists(iri):
            raise NotFound(f"no schema: {schema_id}")
        data = Graph()
        try:
            data.parse(data=sample_turtle, format="turtle")
        except Exception as err:
            raise BadRequest(f"sample is not valid Turtle: {err}") from err
        report = await self._validator.validate_against(data, iri)
        return ValidationResultView(
            conforms=report.conforms,
            violations=[
                {
                    "focus_node": v.focus_node,
                    "result_path": v.result_path,
                    "message": v.message,
                    "value": v.value,
                }
                for v in report.violations
            ],
        )

    async def list_schemas(self) -> list[SchemaInfo]:
        query = (
            "SELECT ?g (SAMPLE(?tc) AS ?target) WHERE { GRAPH ?g {"
            f" ?s a <{SH.NodeShape}> . OPTIONAL {{ ?s <{SH.targetClass}> ?tc }} }}"
            f' FILTER(STRSTARTS(STR(?g), "{schema_namespace(self._base)}/")) }} GROUP BY ?g'
        )
        rows = await self._select(query)
        items: list[SchemaInfo] = []
        for row in rows:
            iri = row.get("g", {}).get("value")
            if not iri:
                continue
            items.append(
                SchemaInfo(
                    id=iri.rsplit("/", 1)[-1],
                    iri=iri,
                    target_class=row.get("target", {}).get("value"),
                )
            )
        items.sort(key=lambda s: s.id)
        return items

    # --- internals ---------------------------------------------------------

    async def _info(self, schema_id: str, iri: str, *, graph: Graph) -> SchemaInfo:
        target = next(iter(graph.objects(None, SH.targetClass)), None)
        return SchemaInfo(
            id=schema_id,
            iri=iri,
            target_class=str(target) if target is not None else None,
            version=await self._version(iri),
        )

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
        return await self._adapter.ask(
            f"ASK {{ GRAPH <{record_graph_uri(iri)}> {{ ?s a <{SH.NodeShape}> }} }}"
        )

    async def _is_referenced(self, iri: str) -> bool:
        return await self._adapter.ask(
            f"ASK {{ GRAPH ?g {{ ?rd a <{FDP_RESOURCE_DEFINITION}> ;"
            f" <{LDP.constrainedBy}> <{iri}> }} }}"
        )

    async def _select(self, query: str) -> list[dict[str, dict[str, str]]]:
        body = await self._adapter.query(query, accept=_SPARQL_JSON)
        payload = json.loads(body)
        return payload.get("results", {}).get("bindings", [])


def _check_slug(schema_id: str) -> None:
    if not _SLUG_RE.match(schema_id):
        raise BadRequest(
            "schema id must be a slug of letters, digits, '.', '_' or '-'",
            details={"id": schema_id},
        )


def _parse_shape(turtle: str) -> Graph:
    """Parse + sanity-check that ``turtle`` is a usable SHACL shape graph."""
    graph = Graph()
    try:
        graph.parse(data=turtle, format="turtle")
    except Exception as err:
        raise BadRequest(f"body is not valid Turtle: {err}") from err
    has_node_shape = (None, RDF.type, SH.NodeShape) in graph
    has_target = next(iter(graph.triples((None, SH.targetClass, None))), None) is not None
    if not (has_node_shape or has_target):
        raise BadRequest(
            "not a SHACL shape: expected a sh:NodeShape or a sh:targetClass",
        )
    # Surface a malformed shape now rather than at first record write: pySHACL
    # raises if the shapes graph itself is broken. Validate against a *copy*:
    # pySHACL normalizes the shapes graph in place (it injects RDFS/OWL
    # axiomatic triples) even with ``inplace=False``, which would otherwise
    # pollute the stored shape — leaking noise into ``GET /schemas/{id}`` and
    # defeating the remote-sync change detection that compares stored vs fetched.
    probe = Graph()
    for triple in graph:
        probe.add(triple)
    try:
        cast(
            tuple[bool, Graph, str],
            pyshacl.validate(  # pyright: ignore[reportUnknownMemberType]
                data_graph=Graph(),
                shacl_graph=probe,
                inference="none",
                advanced=False,
                meta_shacl=False,
                inplace=False,
            ),
        )
    except Exception as err:
        raise BadRequest(f"shape did not load as SHACL: {err}") from err
    return graph


# --- router ----------------------------------------------------------------


def build_schema_router(*, service: SchemaService, prefix: str = "/schemas") -> APIRouter:
    """Build the schema-admin router. Reads are public; writes require admin."""
    router = APIRouter(prefix=prefix, tags=["schemas"])

    def _require_admin(ctx: RequestContext) -> None:
        if _ADMIN_ROLE not in ctx.roles:
            raise Forbidden(
                "admin role required to manage schemas",
                details={"required_role": _ADMIN_ROLE},
            )

    @router.get("", response_model=SchemaListView, name="schema_list")
    async def list_schemas() -> SchemaListView:  # pyright: ignore[reportUnusedFunction]
        return SchemaListView(schemas=await service.list_schemas())

    @router.get("/{schema_id}", name="schema_get")
    async def get_schema(schema_id: str) -> Response:  # pyright: ignore[reportUnusedFunction]
        turtle = await service.get_turtle(schema_id)
        return Response(content=turtle, media_type=_TURTLE)

    @router.put("/{schema_id}", response_model=SchemaInfo, name="schema_put")
    async def put_schema(  # pyright: ignore[reportUnusedFunction]
        schema_id: str,
        request: Request,
        ctx: Annotated[RequestContext, Depends(require_auth)],
    ) -> SchemaInfo:
        """Publish (create or replace) a SHACL shape (admin only).

        Body is Turtle. Each write bumps the shape's ``owl:versionInfo`` and
        re-warms the SHACL validator. The stable IRI keeps resource-definition
        ``schema`` references valid across edits.
        """
        _require_admin(ctx)
        body = (await request.body()).decode("utf-8")
        if not body.strip():
            raise BadRequest("PUT /schemas requires a non-empty Turtle body")
        return await service.put(schema_id, body, subject=ctx.subject)

    @router.delete("/{schema_id}", status_code=204, name="schema_delete")
    async def delete_schema(  # pyright: ignore[reportUnusedFunction]
        schema_id: str,
        ctx: Annotated[RequestContext, Depends(require_auth)],
    ) -> None:
        _require_admin(ctx)
        await service.delete(schema_id)

    @router.post(
        "/{schema_id}/validate", response_model=ValidationResultView, name="schema_validate"
    )
    async def validate_schema(  # pyright: ignore[reportUnusedFunction]
        schema_id: str,
        request: Request,
        ctx: Annotated[RequestContext, Depends(require_auth)],
    ) -> ValidationResultView:
        """Dry-run a sample record (Turtle body) against the shape (authenticated)."""
        del ctx  # any authenticated user may preview; no mutation occurs
        body = (await request.body()).decode("utf-8")
        if not body.strip():
            raise BadRequest("validate requires a non-empty Turtle sample body")
        return await service.validate_sample(schema_id, body)

    return router


__all__ = [
    "SchemaInfo",
    "SchemaListView",
    "SchemaService",
    "ValidationResultView",
    "build_schema_router",
]
