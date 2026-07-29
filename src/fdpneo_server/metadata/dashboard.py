"""User dashboard endpoint (task 6.3).

Returns the records the current user owns, can edit, or has recently
modified — the data backing "My data" screens in ``fdp-client``.

Three lists per response, each list bounded:

* ``owned`` — records whose ``dct:creator`` matches the caller. Sourced
  by SPARQL against the knowledge graph. Bound by ``owned_limit``.
* ``recent`` — records the caller has modified in the recent past,
  derived from the Postgres ``record_audit`` table. Bound by
  ``recent_limit``.
* ``editable`` — records the PDP has previously decided are writable by
  the caller, read from the authorization cache. **Cache-bounded** —
  see :class:`DashboardService` for the trade-off.

Each item carries record IRI, type IRI, ``dct:title`` and a
last-modified timestamp. Items the triple store cannot resolve (e.g.
the record was deleted but the audit row persists) include
``record_iri`` and ``last_modified`` but no title.

Auth model: anonymous → 401. Authenticated callers see only their own
records. Callers with the configured admin role can pass
``?as_admin=true`` to receive system-wide results.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any, Final

import structlog
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import desc, func, select

from fdpneo_server.identity.deps import require_auth
from fdpneo_server.metadata.audit import AuditOperation, RecordAuditRow
from fdpneo_server.metadata.labels import is_safe_iri
from fdpneo_server.policy.model import Action
from fdpneo_server.shared.context import RequestContext
from fdpneo_server.shared.errors import Forbidden

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from fdpneo_server.policy.runtime import RequestScopedPDP
    from fdpneo_server.storage.triplestore.adapter import TripleStoreAdapter


log = structlog.get_logger(__name__)

_ADMIN_ROLE: Final = "admin"

_DEFAULT_OWNED_LIMIT: Final = 100
_DEFAULT_RECENT_LIMIT: Final = 30
_DEFAULT_EDITABLE_LIMIT: Final = 100
_MAX_LIMIT: Final = 500


# --- response models -------------------------------------------------------


class DashboardItem(BaseModel):
    """One record summary surfaced on the dashboard."""

    record_iri: str
    type_iri: str | None = None
    title: str | None = None
    state: str | None = None
    """Publication state (ADR-0010): DRAFT / PUBLISHED / ARCHIVED. ``None`` when
    the store has no state for the record (e.g. it was deleted)."""
    last_modified: datetime | None = None


class DashboardResponse(BaseModel):
    """Response shape for ``GET /me/dashboard``."""

    owned: list[DashboardItem]
    editable: list[DashboardItem]
    recent: list[DashboardItem]


# --- service ---------------------------------------------------------------


class DashboardService:
    """Builds dashboard responses for a given subject.

    Composes three readers (SPARQL for owned + enrichment; SQL for
    recent; PDP cache for editable). ``editable`` is read from the
    authorization cache — it reflects what the PDP has actually
    evaluated, not the full set the user could in principle edit.
    Warming the cache (e.g. anonymous-startup pre-evaluation, or a
    user-login pre-warm hook) is the work that promotes ``editable``
    from "things you've touched" to "everything you could touch". That
    enhancement is intentionally deferred (architecture §15).
    """

    def __init__(
        self,
        *,
        adapter: TripleStoreAdapter,
        session_factory: async_sessionmaker[AsyncSession],
        pdp: RequestScopedPDP,
    ) -> None:
        self._adapter = adapter
        self._session_factory = session_factory
        self._pdp = pdp

    async def for_subject(
        self,
        ctx: RequestContext,
        *,
        owned_limit: int = _DEFAULT_OWNED_LIMIT,
        recent_limit: int = _DEFAULT_RECENT_LIMIT,
        editable_limit: int = _DEFAULT_EDITABLE_LIMIT,
        as_admin: bool = False,
    ) -> DashboardResponse:
        """Build the dashboard for ``ctx``.

        When ``as_admin`` is True, the subject filter is dropped — the
        caller sees every record. The caller must hold the admin role;
        the router enforces that before calling.
        """
        subject = None if as_admin else ctx.subject
        owned, recent, editable_iris = await self._gather(
            subject=subject,
            ctx=ctx,
            owned_limit=owned_limit,
            recent_limit=recent_limit,
            editable_limit=editable_limit,
            as_admin=as_admin,
        )
        # Enrich the editable list against the triple store. recent
        # already carries its last_modified; editable needs nothing
        # except titles and types.
        editable = await self._enrich(editable_iris) if editable_iris else []
        return DashboardResponse(owned=owned, editable=editable, recent=recent)

    async def _gather(
        self,
        *,
        subject: str | None,
        ctx: RequestContext,
        owned_limit: int,
        recent_limit: int,
        editable_limit: int,
        as_admin: bool,
    ) -> tuple[list[DashboardItem], list[DashboardItem], list[str]]:
        owned = await self._read_owned(subject, owned_limit)
        recent = await self._read_recent(subject, recent_limit)
        # ``authorized_graphs`` is keyed on the calling subject's
        # current role set. For admin-as-admin we still scope to what
        # *they* personally can modify — system-wide enumeration of
        # every editable record across all users would be a different
        # query and is not what the dashboard advertises.
        editable_iris: list[str] = []
        if not as_admin or subject is None:  # treat admin view same as own here
            cached = await self._pdp.authorized_graphs(ctx, Action.MODIFY)
            # Drop the IRIs that already appear in ``owned`` so the two
            # lists don't duplicate. ``recent`` legitimately overlaps
            # the others — it's a chronological view, not a permission
            # view, and the client renders the three as separate tabs.
            owned_iris = {item.record_iri for item in owned}
            editable_iris = sorted(cached - owned_iris)[:editable_limit]
        return owned, recent, editable_iris

    # --- owned (SPARQL: dct:creator) -------------------------------------

    async def _read_owned(self, subject: str | None, limit: int) -> list[DashboardItem]:
        # Each OPTIONAL gets its own ``GRAPH ?gN`` binding so the
        # enrichment lookups can find type and title in *any* named
        # graph — not only the one carrying ``dct:creator``. Seed
        # records typically split their content (creator, type) and
        # their human label (title) across the record and meta graphs,
        # so a single shared ``?g`` returned ``title: null`` for every
        # owned item. Cross-graph cartesian products collapse to one
        # item per IRI inside ``_select_to_items``.
        creator_clause: str
        if subject is None:
            # ``as_admin`` path: enumerate every dct:creator. Bounded
            # by ``limit``; ordered by IRI for stability.
            creator_clause = (
                "  GRAPH ?g_creator {\n    ?iri <http://purl.org/dc/terms/creator> ?creator\n  }\n"
            )
        elif not is_safe_iri(subject):
            return []
        else:
            creator_clause = (
                "  GRAPH ?g_creator {\n"
                f"    ?iri <http://purl.org/dc/terms/creator> <{subject}>\n"
                "  }\n"
            )
        sparql = (
            "SELECT ?iri ?type ?title ?state WHERE {\n"
            f"{creator_clause}"
            "  OPTIONAL { GRAPH ?g_type { ?iri a ?type } }\n"
            "  OPTIONAL { GRAPH ?g_title { ?iri <http://purl.org/dc/terms/title> ?title } }\n"
            "  OPTIONAL { GRAPH ?g_state { ?iri <https://w3id.org/fdp/o#metadataState> ?state } }\n"
            "} ORDER BY ?iri\n"
            f"LIMIT {int(limit)}\n"
        )
        return _select_to_items(await self._adapter.query(sparql))

    # --- recent (Postgres audit) -----------------------------------------

    async def _read_recent(self, subject: str | None, limit: int) -> list[DashboardItem]:
        async with self._session_factory() as session:
            # Group by record_iri so we get one row per record with
            # its most-recent timestamp. Exclude delete events: a
            # deleted record can no longer be enriched and shouldn't
            # appear in a "my recent edits" list.
            stmt = (
                select(
                    RecordAuditRow.record_iri,
                    func.max(RecordAuditRow.occurred_at).label("last_at"),
                )
                .where(RecordAuditRow.operation != AuditOperation.DELETE.value)
                .group_by(RecordAuditRow.record_iri)
                .order_by(desc("last_at"))
                .limit(limit)
            )
            if subject is not None:
                stmt = stmt.where(RecordAuditRow.subject == subject)
            rows = (await session.execute(stmt)).all()

        if not rows:
            return []
        enriched = await self._enrich([str(row[0]) for row in rows])
        by_iri = {item.record_iri: item for item in enriched}
        items: list[DashboardItem] = []
        for record_iri, last_at in rows:
            base = by_iri.get(str(record_iri)) or DashboardItem(record_iri=str(record_iri))
            items.append(
                DashboardItem(
                    record_iri=base.record_iri,
                    type_iri=base.type_iri,
                    title=base.title,
                    state=base.state,
                    last_modified=_ensure_utc(last_at),
                )
            )
        return items

    # --- enrich (SPARQL VALUES on an IRI list) ---------------------------

    async def _enrich(self, iris: list[str]) -> list[DashboardItem]:
        """Resolve ``rdf:type`` and ``dct:title`` for each IRI.

        Each OPTIONAL gets its own ``GRAPH ?gN`` so type and title can
        live in different named graphs (record vs meta-metadata, for
        instance) and still both surface. Duplicate rows from the
        cross-graph join are collapsed in :func:`_select_to_items`.
        """
        safe = [iri for iri in iris if is_safe_iri(iri)]
        if not safe:
            return [DashboardItem(record_iri=iri) for iri in iris]
        values_block = " ".join(f"<{iri}>" for iri in safe)
        sparql = (
            "SELECT ?iri ?type ?title ?state WHERE {\n"
            f"  VALUES ?iri {{ {values_block} }}\n"
            "  OPTIONAL { GRAPH ?g_type { ?iri a ?type } }\n"
            "  OPTIONAL { GRAPH ?g_title { ?iri <http://purl.org/dc/terms/title> ?title } }\n"
            "  OPTIONAL { GRAPH ?g_state { ?iri <https://w3id.org/fdp/o#metadataState> ?state } }\n"
            "}\n"
        )
        items_by_iri = {
            item.record_iri: item for item in _select_to_items(await self._adapter.query(sparql))
        }
        # Preserve the input order; fill in placeholders for IRIs the
        # store knows nothing about so the response stays stable.
        return [items_by_iri.get(iri, DashboardItem(record_iri=iri)) for iri in iris]


# --- helpers ---------------------------------------------------------------


def _select_to_items(body: bytes) -> list[DashboardItem]:
    """Parse a SPARQL JSON SELECT into one ``DashboardItem`` per ``?iri``.

    Multiple rows for the same IRI (different ``?type`` or ``?title``
    cardinalities) collapse into one item; we keep the first non-empty
    ``type_iri`` / ``title`` we see, which matches the SPARQL endpoint's
    binding order.
    """
    payload: dict[str, Any] = json.loads(body)
    bindings: list[dict[str, Any]] = payload.get("results", {}).get("bindings", [])
    by_iri: dict[str, DashboardItem] = {}
    for row in bindings:
        iri_term = row.get("iri", {})
        iri = iri_term.get("value")
        if not iri:
            continue
        type_term = row.get("type", {})
        title_term = row.get("title", {})
        state_term = row.get("state", {})
        existing = by_iri.get(iri)
        type_iri = (existing.type_iri if existing else None) or type_term.get("value")
        title = (existing.title if existing else None) or title_term.get("value")
        state = (existing.state if existing else None) or state_term.get("value")
        by_iri[iri] = DashboardItem(
            record_iri=iri,
            type_iri=type_iri or None,
            title=title or None,
            state=state or None,
        )
    return list(by_iri.values())


def _ensure_utc(value: datetime) -> datetime:
    """Coerce naive datetimes to UTC; pass through aware ones unchanged.

    Postgres' ``timestamp with time zone`` always returns aware values
    through SQLAlchemy's typed mapping, but tests that build rows
    without a session may produce naive ones — be lenient.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


# --- router ----------------------------------------------------------------


def build_dashboard_router(*, service: DashboardService) -> APIRouter:
    """Construct ``GET /me/dashboard``.

    Authentication is required (anonymous → 401). ``?as_admin=true``
    requires the caller to hold the ``admin`` role; passing it
    without the role surfaces a structured ``fdp.forbidden`` 403.
    """
    router = APIRouter(tags=["dashboard"])

    @router.get(
        "/me/dashboard",
        response_model=DashboardResponse,
        name="user_dashboard",
    )
    async def user_dashboard(  # pyright: ignore[reportUnusedFunction]
        ctx: Annotated[RequestContext, Depends(require_auth)],
        owned_limit: Annotated[
            int, Query(ge=1, le=_MAX_LIMIT, description="Maximum records in 'owned'.")
        ] = _DEFAULT_OWNED_LIMIT,
        recent_limit: Annotated[
            int, Query(ge=1, le=_MAX_LIMIT, description="Maximum records in 'recent'.")
        ] = _DEFAULT_RECENT_LIMIT,
        editable_limit: Annotated[
            int,
            Query(ge=1, le=_MAX_LIMIT, description="Maximum records in 'editable'."),
        ] = _DEFAULT_EDITABLE_LIMIT,
        as_admin: Annotated[
            bool,
            Query(
                description=(
                    "Drop the subject filter and return system-wide results. "
                    "Requires the admin role."
                ),
            ),
        ] = False,
    ) -> DashboardResponse:
        if as_admin and _ADMIN_ROLE not in ctx.roles:
            raise Forbidden(
                "admin role required for system-wide dashboard view",
                details={"required_role": _ADMIN_ROLE},
            )
        return await service.for_subject(
            ctx,
            owned_limit=owned_limit,
            recent_limit=recent_limit,
            editable_limit=editable_limit,
            as_admin=as_admin,
        )

    return router


__all__ = [
    "DashboardItem",
    "DashboardResponse",
    "DashboardService",
    "build_dashboard_router",
]
