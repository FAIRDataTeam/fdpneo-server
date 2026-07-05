"""ADR-0019 conformance backfill — make existing/seeded records self-describing.

Idempotent and non-destructive. Two phases:

1. **Profiles.** Provision the 1:1 profile (+ immutable schema version snapshot)
   for every managed schema in the store, so ``GET /fdp-api/profiles`` lists them
   and a record's ``dct:conformsTo`` resolves.
2. **Records.** Stamp server-owned ``dct:conformsTo`` (record graph) and
   ``fdp-o:validatedAgainst`` (meta graph) on every non-internal record that
   resolves to a known type and lacks the binding — **without** bumping
   ``owl:versionInfo``/``dct:modified``, because a backfill is not a content edit.

Run once at bootstrap (fresh apply) so seeded records are self-describing, and
via ``fdp profile backfill-conformance`` for deployments created before the
binding shipped. A no-op on an already-bound deployment.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

import structlog
from rdflib import URIRef

from fdp.metadata.prof import ensure_conformance
from fdp.shared.graphs import (
    is_internal_graph_uri,
    is_profile_graph_uri,
    is_schema_graph_uri,
    meta_graph_uri,
    profile_graph_uri,
    schema_graph_uri,
    split_schema_iri,
)
from fdp.shared.namespaces import DCT, FDP_VALIDATED_AGAINST

if TYPE_CHECKING:
    from fdp.metadata.repository import MetadataRepository
    from fdp.storage.triplestore import TripleStoreAdapter

log = structlog.get_logger(__name__)

_SPARQL_JSON: Final = "application/sparql-results+json"
_NT: Final = "application/n-triples"


@dataclass
class ConformanceBackfillReport:
    """What a backfill pass changed."""

    profiles_provisioned: list[str] = field(default_factory=list)
    records_stamped: list[str] = field(default_factory=list)
    already: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.profiles_provisioned or self.records_stamped)


async def backfill_conformance(
    *, adapter: TripleStoreAdapter, repository: MetadataRepository, cache: object
) -> ConformanceBackfillReport:
    """Provision profiles for managed schemas and bind existing records.

    ``cache`` is the resource-definition cache (duck-typed on ``shape_for``); it
    maps a record IRI to the storage IRI of its type's schema.
    """
    report = ConformanceBackfillReport()
    graphs = await _all_graphs(adapter)

    # Phase 1: a profile (+ version snapshot) for every managed *stable* schema.
    for graph_iri in graphs:
        if not is_schema_graph_uri(graph_iri) or is_internal_graph_uri(graph_iri):
            continue
        split = split_schema_iri(graph_iri)
        if split is None:
            continue
        base, slug = split
        if graph_iri != str(schema_graph_uri(base, slug)):
            continue  # a version snapshot (<stable>/<v>), not the stable schema
        if len(await repository.get_graph(profile_graph_uri(base, slug))) > 0:
            continue  # already provisioned — keep the pass idempotent
        resolved = await ensure_conformance(adapter, repository, schema_iri=graph_iri)
        if resolved is not None and resolved[0] not in report.profiles_provisioned:
            report.profiles_provisioned.append(resolved[0])

    # Phase 2: bind every non-internal record of a known type.
    shape_for = getattr(cache, "shape_for", None)
    if shape_for is None:
        return report
    for graph_iri in graphs:
        if is_internal_graph_uri(graph_iri):
            continue
        if is_schema_graph_uri(graph_iri) or is_profile_graph_uri(graph_iri):
            continue
        schema_iri = shape_for(graph_iri)
        if schema_iri is None:
            continue
        resolved = await ensure_conformance(adapter, repository, schema_iri=schema_iri)
        if resolved is None:
            continue
        stable_profile, version_iri = resolved
        if await _stamp_record(
            adapter, repository, record_iri=graph_iri, profile=stable_profile, version=version_iri
        ):
            report.records_stamped.append(graph_iri)
        else:
            report.already += 1

    log.info(
        "conformance_backfill_complete",
        profiles=len(report.profiles_provisioned),
        records=len(report.records_stamped),
        already=report.already,
    )
    return report


async def _all_graphs(adapter: TripleStoreAdapter) -> list[str]:
    body = await adapter.query(
        "SELECT DISTINCT ?g WHERE { GRAPH ?g { ?s ?p ?o } }", accept=_SPARQL_JSON
    )
    bindings = json.loads(body).get("results", {}).get("bindings", [])
    return [b["g"]["value"] for b in bindings if "g" in b]


async def _stamp_record(
    adapter: TripleStoreAdapter,
    repository: MetadataRepository,
    *,
    record_iri: str,
    profile: str,
    version: str,
) -> bool:
    """Add conformsTo (record) + validatedAgainst (meta) if missing; no version bump.

    Returns ``True`` iff anything changed.
    """
    changed = False
    subject = URIRef(record_iri)

    record = await repository.get_graph(record_iri)
    if len(record) > 0:
        bound = any(
            isinstance(obj, URIRef) and is_profile_graph_uri(obj)
            for obj in record.objects(subject, DCT.conformsTo)
        )
        if not bound:
            record.add((subject, DCT.conformsTo, URIRef(profile)))
            await adapter.replace_graph(record_iri, record.serialize(format="nt"), mime=_NT)
            changed = True

    meta = await repository.get_meta(record_iri)
    if len(meta) > 0 and next(iter(meta.objects(subject, FDP_VALIDATED_AGAINST)), None) is None:
        meta.add((subject, FDP_VALIDATED_AGAINST, URIRef(version)))
        await adapter.replace_graph(
            str(meta_graph_uri(record_iri)), meta.serialize(format="nt"), mime=_NT
        )
        changed = True

    return changed


__all__ = ["ConformanceBackfillReport", "backfill_conformance"]
