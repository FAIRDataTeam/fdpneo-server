"""Runtime resource-definition store reads + the mutation coordinator.

Where :mod:`rd_records` owns the RDF record form and :mod:`registry` owns the
resolved in-memory cache, this module bridges them to the triple store
(ADR-0009):

* :func:`build_cache_from_repository` reads every resource-definition record
  back out of the store and resolves them into a fresh
  :class:`ResourceDefinitionCache`. This is what makes the cache a
  *projection* of the triple store rather than of the profile manifest — the
  set of types survives a restart and reflects runtime mutations.
* :class:`ResourceDefinitionService` is the single coordinator for runtime
  mutations: it writes a record through the metadata repository (so meta-
  metadata + SHACL validation happen), rebuilds the cache, and hands it to a
  caller-supplied ``on_rebuilt`` callback. The admin API (a later task) wires
  ``on_rebuilt`` to swap ``app.state.resource_definitions``, clear the cached
  OpenAPI, and warm the validator/authz caches — keeping this module free of
  any ``app.state`` / HTTP coupling so it stays unit-testable.

Enumeration relies on the one-graph-per-record invariant: each RD record's
subject IRI equals its graph IRI, so listing the graphs that contain a
``?rd a fdp:ResourceDefinition`` triple yields the record IRIs directly.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Protocol

import structlog

from fdp.metadata.profiles.rd_records import (
    RD_SHAPE_IRI,
    ResourceDefinitionRecord,
    rd_record_slug,
    record_from_graph,
    record_to_graph,
)
from fdp.metadata.profiles.registry import ResourceDefinitionCache, resolve_cache
from fdp.shared.graphs import record_graph_uri, resource_definition_graph_uri
from fdp.shared.namespaces import FDP_RESOURCE_DEFINITION, SH
from fdp.storage.triplestore.adapter import SPARQL_JSON, construct_named_graph

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from fdp.metadata.repository import MetadataRepository
    from fdp.metadata.shacl import ShaclValidator
    from fdp.storage.triplestore import TripleStoreAdapter

log = structlog.get_logger(__name__)


# --- store reads -----------------------------------------------------------


_LIST_RD_IRIS = f"SELECT DISTINCT ?rd WHERE {{ GRAPH ?g {{ ?rd a <{FDP_RESOURCE_DEFINITION}> }} }}"


async def list_definition_iris(adapter: TripleStoreAdapter) -> list[str]:
    """Return the IRI of every resource-definition record in the store."""
    body = await adapter.query(_LIST_RD_IRIS, accept=SPARQL_JSON)
    payload = json.loads(body)
    bindings = payload.get("results", {}).get("bindings", [])
    return [b["rd"]["value"] for b in bindings if "rd" in b]


async def load_definition_records(
    adapter: TripleStoreAdapter,
) -> list[ResourceDefinitionRecord]:
    """Read and parse every resource-definition record from the store."""
    records: list[ResourceDefinitionRecord] = []
    for iri in await list_definition_iris(adapter):
        graph = await construct_named_graph(adapter, str(record_graph_uri(iri)))
        records.append(record_from_graph(graph, iri))
    return records


async def build_cache_from_repository(
    adapter: TripleStoreAdapter,
    *,
    base_url: str,
) -> ResourceDefinitionCache:
    """Rebuild the resolved cache from the resource-definition records on disk.

    The triple store is the source of truth (ADR-0009); this is the function
    startup and every runtime mutation call to get the current cache.
    """
    records = await load_definition_records(adapter)
    return resolve_cache(records, base_url=base_url.rstrip("/"))


# --- mutation coordinator --------------------------------------------------


class _CacheSink(Protocol):
    """Callback invoked with the freshly rebuilt cache after each mutation."""

    def __call__(self, cache: ResourceDefinitionCache) -> Awaitable[None]: ...


class ResourceDefinitionService:
    """Coordinates runtime create / update / delete of resource definitions.

    Each mutation writes through the metadata repository (meta-metadata +
    storage), rebuilds the cache from the store, and notifies ``on_rebuilt``.
    Validation against the predefined RD shape runs when a ``validator`` is
    supplied. Authorization, url-prefix uniqueness, and reserved-path
    collision checks live in the admin API that drives this service — this
    class is the storage + cache-coherence core only.
    """

    def __init__(
        self,
        *,
        repository: MetadataRepository,
        adapter: TripleStoreAdapter,
        base_url: str,
        validator: ShaclValidator | None = None,
        on_rebuilt: _CacheSink | None = None,
    ) -> None:
        self._repository = repository
        self._adapter = adapter
        self._base_url = base_url.rstrip("/")
        self._validator = validator
        self._on_rebuilt = on_rebuilt

    def record_iri(self, record: ResourceDefinitionRecord) -> str:
        """The graph IRI a record is stored at (stable across mutations)."""
        return str(
            resource_definition_graph_uri(
                self._base_url, rd_record_slug(record.url_prefix, record.name)
            )
        )

    async def schema_exists(self, schema_iri: str) -> bool:
        """True iff a published SHACL shape lives at ``schema_iri``.

        A type may only point at a shape that has actually been published as a
        record (ADR-0009's explicit two-step flow: publish the shape, then
        register the definition). The check is stronger than "graph is
        non-empty": the graph must declare a ``sh:NodeShape`` or carry a
        ``sh:targetClass``, so a definition can't be wired to an arbitrary
        non-shape record. pySHACL infers a node shape from ``sh:targetClass``
        even without the explicit ``rdf:type``, so both forms count.
        """
        return await self._adapter.ask(
            f"ASK {{ GRAPH <{schema_iri}> {{"
            f" {{ ?s a <{SH.NodeShape}> }} UNION {{ ?s <{SH.targetClass}> ?c }}"
            f" }} }}"
        )

    async def rebuild(self) -> ResourceDefinitionCache:
        """Rebuild the cache from the store and notify ``on_rebuilt``."""
        cache = await build_cache_from_repository(self._adapter, base_url=self._base_url)
        await self._notify(cache)
        return cache

    async def put(
        self,
        record: ResourceDefinitionRecord,
        *,
        subject: str | None = None,
    ) -> ResourceDefinitionCache:
        """Create or replace a resource definition, then rebuild the cache.

        Used for both create and update — the IRI is derived from the
        record, so replacing an existing definition overwrites its graph.
        """
        iri = self.record_iri(record)
        graph = record_to_graph(record, iri)
        if self._validator is not None:
            report = await self._validator.validate_against(graph, RD_SHAPE_IRI)
            report.raise_if_failed()
        await self._repository.put_graph(iri, graph, subject=subject)
        log.info("resource_definition_written", iri=iri, url_prefix=record.url_prefix)
        return await self.rebuild()

    async def delete(self, record: ResourceDefinitionRecord) -> ResourceDefinitionCache:
        """Delete a resource definition's record, then rebuild the cache."""
        iri = self.record_iri(record)
        await self._repository.delete_graph(iri)
        log.info("resource_definition_deleted", iri=iri, url_prefix=record.url_prefix)
        return await self.rebuild()

    async def _notify(self, cache: ResourceDefinitionCache) -> None:
        if self._on_rebuilt is not None:
            await self._on_rebuilt(cache)


__all__ = [
    "ResourceDefinitionService",
    "build_cache_from_repository",
    "list_definition_iris",
    "load_definition_records",
]
