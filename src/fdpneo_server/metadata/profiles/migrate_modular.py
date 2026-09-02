"""One-shot reconciliation: migrate a deployment to the modular profile (task 15.2).

Task 15.2 rebuilt the default profile's schemas as a *modular* set composed along
the DCAT 3 subclass chain extended with FDP-O, and re-typed the root from
``fdp:Repository`` to ``fdp-o:FAIRDataPoint`` (with its child relation changing
from ``dcat:catalog`` to ``fdp-o:servesMetadata``). A deployment bootstrapped
*before* 15.2 still holds the old non-modular schemas, the old resource
definitions, and a root record typed with the old class.

A ``force-apply`` re-bootstrap fixes the config but **re-seeds the root**, so any
authored root metadata (custom title/description/rights) is clobbered — and while
it does not wipe authored member records (no ``DROP ALL``), it is heavier than a
production operator wants. This module provides a **non-destructive,
content-preserving** reconciliation instead, driven by
``fdp profile migrate-modular``:

1. **Schemas** — write the new modular shapes to their storage IRIs
   (``{base}/fdp-api/schemas/{slug}``), expanding the ``urn:fdp-schema:`` import
   placeholders, skipping any shape already identical (RDF-isomorphism compare,
   since SHACL shapes are blank-node heavy).
2. **Resource definitions** — rebuild every RD record from the new manifest (new
   ``ldp:constrainedBy`` storage IRIs + new child relations + root name), skipping
   any already identical.
3. **Root record re-type** — swap the root's domain ``rdf:type`` to the new class
   and reset its LDP Direct Container config to the new RD's child relations,
   **preserving** every other root triple (title, ``dct:rights``, authored
   metadata). Member records keep their (unchanged) DCAT types and are validated
   lazily against the new composed shapes on their next write.

Idempotent: a second pass finds the schemas/RDs identical and the root already on
the new class, and changes nothing. Not transactional across stores (ADR-0005);
ordered schemas → RDs → root so an interruption still leaves shapes resolvable.
Orphaned old schema graphs (e.g. a renamed root-schema slug) are left in place —
they are unreferenced and removable via ``DELETE /schemas/{id}``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog
from rdflib import URIRef
from rdflib.compare import isomorphic
from rdflib.namespace import OWL, RDF

from fdpneo_server.metadata.prof import provision_profile
from fdpneo_server.metadata.profiles.applier import direct_container_config, root_type_iris
from fdpneo_server.metadata.profiles.iri import IRIExpander, expand_schema_refs, schema_slug
from fdpneo_server.metadata.profiles.rd_records import (
    ResourceDefinitionParseError,
    rd_record_slug,
    record_from_graph,
    record_to_graph,
)
from fdpneo_server.metadata.profiles.rd_service import list_definition_iris
from fdpneo_server.metadata.profiles.registry import (
    build_cache_from_manifest,
    records_from_manifest,
)
from fdpneo_server.metadata.profiles.validator import validate_profile
from fdpneo_server.metadata.states import SEED_STATE
from fdpneo_server.shared.errors import BadRequest
from fdpneo_server.shared.graphs import (
    meta_graph_uri,
    resource_definition_graph_uri,
    schema_version_graph_uri,
)
from fdpneo_server.shared.namespaces import LDP

if TYPE_CHECKING:
    from rdflib import Graph

    from fdpneo_server.config import Settings
    from fdpneo_server.metadata.profiles.manifest import DeploymentProfile
    from fdpneo_server.metadata.repository import MetadataRepository
    from fdpneo_server.metadata.shacl import ShaclValidator
    from fdpneo_server.storage.triplestore import TripleStoreAdapter

log = structlog.get_logger(__name__)

# rdf:type values that describe a resource's LDP interaction model rather than its
# domain class. The root re-type preserves none of the old domain type but keeps
# the LDP config under its own (reset) control, so these are the markers it knows
# to leave to step 3 rather than treat as a stale domain type.
_LDP_TYPES = frozenset(
    {LDP.Resource, LDP.RDFSource, LDP.Container, LDP.DirectContainer, LDP.BasicContainer}
)
# Predicates that make up a Direct Container's membership config (reset wholesale).
_MEMBERSHIP_PREDICATES = (
    LDP.membershipResource,
    LDP.hasMemberRelation,
    LDP.insertedContentRelation,
)


@dataclass
class ModularMigrationReport:
    """What the modular-profile reconciliation changed (or would have)."""

    schemas_written: list[str] = field(default_factory=list)
    resource_definitions_written: list[str] = field(default_factory=list)
    resource_definitions_removed: list[str] = field(default_factory=list)
    root_retyped: str | None = None
    root_already_current: bool = False

    @property
    def changed(self) -> bool:
        return bool(
            self.schemas_written
            or self.resource_definitions_written
            or self.resource_definitions_removed
            or self.root_retyped
        )


async def migrate_to_modular_profile(
    profile: DeploymentProfile,
    *,
    repository: MetadataRepository,
    adapter: TripleStoreAdapter,
    settings: Settings,
    validator: ShaclValidator | None = None,
) -> ModularMigrationReport:
    """Reconcile an already-applied deployment to ``profile``'s modular schema set."""
    pre = validate_profile(profile)
    if not pre.ok:
        raise BadRequest(
            "profile failed structural validation",
            details={
                "issues": [
                    {"where": i.where, "code": i.code, "message": i.message} for i in pre.issues
                ]
            },
        )

    expander = IRIExpander(settings=settings)
    report = ModularMigrationReport()

    # 1. Schemas — overwrite each modular shape at its storage IRI, skip if identical.
    for loaded in profile.schemas:
        iri = expander.schema_storage_iri(loaded.entry.id)
        graph = expand_schema_refs(loaded.graph, expander.base_url)
        if await _identical(repository, iri, graph):
            continue
        await repository.put_graph(iri, graph, subject=None, initial_state=SEED_STATE)
        report.schemas_written.append(iri)
        if validator is not None:
            validator.invalidate(iri)
        log.info("modular_schema_written", iri=iri)
        # Mirror SchemaService.put (ADR-0019 §4): snapshot the just-written
        # shape at its immutable versioned IRI and (re)provision the 1:1
        # conformance profile so GET /fdp-api/profiles/{slug} points at the
        # CURRENT artifact. Without this, a reconciled deployment keeps
        # advertising the pre-change snapshot and the new versioned IRI never
        # dereferences. Version None (no meta yet — e.g. a bare test store)
        # skips the snapshot, exactly like SchemaService.put.
        slug = schema_slug(loaded.entry.id)
        version = await _schema_version(adapter, iri)
        if version is not None:
            snapshot = str(schema_version_graph_uri(expander.base_url, slug, str(version)))
            await adapter.replace_graph(
                snapshot, graph.serialize(format="nt"), mime="application/n-triples"
            )
            await provision_profile(adapter, base_url=expander.base_url, slug=slug, version=version)

    # 2. Resource definitions — rebuild from the new manifest, skip if identical,
    #    then drop any orphaned RD record (e.g. the renamed root: Repository →
    #    FAIRDataPoint changes the record slug). Leaving an orphan would put two
    #    root RDs in the store, and the startup cache rebuild
    #    (build_cache_from_repository) would see two empty-prefix definitions.
    if profile.manifest.resource_definitions:
        records = records_from_manifest(profile.manifest.resource_definitions, expander=expander)
        new_rd_iris: set[str] = set()
        for record in records:
            iri = str(
                resource_definition_graph_uri(
                    expander.base_url, rd_record_slug(record.url_prefix, record.name)
                )
            )
            new_rd_iris.add(iri)
            # Compare *parsed records*, not graphs: an RD record carries
            # xsd:string-typed literals that some stores canonicalize to plain
            # literals on round-trip, so a graph-isomorphism check would report a
            # spurious change on every run. Value equality of the dataclass is
            # both correct and stable (children are sorted deterministically).
            if await _rd_unchanged(repository, iri, record):
                continue
            await repository.put_graph(
                iri, record_to_graph(record, iri), subject=None, initial_state=SEED_STATE
            )
            report.resource_definitions_written.append(iri)
            log.info("modular_resource_definition_written", iri=iri)

        for existing_iri in await list_definition_iris(adapter):
            if existing_iri not in new_rd_iris:
                await repository.delete_graph(existing_iri)
                report.resource_definitions_removed.append(existing_iri)
                log.info("orphaned_resource_definition_removed", iri=existing_iri)

    # 3. Root record re-type (content-preserving).
    await _retype_root(profile, repository=repository, expander=expander, report=report)

    return report


async def _retype_root(
    profile: DeploymentProfile,
    *,
    repository: MetadataRepository,
    expander: IRIExpander,
    report: ModularMigrationReport,
) -> None:
    """Swap the root's domain type + reset its Direct Container config in place."""
    if not profile.manifest.resource_definitions:
        return
    cache = build_cache_from_manifest(profile.manifest.resource_definitions, expander=expander)
    root_rd = cache.root()
    root_entry = next((rd for rd in profile.manifest.resource_definitions if rd.is_root), None)
    if root_rd is None or root_entry is None:
        return

    repo_iri = expander.base_url
    new_class = URIRef(expander.schema_iri(root_entry.schema_id))
    new_types = {URIRef(t) for t in root_type_iris(str(new_class))}
    new_relations = [c.relation_uri for c in root_rd.children]

    graph = await repository.get_graph(repo_iri)
    if len(graph) == 0:
        return  # No root record to migrate.

    subject = URIRef(repo_iri)
    domain_types = {t for t in graph.objects(subject, RDF.type) if t not in _LDP_TYPES}
    current_relations = set(graph.objects(subject, LDP.hasMemberRelation))
    if domain_types == new_types and current_relations == {URIRef(r) for r in new_relations}:
        report.root_already_current = True
        return

    # Drop every old domain type (keep LDP interaction-model types — reset below).
    for old_type in domain_types:
        graph.remove((subject, RDF.type, old_type))
    # Reset the Direct Container config wholesale, then re-stamp from the new RD.
    graph.remove((subject, RDF.type, LDP.DirectContainer))
    graph.remove((subject, RDF.type, LDP.BasicContainer))
    graph.remove((subject, RDF.type, LDP.Container))
    for predicate in _MEMBERSHIP_PREDICATES:
        graph.remove((subject, predicate, None))
    for new_type in new_types:
        graph.add((subject, RDF.type, new_type))
    for triple in direct_container_config(subject, new_relations):
        graph.add(triple)

    await repository.put_graph(repo_iri, graph, subject=None)
    report.root_retyped = repo_iri
    log.info("root_record_retyped", root=repo_iri, new_class=str(new_class))


async def _schema_version(adapter: TripleStoreAdapter, iri: str) -> int | None:
    """The shape's current ``owl:versionInfo`` from its meta graph, or ``None``.

    Same contract as ``SchemaService._version`` — the meta writer bumps the
    version on every ``put_graph``, so right after a write this is the version
    the snapshot should be filed under.
    """
    body = await adapter.query(
        f"SELECT ?v WHERE {{ GRAPH <{meta_graph_uri(iri)}> {{ <{iri}> <{OWL.versionInfo}> ?v }} }}",
        accept="application/sparql-results+json",
    )
    payload = json.loads(body)
    for row in payload.get("results", {}).get("bindings", []):
        raw = row.get("v", {}).get("value")
        if raw is not None:
            try:
                return int(raw)
            except ValueError:
                return None
    return None


async def _identical(repository: MetadataRepository, iri: str, candidate: Graph) -> bool:
    """True iff the store already holds a graph isomorphic to ``candidate`` at ``iri``."""
    stored = await repository.get_graph(iri)
    if len(stored) == 0:
        return False
    return isomorphic(stored, candidate)


async def _rd_unchanged(repository: MetadataRepository, iri: str, record: object) -> bool:
    """True iff the RD record stored at ``iri`` already equals ``record`` by value."""
    stored = await repository.get_graph(iri)
    if len(stored) == 0:
        return False
    try:
        return record_from_graph(stored, iri) == record
    except ResourceDefinitionParseError:
        return False


__all__ = ["ModularMigrationReport", "migrate_to_modular_profile"]
