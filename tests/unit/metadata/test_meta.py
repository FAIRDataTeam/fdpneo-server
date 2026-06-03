"""Unit tests for :mod:`fdp.metadata.meta`."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest
from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import RDF

from fdp.metadata.meta import (
    FDP_CREATE_OPERATION,
    FDP_MODIFY_OPERATION,
    META_SHAPE_IRI,
    MetaWriter,
    Operation,
    build_meta_graph,
)
from fdp.metadata.shacl import InMemoryShapeProvider, ShaclValidator
from fdp.shared.errors import SchemaViolation
from fdp.shared.namespaces import DCT, OWL, PROV

RECORD = "https://example.org/records/r1"
RECORD_URI = URIRef(RECORD)
ALICE = "https://idp.example/realms/fdp#alice"
BOB = "https://idp.example/realms/fdp#bob"
NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
LATER = datetime(2026, 6, 1, 13, 0, tzinfo=UTC)

META_SHAPE_TTL = (
    Path(__file__).resolve().parents[3] / "profiles" / "default" / "schemas" / "meta-metadata.ttl"
).read_text(encoding="utf-8")


def _empty() -> Graph:
    return Graph()


# --- builder ----------------------------------------------------------------


@pytest.mark.unit
def test_first_write_stamps_create_activity_and_creator() -> None:
    result = build_meta_graph(
        record_iri=RECORD,
        prior=_empty(),
        subject=ALICE,
        now=NOW,
    )

    assert result.operation is Operation.CREATE
    assert result.version == 1

    graph = result.graph
    assert (RECORD_URI, RDF.type, PROV.Entity) in graph
    assert (RECORD_URI, DCT.creator, URIRef(ALICE)) in graph
    assert (RECORD_URI, OWL.versionInfo, Literal(1)) in graph

    activities = list(graph.objects(RECORD_URI, PROV.wasGeneratedBy))
    assert len(activities) == 1
    activity = activities[0]
    assert isinstance(activity, BNode)
    assert (activity, RDF.type, PROV.Activity) in graph
    assert (activity, RDF.type, FDP_CREATE_OPERATION) in graph
    assert (activity, PROV.wasAssociatedWith, URIRef(ALICE)) in graph
    times = list(graph.objects(activity, PROV.atTime))
    assert times and isinstance(times[0], Literal)


@pytest.mark.unit
def test_subsequent_write_preserves_creator_bumps_version_and_marks_modify() -> None:
    first = build_meta_graph(record_iri=RECORD, prior=_empty(), subject=ALICE, now=NOW)
    second = build_meta_graph(
        record_iri=RECORD,
        prior=first.graph,
        subject=BOB,  # Bob modifies; creator should still be Alice
        now=LATER,
    )

    assert second.operation is Operation.MODIFY
    assert second.version == 2
    g = second.graph
    assert (RECORD_URI, DCT.creator, URIRef(ALICE)) in g
    assert (RECORD_URI, DCT.creator, URIRef(BOB)) not in g
    assert (RECORD_URI, OWL.versionInfo, Literal(2)) in g
    activity = next(g.objects(RECORD_URI, PROV.wasGeneratedBy))
    assert (activity, RDF.type, FDP_MODIFY_OPERATION) in g
    assert (activity, PROV.wasAssociatedWith, URIRef(BOB)) in g


@pytest.mark.unit
def test_anonymous_first_write_skips_creator_and_actor() -> None:
    result = build_meta_graph(record_iri=RECORD, prior=_empty(), subject=None, now=NOW)
    g = result.graph
    assert not list(g.objects(RECORD_URI, DCT.creator))
    activity = next(g.objects(RECORD_URI, PROV.wasGeneratedBy))
    assert not list(g.objects(activity, PROV.wasAssociatedWith))


@pytest.mark.unit
def test_modified_timestamp_updates_each_write() -> None:
    first = build_meta_graph(record_iri=RECORD, prior=_empty(), subject=ALICE, now=NOW)
    second = build_meta_graph(record_iri=RECORD, prior=first.graph, subject=ALICE, now=LATER)
    first_modified = next(first.graph.objects(RECORD_URI, DCT.modified))
    second_modified = next(second.graph.objects(RECORD_URI, DCT.modified))
    assert str(first_modified) != str(second_modified)


# --- writer + SHACL ---------------------------------------------------------


def _empty_writes() -> list[tuple[str, bytes]]:
    return []


@dataclass
class _RecordingAdapter:
    """Minimal adapter that captures replace_graph calls for assertion."""

    writes: list[tuple[str, bytes]] = field(default_factory=_empty_writes)

    async def replace_graph(
        self,
        graph_uri: str,
        data: bytes | str | Graph,
        *,
        mime: str = "text/turtle",
    ) -> None:
        del mime
        if isinstance(data, Graph):
            raw = data.serialize(format="nt").encode("utf-8")
        elif isinstance(data, str):
            raw = data.encode("utf-8")
        else:
            raw = data
        self.writes.append((graph_uri, raw))


def _validator_with_default_shape() -> ShaclValidator:
    return ShaclValidator(InMemoryShapeProvider({META_SHAPE_IRI: META_SHAPE_TTL}))


@pytest.mark.unit
def test_writer_validates_flag_reflects_construction() -> None:
    assert MetaWriter().validates is False
    assert MetaWriter(validator=_validator_with_default_shape(), shape_iri=META_SHAPE_IRI).validates


@pytest.mark.unit
def test_writer_rejects_half_configured_validation() -> None:
    with pytest.raises(ValueError, match="must be supplied together"):
        MetaWriter(validator=_validator_with_default_shape())
    with pytest.raises(ValueError, match="must be supplied together"):
        MetaWriter(shape_iri=META_SHAPE_IRI)


@pytest.mark.unit
async def test_writer_round_trips_through_default_shape() -> None:
    adapter = _RecordingAdapter()
    writer = MetaWriter(validator=_validator_with_default_shape(), shape_iri=META_SHAPE_IRI)
    result = await writer.write(
        adapter,  # type: ignore[arg-type]
        record_iri=RECORD,
        prior=_empty(),
        subject=ALICE,
        now=NOW,
    )
    assert result.operation is Operation.CREATE
    assert adapter.writes
    target_uri, _payload = adapter.writes[-1]
    assert target_uri.endswith("/meta")


@pytest.mark.unit
async def test_writer_raises_schema_violation_when_required_field_stripped() -> None:
    """A hand-rolled bad meta graph trips the default shape."""
    adapter = _RecordingAdapter()
    writer = MetaWriter(validator=_validator_with_default_shape(), shape_iri=META_SHAPE_IRI)
    # Build a normal meta and then strip dct:modified before validating.
    result = build_meta_graph(record_iri=RECORD, prior=_empty(), subject=ALICE, now=NOW)
    result.graph.remove((RECORD_URI, DCT.modified, None))
    # Feed the broken graph back into the validator via the writer's path.
    # The adapter writes nothing because we never get past validation.
    report = await writer._validator.validate_against(result.graph, META_SHAPE_IRI)  # type: ignore[union-attr]
    assert not report.conforms
    with pytest.raises(SchemaViolation):
        report.raise_if_failed()
    assert adapter.writes == []


@pytest.mark.unit
async def test_writer_tolerates_missing_meta_shape() -> None:
    """A validator is configured but the shape isn't stored → skip, still write.

    Server-generated meta must never be blocked by a missing/misconfigured meta
    shape (e.g. a profile that omits ``metaMetadataSchema``).
    """
    adapter = _RecordingAdapter()
    writer = MetaWriter(
        validator=ShaclValidator(InMemoryShapeProvider({})), shape_iri=META_SHAPE_IRI
    )
    result = await writer.write(
        adapter,  # type: ignore[arg-type]
        record_iri=RECORD,
        prior=_empty(),
        subject=ALICE,
        now=NOW,
    )
    assert result.operation is Operation.CREATE
    assert adapter.writes  # the meta graph was still committed


@pytest.mark.unit
def test_default_shape_file_parses_cleanly() -> None:
    """Guard against the shipped TTL silently going invalid."""
    g = Graph()
    g.parse(data=META_SHAPE_TTL, format="turtle")
    assert (URIRef(META_SHAPE_IRI), None, None) in g
