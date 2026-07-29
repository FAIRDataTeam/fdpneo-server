"""One-shot reconciliation: move profile schemas into the schemas namespace.

Task 10.5 changed where the applier *stores* profile schemas — from each
schema's vocabulary/class IRI (e.g. ``http://www.w3.org/ns/dcat#Catalog``) to
the reserved schemas namespace (``{base}/fdp-api/schemas/{slug}``), the same
place a runtime ``PUT /schemas/{id}`` writes — so a profile schema is thereafter
an ordinary listable/editable user schema. Deployments bootstrapped *before*
that change still have their schemas at the old vocabulary IRIs, with resource
definitions pointing ``ldp:constrainedBy`` there.

A ``force-apply`` would fix the layout but wipes the whole store (and any
authored records), so this module provides a **non-destructive** reconciliation
instead, driven by ``fdp schema migrate-namespace``:

1. For each schema the profile declares, compute ``old`` (vocabulary IRI) and
   ``new`` (storage IRI). Where they differ and a graph exists at ``old``, copy
   it to ``new`` and drop ``old`` (and its ``/meta`` sibling).
2. Repoint every resource-definition record whose ``ldp:constrainedBy`` names a
   moved schema at the new IRI.
3. Invalidate the validator's compiled-shape cache for the touched IRIs.

Idempotent: a schema already at ``new`` (nothing at ``old``) is skipped, so a
second run is a no-op. The reconciliation is *not* transactional across both
stores (consistent with the applier, ADR-0005); it is ordered new-before-old so
an interruption leaves the schema readable at both IRIs rather than neither.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

from fdpneo_server.metadata.profiles.iri import IRIExpander
from fdpneo_server.metadata.profiles.rd_records import record_to_graph
from fdpneo_server.metadata.profiles.rd_service import load_definition_records
from fdpneo_server.metadata.states import SEED_STATE

if TYPE_CHECKING:
    from fdpneo_server.config import Settings
    from fdpneo_server.metadata.profiles.manifest import DeploymentProfile
    from fdpneo_server.metadata.profiles.rd_records import ResourceDefinitionRecord
    from fdpneo_server.metadata.repository import MetadataRepository
    from fdpneo_server.metadata.shacl import ShaclValidator
    from fdpneo_server.storage.triplestore import TripleStoreAdapter

log = structlog.get_logger(__name__)


@dataclass
class MigrationReport:
    """What the reconciliation changed (or would have)."""

    moved: list[tuple[str, str]] = field(default_factory=list)
    """``(old_iri, new_iri)`` for each schema graph relocated."""

    resource_definitions_repointed: list[str] = field(default_factory=list)
    """IRIs of RD records whose ``ldp:constrainedBy`` was rewritten."""

    already_migrated: list[str] = field(default_factory=list)
    """``new`` IRIs that needed no move (nothing at ``old``)."""

    @property
    def changed(self) -> bool:
        return bool(self.moved or self.resource_definitions_repointed)


async def migrate_schema_namespace(
    profile: DeploymentProfile,
    *,
    repository: MetadataRepository,
    adapter: TripleStoreAdapter,
    settings: Settings,
    validator: ShaclValidator | None = None,
) -> MigrationReport:
    """Relocate ``profile``'s schemas to the storage namespace and repoint RDs."""
    expander = IRIExpander(settings=settings)
    # old vocabulary IRI -> new storage IRI, for schemas whose location moved.
    mapping: dict[str, str] = {}
    for loaded in profile.schemas:
        old = expander.schema_iri(loaded.entry.id)
        new = expander.schema_storage_iri(loaded.entry.id)
        if old != new:
            mapping[old] = new

    report = MigrationReport()
    if not mapping:
        return report

    # 1. Move each schema graph old -> new (new first, then drop old).
    for old, new in mapping.items():
        graph = await repository.get_graph(old)
        if len(graph) == 0:
            # Nothing at the old IRI: either never present or already migrated.
            report.already_migrated.append(new)
            continue
        await repository.put_graph(new, graph, subject=None, initial_state=SEED_STATE)
        await repository.delete_graph(old)
        report.moved.append((old, new))
        if validator is not None:
            validator.invalidate(old)
            validator.invalidate(new)
        log.info("schema_namespace_migrated", old=old, new=new)

    # 2. Repoint resource definitions that still reference a moved schema.
    records = await load_definition_records(adapter)
    for record in records:
        new_iri = mapping.get(record.schema_iri)
        if new_iri is None:
            continue
        rd_iri = _rd_record_iri(record, expander)
        repointed = _with_schema(record, new_iri)
        await repository.put_graph(rd_iri, record_to_graph(repointed, rd_iri), subject=None)
        report.resource_definitions_repointed.append(rd_iri)
        log.info("resource_definition_repointed", rd=rd_iri, schema=new_iri)

    return report


def _rd_record_iri(record: ResourceDefinitionRecord, expander: IRIExpander) -> str:
    from fdpneo_server.metadata.profiles.rd_records import rd_record_slug
    from fdpneo_server.shared.graphs import resource_definition_graph_uri

    return str(
        resource_definition_graph_uri(
            expander.base_url, rd_record_slug(record.url_prefix, record.name)
        )
    )


def _with_schema(record: ResourceDefinitionRecord, schema_iri: str) -> ResourceDefinitionRecord:
    from dataclasses import replace

    return replace(record, schema_iri=schema_iri)


__all__ = ["MigrationReport", "migrate_schema_namespace"]
