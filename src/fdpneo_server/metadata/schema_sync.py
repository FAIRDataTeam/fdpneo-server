"""Remote schema synchronization (Phase 10.2, mirrors the RI's
remote-schema refresh in ``MetadataSchemaService``).

A published SHACL schema (Phase 10.1) may carry a ``dct:source <remote-url>``
triple. When sync runs, the server refetches that URL, and — if the remote
shape's *content* differs from what is stored — republishes it through the
ordinary schema-admin write path, which bumps the shape's ``owl:versionInfo``
at its stable IRI (so resource-definition ``schema`` references stay valid).

**Opt-in, twice over.** Sync touches the network, so it is off unless an
operator turns it on:

* The schema must declare ``dct:source`` — no source, no sync (per-schema
  opt-in).
* The source host must be on :attr:`SchemaSyncSettings.allowed_hosts` — an
  empty allow-list denies every fetch. This is the structural "no
  unauthenticated outbound fetches to arbitrary IRIs" boundary and is checked
  on every fetch, even for a manual run.

The scheduled background pass is additionally gated by
:attr:`SchemaSyncSettings.enabled`; a manual ``fdp schema sync`` is an explicit
operator action and runs the pass regardless of that flag (the allow-list still
applies).

**Change detection** uses RDF graph isomorphism (``rdflib.compare``), not a
byte/ETag compare: SHACL shapes are blank-node heavy (``sh:property`` nodes),
so two serializations of the same shape differ textually while being the same
graph. The bookkeeping ``dct:source`` triple is excluded from the comparison
and re-stamped (canonically, on the schema record IRI) on every republish, so
it neither triggers a spurious change nor gets lost.

**Scheduling.** Like the metrics rollups, the work is a plain async pass
(:meth:`SchemaSyncService.sync_all`) invoked by ``fdp schema sync`` from an
external scheduler (cron / k8s ``CronJob``) at
:attr:`SchemaSyncSettings.interval_seconds`. Per-schema failures are collected,
logged, and never abort the batch.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final
from urllib.parse import urlparse

import httpx
import structlog
from rdflib import Graph, URIRef
from rdflib.compare import isomorphic

from fdpneo_server.shared.errors import FDPError
from fdpneo_server.shared.graphs import schema_namespace
from fdpneo_server.shared.namespaces import DCT

if TYPE_CHECKING:
    from fdpneo_server.config import SchemaSyncSettings
    from fdpneo_server.metadata.schemas import SchemaInfo, SchemaService
    from fdpneo_server.storage.triplestore.adapter import TripleStoreAdapter

log = structlog.get_logger(__name__)

_SPARQL_JSON: Final = "application/sparql-results+json"

# Formats tried when parsing a fetched body, in order. Turtle first (the
# documented shape format); RDF/XML and JSON-LD as fallbacks driven by the
# remote's media type when present.
_FORMAT_BY_CONTENT_TYPE: Final = {
    "text/turtle": "turtle",
    "application/x-turtle": "turtle",
    "application/rdf+xml": "xml",
    "application/ld+json": "json-ld",
    "application/n-triples": "nt",
}


# --- value types -----------------------------------------------------------


@dataclass(frozen=True)
class RemoteSchema:
    """A published schema that declares a ``dct:source`` to sync from."""

    schema_id: str
    iri: str
    source_url: str


class SyncStatus(StrEnum):
    """Outcome of syncing one schema."""

    UPDATED = "updated"
    UNCHANGED = "unchanged"
    SKIPPED = "skipped"  # source host not on the allow-list
    FAILED = "failed"  # fetch / parse / store error


@dataclass(frozen=True)
class SyncOutcome:
    """Result of syncing a single :class:`RemoteSchema`."""

    schema: RemoteSchema
    status: SyncStatus
    detail: str | None = None
    new_version: int | None = None


@dataclass
class SyncReport:
    """Aggregate result of a full sync pass."""

    outcomes: list[SyncOutcome]

    def _count(self, status: SyncStatus) -> int:
        return sum(1 for o in self.outcomes if o.status is status)

    @property
    def updated(self) -> int:
        return self._count(SyncStatus.UPDATED)

    @property
    def unchanged(self) -> int:
        return self._count(SyncStatus.UNCHANGED)

    @property
    def skipped(self) -> int:
        return self._count(SyncStatus.SKIPPED)

    @property
    def failed(self) -> int:
        return self._count(SyncStatus.FAILED)


# --- service ---------------------------------------------------------------


class SchemaSyncService:
    """Discovers remote-sourced schemas and refreshes the changed ones.

    Stateless aside from its collaborators; safe to construct per run.
    """

    def __init__(
        self,
        *,
        schema_service: SchemaService,
        adapter: TripleStoreAdapter,
        http_client: httpx.AsyncClient,
        settings: SchemaSyncSettings,
        base_url: str,
    ) -> None:
        self._schemas = schema_service
        self._adapter = adapter
        self._http = http_client
        self._settings = settings
        self._base = base_url.rstrip("/")

    async def sync_all(self) -> SyncReport:
        """Run one sync pass over every schema declaring a ``dct:source``."""
        remotes = await self.discover()
        log.info("schema_sync_started", candidates=len(remotes))
        outcomes: list[SyncOutcome] = []
        for remote in remotes:
            outcomes.append(await self.sync_one(remote))
        report = SyncReport(outcomes=outcomes)
        log.info(
            "schema_sync_completed",
            updated=report.updated,
            unchanged=report.unchanged,
            skipped=report.skipped,
            failed=report.failed,
        )
        return report

    async def discover(self) -> list[RemoteSchema]:
        """Find published schemas under ``{base}/schemas/`` with a ``dct:source``."""
        query = (
            "SELECT ?g ?src WHERE {"
            f" GRAPH ?g {{ ?s <{DCT.source}> ?src }}"
            f' FILTER(STRSTARTS(STR(?g), "{schema_namespace(self._base)}/"))'
            " FILTER(isIRI(?src)) }"
        )
        body = await self._adapter.query(query, accept=_SPARQL_JSON)
        bindings = json.loads(body).get("results", {}).get("bindings", [])
        # One source per schema graph; first binding wins if several are present.
        seen: dict[str, RemoteSchema] = {}
        for row in bindings:
            iri = row.get("g", {}).get("value")
            src = row.get("src", {}).get("value")
            if not iri or not src or iri in seen:
                continue
            seen[iri] = RemoteSchema(schema_id=iri.rsplit("/", 1)[-1], iri=iri, source_url=src)
        return sorted(seen.values(), key=lambda r: r.schema_id)

    async def sync_one(self, remote: RemoteSchema) -> SyncOutcome:
        """Fetch, compare, and (if changed) republish a single schema."""
        if not self._host_allowed(remote.source_url):
            log.warning(
                "schema_sync_host_not_allowed",
                schema=remote.schema_id,
                source=remote.source_url,
            )
            return SyncOutcome(remote, SyncStatus.SKIPPED, detail="source host not on allow-list")
        try:
            fetched = await self._fetch(remote.source_url)
            current = await self._current_graph(remote.schema_id)
            if isomorphic(_content(fetched), _content(current)):
                return SyncOutcome(remote, SyncStatus.UNCHANGED)
            info = await self._republish(remote, fetched)
        except FDPError as err:
            log.warning("schema_sync_failed", schema=remote.schema_id, error=err.message)
            return SyncOutcome(remote, SyncStatus.FAILED, detail=err.message)
        except Exception as err:
            log.warning("schema_sync_failed", schema=remote.schema_id, error=repr(err))
            return SyncOutcome(remote, SyncStatus.FAILED, detail=str(err))
        log.info(
            "schema_sync_updated",
            schema=remote.schema_id,
            source=remote.source_url,
            version=info.version,
        )
        return SyncOutcome(remote, SyncStatus.UPDATED, new_version=info.version)

    # --- internals ---------------------------------------------------------

    def _host_allowed(self, url: str) -> bool:
        host = urlparse(url).hostname
        return host is not None and host in set(self._settings.allowed_hosts)

    async def _fetch(self, url: str) -> Graph:
        """Stream the remote body (size-capped) and parse it into a graph."""
        max_bytes = self._settings.max_bytes
        chunks: list[bytes] = []
        total = 0
        async with self._http.stream(
            "GET",
            url,
            timeout=self._settings.timeout_seconds,
            headers={"Accept": "text/turtle, application/rdf+xml;q=0.8, */*;q=0.1"},
        ) as response:
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise _SyncError(f"remote shape exceeds the {max_bytes}-byte cap")
                chunks.append(chunk)
        return _parse(b"".join(chunks), content_type=content_type)

    async def _current_graph(self, schema_id: str) -> Graph:
        graph = Graph()
        try:
            turtle = await self._schemas.get_turtle(schema_id)
        except FDPError:
            return graph  # not stored yet → empty, forces an update
        graph.parse(data=turtle, format="turtle")
        return graph

    async def _republish(self, remote: RemoteSchema, fetched: Graph) -> SchemaInfo:
        """Store the fetched shape with a canonical ``dct:source`` re-stamped."""
        to_store = Graph()
        for triple in fetched:
            if triple[1] == DCT.source:
                continue
            to_store.add(triple)
        to_store.add((URIRef(remote.iri), DCT.source, URIRef(remote.source_url)))
        # Goes through the admin write path: validates the shape, bumps the
        # version, and re-warms the validator cache.
        return await self._schemas.put(
            remote.schema_id, to_store.serialize(format="turtle"), subject=None
        )


# --- module-private error + helpers ----------------------------------------


class _SyncError(FDPError):
    """A recoverable per-schema sync failure (collected, never fatal)."""

    code = "fdp.schema_sync_failed"
    http_status = 502
    docs_url = "https://specs.fairdatapoint.org/errors#fdp.schema_sync_failed"


def _content(graph: Graph) -> Graph:
    """A copy of ``graph`` without ``dct:source`` bookkeeping, for comparison."""
    out = Graph()
    for triple in graph:
        if triple[1] != DCT.source:
            out.add(triple)
    return out


def _parse(body: bytes, *, content_type: str) -> Graph:
    """Parse a fetched body, picking the rdflib format from the media type.

    Falls back across Turtle / RDF/XML / JSON-LD when the media type is absent
    or unhelpful, since the remote may not set a precise ``Content-Type``.
    """
    media = content_type.split(";", 1)[0].strip().lower()
    preferred = _FORMAT_BY_CONTENT_TYPE.get(media)
    candidates = [preferred] if preferred else []
    candidates += [f for f in ("turtle", "xml", "json-ld") if f != preferred]
    last_err: Exception | None = None
    for fmt in candidates:
        graph = Graph()
        try:
            graph.parse(data=body, format=fmt)
        except Exception as err:  # try the next candidate format
            last_err = err
            continue
        return graph
    raise _SyncError(f"remote body is not parseable RDF: {last_err}")


__all__ = [
    "RemoteSchema",
    "SchemaSyncService",
    "SyncOutcome",
    "SyncReport",
    "SyncStatus",
]
