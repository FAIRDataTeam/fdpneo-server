"""Pure record → search-fields extraction (Phase 7.1).

No I/O: given a record's graph and its meta graph, produce the flat
:class:`ExtractedRecord` the indexer hands to the repository. Kept pure so it is
unit-testable without a database or triple store.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from rdflib import Literal, URIRef
from rdflib.namespace import RDF

from fdp.metadata.states import MetadataState
from fdp.shared.graphs import (
    is_internal_graph_uri,
    is_license_graph_uri,
    is_policy_graph_uri,
)
from fdp.shared.namespaces import (
    DCAT,
    DCT,
    FDP_METADATA_STATE,
    FDP_RESOURCE_DEFINITION,
    LDP,
    ODRL,
    PROV,
    SH,
)

if TYPE_CHECKING:
    from rdflib import Graph

# rdf:type values that mark a record as FDP machinery rather than indexable
# content. Such records (schemas, offers, resource definitions, the LDP
# container/meta scaffolding) are skipped by the indexer and the reindex walk.
_NON_CONTENT_TYPES = frozenset({SH.NodeShape, ODRL.Offer, FDP_RESOURCE_DEFINITION, PROV.Entity})
# Namespaces whose types are structural, never the record's "primary" type.
_STRUCTURAL_TYPE_PREFIXES = (str(LDP), str(PROV))


@dataclass(frozen=True)
class ExtractedRecord:
    """Flat search fields pulled from a record + its meta graph."""

    record_iri: str
    type_iri: str | None
    title: str | None
    description: str | None
    license: str | None
    keywords: str | None
    state: MetadataState | None
    updated_at: datetime | None

    @property
    def search_source(self) -> str:
        """The text fed to ``to_tsvector`` — title, description, keywords."""
        return " ".join(p for p in (self.title, self.description, self.keywords) if p)


def is_indexable(record_iri: str, record_graph: Graph) -> bool:
    """True iff ``record_iri`` is indexable content (not internal/config).

    Excludes internal graphs (meta/audit/resource-definitions) and config
    records (SHACL shapes, *un-managed* ODRL offers) by their ``rdf:type``.

    Managed policy and license documents (ADR-0012) are first-class, searchable
    reference content and are indexed despite a policy being typed
    ``odrl:Offer`` — only their internal ``/meta`` and ``/audit`` siblings
    (excluded by the internal-graph check above) are skipped.
    """
    if is_internal_graph_uri(record_iri):
        return False
    if is_policy_graph_uri(record_iri) or is_license_graph_uri(record_iri):
        return True
    subject = URIRef(record_iri)
    types = set(record_graph.objects(subject, RDF.type))
    return not (types & _NON_CONTENT_TYPES)


def extract(record_iri: str, record_graph: Graph, meta_graph: Graph) -> ExtractedRecord:
    """Build an :class:`ExtractedRecord` from a record graph + its meta graph."""
    subject = URIRef(record_iri)
    return ExtractedRecord(
        record_iri=record_iri,
        type_iri=_primary_type(record_graph, subject),
        title=_first_literal(record_graph, subject, DCT.title),
        description=_first_literal(record_graph, subject, DCT.description),
        license=_first_str(record_graph, subject, DCT.license),
        keywords=_keywords(record_graph, subject),
        state=_state(meta_graph, subject),
        updated_at=_modified(meta_graph, subject),
    )


# --- helpers ---------------------------------------------------------------


def _primary_type(graph: Graph, subject: URIRef) -> str | None:
    """The record's first non-structural ``rdf:type`` (e.g. dcat:Catalog)."""
    for obj in graph.objects(subject, RDF.type):
        if not isinstance(obj, URIRef):
            continue
        text = str(obj)
        if any(text.startswith(prefix) for prefix in _STRUCTURAL_TYPE_PREFIXES):
            continue
        return text
    return None


def _first_literal(graph: Graph, subject: URIRef, predicate: URIRef) -> str | None:
    for obj in graph.objects(subject, predicate):
        if isinstance(obj, Literal):
            return str(obj)
    return None


def _first_str(graph: Graph, subject: URIRef, predicate: URIRef) -> str | None:
    for obj in graph.objects(subject, predicate):
        return str(obj)
    return None


def _keywords(graph: Graph, subject: URIRef) -> str | None:
    values: list[str] = []
    for predicate in (DCAT.keyword, DCT.subject):
        for obj in graph.objects(subject, predicate):
            if isinstance(obj, Literal):
                values.append(str(obj))
    return " ".join(values) if values else None


def _state(meta_graph: Graph, subject: URIRef) -> MetadataState | None:
    for obj in meta_graph.objects(subject, FDP_METADATA_STATE):
        try:
            return MetadataState(str(obj))
        except ValueError:
            return None
    return None


def _modified(meta_graph: Graph, subject: URIRef) -> datetime | None:
    for obj in meta_graph.objects(subject, DCT.modified):
        if isinstance(obj, Literal):
            raw = obj.toPython()
            if isinstance(raw, datetime):
                return raw
            try:
                return datetime.fromisoformat(str(obj))
            except ValueError:
                return None
    return None


__all__ = ["ExtractedRecord", "extract", "is_indexable"]
