"""Meta-metadata graph construction + validation.

Each record has a sibling ``<record>/meta`` graph that carries provenance
(creator, created/modified timestamps, version) and a PROV ``Activity``
describing the most recent operation (architecture §6.2). The default
schema lives at ``profiles/default/schemas/meta-metadata.ttl`` and is
validated by ``ShaclValidator`` against :data:`META_SHAPE_IRI`.

This module owns two things:

* :func:`build_meta_graph` — pure builder. Reads the prior meta graph,
  determines whether this is a CREATE or MODIFY, and returns a fresh
  :class:`Graph` reflecting the new state. The latest Activity replaces
  any prior Activity in the meta graph; the audit graph (out of scope
  here) is where historical Activities accumulate.
* :class:`MetaWriter` — orchestrates ``build → validate → replace_graph``.
  Validation is opt-in by passing a :class:`ShaclValidator` and the shape
  IRI; without those (the default), the writer just builds and replaces.
  Bootstrap (5.2) and unit tests use the no-validation form; runtime
  composition wires the validator + default shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

import structlog
from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import RDF

from fdp.metadata.graphs import meta_graph_uri, record_graph_uri
from fdp.metadata.states import DEFAULT_STATE, MetadataState
from fdp.shared.namespaces import DCT, FDP_DEFAULT, FDP_METADATA_STATE, OWL, PROV

if TYPE_CHECKING:
    from fdp.metadata.shacl import ShaclValidator
    from fdp.storage.triplestore import TripleStoreAdapter

log = structlog.get_logger(__name__)


META_SHAPE_IRI = "https://w3id.org/fdp/o#MetaMetadataShape"
"""IRI of the default meta-metadata SHACL shape."""

FDP_CREATE_OPERATION = FDP_DEFAULT["CreateOperation"]
FDP_MODIFY_OPERATION = FDP_DEFAULT["ModifyOperation"]


class Operation(StrEnum):
    """The operation a meta graph refresh records."""

    CREATE = "create"
    MODIFY = "modify"


@dataclass(frozen=True)
class MetaResult:
    """Output of :func:`build_meta_graph`."""

    graph: Graph
    operation: Operation
    version: int
    state: MetadataState


def build_meta_graph(
    *,
    record_iri: str | URIRef,
    prior: Graph,
    subject: str | None,
    now: datetime,
    initial_state: MetadataState = DEFAULT_STATE,
) -> MetaResult:
    """Build the next meta graph for ``record_iri``.

    Args:
        record_iri: The record being written. Used as the meta-graph subject.
        prior: The current meta graph for the record (empty graph on first write).
        subject: The current acting principal's URI; ``None`` if anonymous.
        now: The write timestamp; supplied by the caller for determinism.
        initial_state: Publication state for a *new* record (ADR-0010). LDP
            creates default to ``DRAFT``; the profile applier passes
            ``PUBLISHED`` for seeded records. On MODIFY the prior state is
            preserved and this argument is ignored — a content edit never
            changes publication state; only the transition API does.

    Returns:
        A :class:`MetaResult` whose ``graph`` is ready to replace whatever
        the triple store currently holds at ``<record>/meta``.
    """
    record_subject = record_graph_uri(record_iri)
    version = _next_version(prior, record_subject)
    created_at, prior_creator = _extract_creation(prior, record_subject)

    is_creation = created_at is None
    operation = Operation.CREATE if is_creation else Operation.MODIFY
    effective_created = created_at or now
    effective_creator = prior_creator if not is_creation else subject
    # State is server-managed lifecycle metadata: set it on create, preserve
    # it across content edits. Only the transition API (lifecycle.py) changes
    # an existing record's state.
    prior_state = _extract_state(prior, record_subject)
    effective_state = initial_state if is_creation else (prior_state or initial_state)

    graph = Graph()
    graph.add((record_subject, RDF.type, PROV.Entity))
    graph.add((record_subject, DCT.created, Literal(effective_created)))
    if effective_creator is not None:
        graph.add((record_subject, DCT.creator, URIRef(effective_creator)))
    graph.add((record_subject, DCT.modified, Literal(now)))
    graph.add((record_subject, OWL.versionInfo, Literal(version)))
    graph.add((record_subject, FDP_METADATA_STATE, Literal(effective_state.value)))

    activity = BNode()
    graph.add((record_subject, PROV.wasGeneratedBy, activity))
    graph.add((activity, RDF.type, PROV.Activity))
    graph.add(
        (
            activity,
            RDF.type,
            FDP_CREATE_OPERATION if is_creation else FDP_MODIFY_OPERATION,
        )
    )
    graph.add((activity, PROV.atTime, Literal(now)))
    if subject is not None:
        graph.add((activity, PROV.wasAssociatedWith, URIRef(subject)))

    return MetaResult(graph=graph, operation=operation, version=version, state=effective_state)


class MetaWriter:
    """Build + (optionally) SHACL-validate + commit the meta graph."""

    def __init__(
        self,
        *,
        validator: ShaclValidator | None = None,
        shape_iri: str | None = None,
    ) -> None:
        if (validator is None) != (shape_iri is None):
            raise ValueError("validator and shape_iri must be supplied together")
        self._validator = validator
        self._shape_iri = shape_iri

    @property
    def validates(self) -> bool:
        """True iff this writer SHACL-validates each meta graph it writes."""
        return self._validator is not None and self._shape_iri is not None

    async def write(
        self,
        adapter: TripleStoreAdapter,
        *,
        record_iri: str | URIRef,
        prior: Graph,
        subject: str | None,
        now: datetime,
        initial_state: MetadataState = DEFAULT_STATE,
    ) -> MetaResult:
        """Run the full build → validate → commit cycle."""
        result = build_meta_graph(
            record_iri=record_iri,
            prior=prior,
            subject=subject,
            now=now,
            initial_state=initial_state,
        )
        if self._validator is not None and self._shape_iri is not None:
            report = await self._validator.validate_against(result.graph, self._shape_iri)
            if not report.conforms:
                log.error(
                    "meta_metadata_schema_violation",
                    record=str(record_iri),
                    shape=self._shape_iri,
                    violations=len(report.violations),
                )
            report.raise_if_failed()
        nt = result.graph.serialize(format="nt")
        await adapter.replace_graph(
            str(meta_graph_uri(record_iri)),
            nt,
            mime="application/n-triples",
        )
        return result


# --- private helpers --------------------------------------------------------


def _next_version(prior: Graph, record_subject: URIRef) -> int:
    for value in prior.objects(record_subject, OWL.versionInfo):
        if isinstance(value, Literal):
            try:
                return int(str(value)) + 1
            except ValueError:
                return 1
    return 1


def _extract_creation(
    prior: Graph,
    record_subject: URIRef,
) -> tuple[datetime | None, str | None]:
    created: datetime | None = None
    for value in prior.objects(record_subject, DCT.created):
        if isinstance(value, Literal):
            raw = value.toPython()
            if isinstance(raw, datetime):
                created = raw
            else:
                try:
                    created = datetime.fromisoformat(str(value))
                except ValueError:
                    created = None
        break
    creator: str | None = None
    for value in prior.objects(record_subject, DCT.creator):
        if isinstance(value, URIRef):
            creator = str(value)
            break
    return created, creator


def _extract_state(prior: Graph, record_subject: URIRef) -> MetadataState | None:
    """Return the prior publication state, or ``None`` if not present/invalid."""
    for value in prior.objects(record_subject, FDP_METADATA_STATE):
        try:
            return MetadataState(str(value))
        except ValueError:
            return None
    return None


__all__ = [
    "FDP_CREATE_OPERATION",
    "FDP_MODIFY_OPERATION",
    "META_SHAPE_IRI",
    "MetaResult",
    "MetaWriter",
    "Operation",
    "build_meta_graph",
]
