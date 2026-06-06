"""Policy admin API (TASKS Phase 14.2, ADR-0012) — first-class ODRL Offers.

Makes ODRL access conditions **managed, reusable documents**, symmetric with
the runtime SHACL-shape admin (:mod:`fdp.metadata.schemas`). An Offer is no
longer an anonymous fragment seeded only inside the record it governs: it is an
ordinary metadata record stored at the stable, dereferenceable IRI
``{base_url}/policies/{id}``, validated against the FDP ODRL profile
(:func:`fdp.policy.parser.parse_offer`) on every write, and **enforced** by the
PDP — a record opts in via ``dct:rights`` and the existing
:class:`~fdp.policy.resolver.GraphBackedOfferResolver` fetches it by IRI.

Storing the Offer as a record gives it meta-metadata for free, including
``owl:versionInfo`` (bumped per write) — a stable IRI keeps ``dct:rights``
references valid across edits. On every write the PDP authorization cache is
cleared (ADR-0012): a policy can be the effective Offer for many resources via
``dct:isPartOf`` inheritance, so the safe response to an admin-rare policy
change is to invalidate and re-warm lazily.

Surfaces (mirror :mod:`fdp.metadata.schemas`):

* ``GET  /policies``              — list managed policies (public).
* ``GET  /policies/{id}``         — the Offer as Turtle (public, dereferenceable).
* ``PUT  /policies/{id}``         — create/replace, validating the profile (admin).
* ``DELETE /policies/{id}``       — remove (admin; refused if a record still
                                    references it via ``dct:rights``).
* ``POST /policies/{id}/validate`` — dry-run a candidate Offer body against the
                                    profile and return the result (authenticated).

The descriptive metadata (``dct:title`` etc.) lives in the same graph as the
Offer; lifecycle (draft/published/archived) is the shared publication-state
machine (Phase 12) applied to the policy record.
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

from fdp.identity.deps import require_auth
from fdp.metadata.events import RecordDeleted, RecordModified
from fdp.policy.parser import parse_offer
from fdp.shared.context import RequestContext
from fdp.shared.errors import BadRequest, Conflict, Forbidden, NotFound, SchemaViolation
from fdp.shared.graphs import meta_graph_uri, policy_graph_uri
from fdp.shared.namespaces import DCT, ODRL, OWL

if TYPE_CHECKING:
    from fdp.metadata.repository import MetadataRepository
    from fdp.policy.pdp import PDP
    from fdp.shared.events import Event, EventBus
    from fdp.storage.triplestore import TripleStoreAdapter

log = structlog.get_logger(__name__)

_ADMIN_ROLE: Final = "admin"
_SLUG_RE: Final = re.compile(r"^[A-Za-z0-9._-]+$")
_TURTLE: Final = "text/turtle"
_SPARQL_JSON: Final = "application/sparql-results+json"


# --- response models -------------------------------------------------------


class PolicyInfo(BaseModel):
    """Summary of a managed policy."""

    id: str
    iri: str
    title: str | None = None
    assigner: str | None = None
    permissions: int = 0
    prohibitions: int = 0
    version: int | None = None


class PolicyListView(BaseModel):
    policies: list[PolicyInfo]


class ValidationResultView(BaseModel):
    """The outcome of a dry-run profile validation of an Offer body."""

    conforms: bool
    violations: list[dict[str, str | None]]


# --- service ---------------------------------------------------------------


class PolicyService:
    """Runtime ODRL-Offer storage + PDP-cache coherence.

    Stateless aside from its collaborators; safe to share across requests.
    """

    def __init__(
        self,
        *,
        repository: MetadataRepository,
        adapter: TripleStoreAdapter,
        pdp: PDP,
        base_url: str,
        event_bus: EventBus | None = None,
    ) -> None:
        self._repo = repository
        self._adapter = adapter
        self._pdp = pdp
        self._base = base_url.rstrip("/")
        self._event_bus = event_bus

    def iri(self, policy_id: str) -> str:
        return str(policy_graph_uri(self._base, policy_id))

    async def put(self, policy_id: str, turtle: str, *, subject: str | None) -> PolicyInfo:
        """Create or replace policy ``policy_id`` after validating the profile."""
        _check_slug(policy_id)
        iri = self.iri(policy_id)
        graph, offer = _parse_offer_document(turtle, iri)
        etag = await self._repo.put_graph(iri, graph, subject=subject)
        # A policy can be the effective Offer for many resources via inheritance;
        # clear the authz index and let it re-warm lazily (ADR-0012).
        cleared = await self._pdp.invalidate_all()
        # Drive the search indexer + audit log (the doc is searchable content).
        await self._publish(
            RecordModified(record_iri=iri, subject=subject, etag=etag, timestamp=datetime.now(UTC))
        )
        log.info("policy_published", iri=iri, subject=subject, cache_cleared=cleared)
        return _info(policy_id, iri, graph=graph, offer=offer, version=await self._version(iri))

    async def get_turtle(self, policy_id: str) -> str:
        _check_slug(policy_id)
        iri = self.iri(policy_id)
        graph = await self._repo.get_graph(iri)
        if len(graph) == 0:
            raise NotFound(f"no policy: {policy_id}")
        return graph.serialize(format="turtle")

    async def delete(self, policy_id: str) -> None:
        _check_slug(policy_id)
        iri = self.iri(policy_id)
        if await self._is_referenced(iri):
            raise Conflict(
                "policy is referenced by a record via dct:rights; repoint those records first",
                details={"policy": iri},
            )
        if not await self._exists(iri):
            raise NotFound(f"no policy: {policy_id}")
        await self._repo.delete_graph(iri)
        cleared = await self._pdp.invalidate_all()
        await self._publish(
            RecordDeleted(record_iri=iri, subject=None, timestamp=datetime.now(UTC))
        )
        log.info("policy_deleted", iri=iri, cache_cleared=cleared)

    async def validate_body(self, policy_id: str, turtle: str) -> ValidationResultView:
        """Dry-run: parse ``turtle`` against the FDP ODRL profile, no mutation."""
        _check_slug(policy_id)
        iri = self.iri(policy_id)
        try:
            _parse_offer_document(turtle, iri)
        except SchemaViolation as err:
            return ValidationResultView(
                conforms=False,
                violations=[{"message": err.message, **_detail_strings(err.details)}],
            )
        except BadRequest as err:
            return ValidationResultView(conforms=False, violations=[{"message": err.message}])
        return ValidationResultView(conforms=True, violations=[])

    async def list_policies(self) -> list[PolicyInfo]:
        query = (
            "SELECT ?g WHERE { GRAPH ?g {"
            f" ?g a <{ODRL.Offer}> }}"
            f' FILTER(STRSTARTS(STR(?g), "{self._base}/policies/")) }}'
        )
        rows = await self._select(query)
        items: list[PolicyInfo] = []
        for row in rows:
            iri = row.get("g", {}).get("value")
            if not iri:
                continue
            graph = await self._repo.get_graph(iri)
            try:
                offer = parse_offer(graph, URIRef(iri))
            except SchemaViolation:
                offer = None
            items.append(_info(iri.rsplit("/", 1)[-1], iri, graph=graph, offer=offer, version=None))
        items.sort(key=lambda p: p.id)
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
        return await self._adapter.ask(f"ASK {{ GRAPH <{iri}> {{ <{iri}> a <{ODRL.Offer}> }} }}")

    async def _is_referenced(self, iri: str) -> bool:
        return await self._adapter.ask(f"ASK {{ GRAPH ?g {{ ?r <{DCT.rights}> <{iri}> }} }}")

    async def _select(self, query: str) -> list[dict[str, dict[str, str]]]:
        body = await self._adapter.query(query, accept=_SPARQL_JSON)
        payload = json.loads(body)
        return payload.get("results", {}).get("bindings", [])

    async def _publish(self, event: Event) -> None:
        if self._event_bus is None:
            return
        await self._event_bus.publish(event)


def _check_slug(policy_id: str) -> None:
    if not _SLUG_RE.match(policy_id):
        raise BadRequest(
            "policy id must be a slug of letters, digits, '.', '_' or '-'",
            details={"id": policy_id},
        )


def _parse_offer_document(turtle: str, iri: str) -> tuple[Graph, object]:
    """Parse + profile-validate an Offer whose subject is the stable ``iri``.

    Turtle is parsed with ``iri`` as the document base, so an editor may emit a
    relative ``<>`` for the Offer subject. The body must declare ``<iri>`` as an
    ``odrl:Offer``; :func:`parse_offer` then enforces the full FDP profile,
    raising :class:`SchemaViolation` on any out-of-profile construct.
    """
    graph = Graph()
    try:
        graph.parse(data=turtle, format="turtle", publicID=iri)
    except SchemaViolation:
        raise
    except Exception as err:
        raise BadRequest(f"body is not valid Turtle: {err}") from err
    offer = parse_offer(graph, URIRef(iri))  # raises SchemaViolation if non-conforming
    return graph, offer


def _info(
    policy_id: str, iri: str, *, graph: Graph, offer: object, version: int | None
) -> PolicyInfo:
    from fdp.policy.model import Offer

    title = next(iter(graph.objects(URIRef(iri), DCT.title)), None)
    if isinstance(offer, Offer):
        return PolicyInfo(
            id=policy_id,
            iri=iri,
            title=str(title) if title is not None else None,
            assigner=offer.assigner,
            permissions=len(offer.permissions),
            prohibitions=len(offer.prohibitions),
            version=version,
        )
    return PolicyInfo(
        id=policy_id,
        iri=iri,
        title=str(title) if title is not None else None,
        version=version,
    )


def _detail_strings(details: object | None) -> dict[str, str | None]:
    if not isinstance(details, dict):
        return {}
    return {str(k): (None if v is None else str(v)) for k, v in details.items()}


# --- router ----------------------------------------------------------------


def build_policy_router(*, service: PolicyService, prefix: str = "/policies") -> APIRouter:
    """Build the policy-admin router. Reads are public; writes require admin."""
    router = APIRouter(prefix=prefix, tags=["policies"])

    def _require_admin(ctx: RequestContext) -> None:
        if _ADMIN_ROLE not in ctx.roles:
            raise Forbidden(
                "admin role required to manage policies",
                details={"required_role": _ADMIN_ROLE},
            )

    @router.get("", response_model=PolicyListView, name="policy_list")
    async def list_policies() -> PolicyListView:  # pyright: ignore[reportUnusedFunction]
        return PolicyListView(policies=await service.list_policies())

    @router.get("/{policy_id}", name="policy_get")
    async def get_policy(policy_id: str) -> Response:  # pyright: ignore[reportUnusedFunction]
        turtle = await service.get_turtle(policy_id)
        return Response(content=turtle, media_type=_TURTLE)

    @router.put("/{policy_id}", response_model=PolicyInfo, name="policy_put")
    async def put_policy(  # pyright: ignore[reportUnusedFunction]
        policy_id: str,
        request: Request,
        ctx: Annotated[RequestContext, Depends(require_auth)],
    ) -> PolicyInfo:
        """Publish (create or replace) an ODRL Offer (admin only).

        Body is Turtle. The Offer must conform to the FDP ODRL profile; each
        write bumps ``owl:versionInfo`` and clears the PDP authorization cache.
        The stable IRI keeps ``dct:rights`` references valid across edits.
        """
        _require_admin(ctx)
        body = (await request.body()).decode("utf-8")
        if not body.strip():
            raise BadRequest("PUT /policies requires a non-empty Turtle body")
        return await service.put(policy_id, body, subject=ctx.subject)

    @router.delete("/{policy_id}", status_code=204, name="policy_delete")
    async def delete_policy(  # pyright: ignore[reportUnusedFunction]
        policy_id: str,
        ctx: Annotated[RequestContext, Depends(require_auth)],
    ) -> None:
        _require_admin(ctx)
        await service.delete(policy_id)

    @router.post(
        "/{policy_id}/validate", response_model=ValidationResultView, name="policy_validate"
    )
    async def validate_policy(  # pyright: ignore[reportUnusedFunction]
        policy_id: str,
        request: Request,
        ctx: Annotated[RequestContext, Depends(require_auth)],
    ) -> ValidationResultView:
        """Dry-run a candidate Offer (Turtle body) against the profile (authenticated)."""
        del ctx  # any authenticated user may preview; no mutation occurs
        body = (await request.body()).decode("utf-8")
        if not body.strip():
            raise BadRequest("validate requires a non-empty Turtle body")
        return await service.validate_body(policy_id, body)

    return router


__all__ = [
    "PolicyInfo",
    "PolicyListView",
    "PolicyService",
    "ValidationResultView",
    "build_policy_router",
]
