"""One-time FDP vocabulary migration (v0.16.0, ADR-0026).

Releases before 0.16 minted every FDP vocabulary term under
``https://w3id.org/fdp/o#`` — a typo for the published FDP Ontology namespace
``https://w3id.org/fdp/fdp-o#`` (the old IRI was never registered on w3id.org
and 404s). The wrong IRIs are persisted everywhere a record graph mentions the
vocabulary: the root's ``rdf:type``, membership relations, every record's
``/meta`` sibling (``metadataState``, ``validatedAgainst``, the prov activity
types), resource-definition machinery records, schema graphs and their
immutable version snapshots — and four graphs are *named* under it (the
server-owned SHACL shapes, which move to ``urn:fdp-shape:*`` because they are
stored artifacts, not vocabulary).

This module rewrites all of that in place. It runs automatically at startup
(idempotent — a store with no old-namespace term is a no-op) and is exposed as
``fdp vocab migrate [--dry-run]`` for inspection. After a run that changed
anything the caller must invalidate the authz cache (cached rows reference the
old shape graph URIs) and rebuild the search index (``metadata_search.type_iri``
holds rewritten class IRIs); the startup hook in ``main.py`` does both.

Only server-owned constants are interpolated into the SPARQL below — the same
controlled-input pattern as ``metadata/pid/rebase.py``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog
from rdflib import RDF, Graph, URIRef

from fdpneo_server.shared.namespaces import (
    FDP_FAIRDATAPOINT,
    FDP_LEGACY,
    FDP_METADATA_SERVICE,
)
from fdpneo_server.storage.triplestore.adapter import construct_named_graph

if TYPE_CHECKING:
    from fdpneo_server.storage.triplestore.adapter import TripleStoreAdapter

__all__ = ["VocabMigrationReport", "map_iri", "migrate_vocabulary"]

log = structlog.get_logger(__name__)

_OLD = str(FDP_LEGACY)
_NEW = "https://w3id.org/fdp/fdp-o#"

# The four server-owned SHACL shapes were *named* under the old namespace.
# They are stored artifacts, not vocabulary, so they move to ``urn:fdp-shape:``
# (the values mirror META_SHAPE_IRI / LICENSE_SHAPE_IRI / RD_SHAPE_IRI and the
# child-link shape IRI; a unit test pins the correspondence to avoid importing
# those modules here).
_SHAPE_LOCALS: dict[str, str] = {
    "MetaMetadataShape": "urn:fdp-shape:meta-metadata",
    "LicenseDocumentShape": "urn:fdp-shape:license-document",
    "ResourceDefinitionShape": "urn:fdp-shape:resource-definition",
    "ChildLinkShape": "urn:fdp-shape:child-link",
}


def map_iri(value: str) -> str | None:
    """Map one old-namespace IRI to its 0.16 home, or ``None`` if unaffected.

    Shape IRIs (and their ``/meta`` siblings) map to ``urn:fdp-shape:*``;
    every other local name moves verbatim to the FDP Ontology namespace.
    """
    if not value.startswith(_OLD):
        return None
    local = value[len(_OLD) :]
    for shape_local, urn in _SHAPE_LOCALS.items():
        if local == shape_local:
            return urn
        if local.startswith(shape_local + "/"):
            return urn + local[len(shape_local) :]
    return _NEW + local


def _map_term(term: object) -> object:
    if isinstance(term, URIRef):
        mapped = map_iri(str(term))
        if mapped is not None:
            return URIRef(mapped)
    return term


def _rewrite(graph: Graph) -> Graph:
    out = Graph()
    for s, p, o in graph:
        out.add(
            (
                _map_term(s),  # type: ignore[arg-type]
                _map_term(p),  # type: ignore[arg-type]
                _map_term(o),  # type: ignore[arg-type]
            )
        )
    return out


@dataclass
class VocabMigrationReport:
    """Summary of a vocabulary-migration run."""

    dry_run: bool
    rewritten: list[str] = field(default_factory=list)
    """Graphs whose *content* was rewritten in place."""
    renamed: list[tuple[str, str]] = field(default_factory=list)
    """(old, new) pairs for graphs whose *name* moved (the shape graphs)."""
    root_backfilled: bool = False
    """Whether ``fdp-o:MetadataService`` was added to the root record."""

    @property
    def changed(self) -> bool:
        return bool(self.rewritten or self.renamed or self.root_backfilled)


async def _affected_graphs(adapter: TripleStoreAdapter) -> list[str]:
    query = (
        "SELECT DISTINCT ?g WHERE { GRAPH ?g { ?s ?p ?o }\n"
        f'  FILTER( STRSTARTS(STR(?g), "{_OLD}")\n'
        f'    || STRSTARTS(STR(?s), "{_OLD}")\n'
        f'    || STRSTARTS(STR(?p), "{_OLD}")\n'
        f'    || (isIRI(?o) && STRSTARTS(STR(?o), "{_OLD}")) ) }}'
    )
    body = await adapter.query(query)
    bindings = json.loads(body).get("results", {}).get("bindings", [])
    return [b["g"]["value"] for b in bindings if "g" in b]


async def migrate_vocabulary(
    *,
    adapter: TripleStoreAdapter,
    root_iri: str | None = None,
    dry_run: bool = False,
) -> VocabMigrationReport:
    """Rewrite every stored old-namespace term; rename the shape graphs.

    Idempotent: a second pass finds no old-namespace term and does nothing.
    When ``root_iri`` is given, the root record is additionally backfilled
    with ``fdp-o:MetadataService`` next to ``fdp-o:FAIRDataPoint`` (FDP Index
    validators match ``MetadataService`` literally — see ``root_type_iris``).
    """
    report = VocabMigrationReport(dry_run=dry_run)

    for graph_uri in await _affected_graphs(adapter):
        target = map_iri(graph_uri) or graph_uri
        if target != graph_uri:
            report.renamed.append((graph_uri, target))
        else:
            report.rewritten.append(graph_uri)
        if dry_run:
            continue
        rewritten = _rewrite(await construct_named_graph(adapter, graph_uri))
        await adapter.replace_graph(
            target, rewritten.serialize(format="nt"), mime="application/n-triples"
        )
        if target != graph_uri:
            await adapter.drop_graph(graph_uri)

    if root_iri is not None:
        report.root_backfilled = await _backfill_root_types(
            adapter, root_iri.rstrip("/"), dry_run=dry_run
        )

    log.info(
        "vocab_migration_completed" if report.changed else "vocab_migration_noop",
        rewritten=len(report.rewritten),
        renamed=len(report.renamed),
        root_backfilled=report.root_backfilled,
        dry_run=dry_run,
    )
    return report


async def _backfill_root_types(
    adapter: TripleStoreAdapter, root_iri: str, *, dry_run: bool
) -> bool:
    """Add ``fdp-o:MetadataService`` to a FAIRDataPoint root that lacks it."""
    graph = await construct_named_graph(adapter, root_iri)
    subject = URIRef(root_iri)
    if (subject, RDF.type, FDP_FAIRDATAPOINT) not in graph:
        return False
    if (subject, RDF.type, FDP_METADATA_SERVICE) in graph:
        return False
    if not dry_run:
        graph.add((subject, RDF.type, FDP_METADATA_SERVICE))
        await adapter.replace_graph(
            root_iri, graph.serialize(format="nt"), mime="application/n-triples"
        )
    return True
