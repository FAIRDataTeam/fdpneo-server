"""Profile validation (architecture §12.2).

Six structural checks, run before any data hits the triple store:

1. Every SHACL shape graph parses (already done in the loader, but
   re-verified by running pySHACL against an empty data graph — a
   broken shape errors out at this point).
2. Every ODRL Offer conforms to the FDP profile (call
   :func:`fdp.policy.parser.parse_offer`).
3. Every seed record validates against the SHACL shape its ``kind``
   names.
4. Container references resolve (``parent`` and ``constrainedBy``
   point at declared ids).
5. The ``extends`` parent profile is ``null`` — cross-bundle imports
   are a v1.x increment.
6. Schema and offer ids are unique within the bundle.

Failures are collected as :class:`ValidationIssue` rows so the caller
sees every problem in one pass rather than fixing one and discovering
the next.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

import pyshacl  # type: ignore[import-untyped]
import structlog
from rdflib import Graph, URIRef
from rdflib.namespace import RDF

from fdp.policy.parser import parse_offer
from fdp.shared.errors import SchemaViolation
from fdp.shared.namespaces import ODRL

if TYPE_CHECKING:
    from fdp.metadata.profiles.manifest import DeploymentProfile

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ValidationIssue:
    """One problem detected in a profile bundle."""

    where: str
    """Item path within the manifest (``schemas[0]``, ``offers[1]`` …)."""

    code: str
    """Stable identifier for the error class — useful for tests."""

    message: str


@dataclass
class ValidationReport:
    """Outcome of :func:`validate_profile`."""

    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues

    def add(self, where: str, code: str, message: str) -> None:
        self.issues.append(ValidationIssue(where=where, code=code, message=message))


def validate_profile(profile: DeploymentProfile) -> ValidationReport:
    """Run every structural check on ``profile`` and return the report."""
    report = ValidationReport()

    if profile.manifest.extends is not None:
        report.add(
            where="extends",
            code="extends_unsupported",
            message="cross-bundle profile imports are not supported in v1",
        )

    _check_unique_ids(profile, report)
    _check_shapes_parse(profile, report)
    _check_offers_conform(profile, report)
    _check_containers_resolve(profile, report)
    _check_seed_records_validate(profile, report)

    return report


# --- check helpers ---------------------------------------------------------


def _check_unique_ids(profile: DeploymentProfile, report: ValidationReport) -> None:
    seen_schemas: set[str] = set()
    for i, schema in enumerate(profile.schemas):
        if schema.entry.id in seen_schemas:
            report.add(
                where=f"schemas[{i}]",
                code="duplicate_schema_id",
                message=f"duplicate schema id: {schema.entry.id}",
            )
        seen_schemas.add(schema.entry.id)

    seen_offers: set[str] = set()
    default_count = 0
    for i, offer in enumerate(profile.offers):
        if offer.entry.id in seen_offers:
            report.add(
                where=f"offers[{i}]",
                code="duplicate_offer_id",
                message=f"duplicate offer id: {offer.entry.id}",
            )
        seen_offers.add(offer.entry.id)
        if offer.entry.is_system_default:
            default_count += 1
    if default_count > 1:
        report.add(
            where="offers",
            code="multiple_system_default_offers",
            message="at most one offer may set isSystemDefault: true",
        )

    seen_containers: set[str] = set()
    for i, container in enumerate(profile.manifest.containers):
        if container.id in seen_containers:
            report.add(
                where=f"containers[{i}]",
                code="duplicate_container_id",
                message=f"duplicate container id: {container.id}",
            )
        seen_containers.add(container.id)


def _check_shapes_parse(profile: DeploymentProfile, report: ValidationReport) -> None:
    """Run pySHACL against each shape graph with an empty data graph.

    A shape that won't load through pySHACL fails here; a shape that
    loads cleanly but happens to reject every conceivable data graph is
    not a profile bug, so this only catches *parse* failures, not
    semantic mistakes.
    """
    for i, schema in enumerate(profile.schemas):
        try:
            _ = cast(
                tuple[bool, Graph, str],
                pyshacl.validate(  # pyright: ignore[reportUnknownMemberType]
                    data_graph=Graph(),
                    shacl_graph=schema.graph,
                    inference="none",
                    advanced=False,
                    meta_shacl=False,
                    inplace=False,
                ),
            )
        except Exception as err:
            report.add(
                where=f"schemas[{i}]",
                code="shape_parse_failed",
                message=f"SHACL shape {schema.entry.id} did not load: {err}",
            )

    if profile.meta_metadata_schema is not None:
        try:
            _ = cast(
                tuple[bool, Graph, str],
                pyshacl.validate(  # pyright: ignore[reportUnknownMemberType]
                    data_graph=Graph(),
                    shacl_graph=profile.meta_metadata_schema,
                    inference="none",
                    advanced=False,
                    meta_shacl=False,
                    inplace=False,
                ),
            )
        except Exception as err:
            report.add(
                where="metaMetadataSchema",
                code="shape_parse_failed",
                message=f"meta-metadata shape did not load: {err}",
            )


def _check_offers_conform(profile: DeploymentProfile, report: ValidationReport) -> None:
    """Each offer graph must contain exactly one odrl:Offer that parses cleanly."""
    for i, offer in enumerate(profile.offers):
        offer_subjects: list[URIRef] = []
        for subj in offer.graph.subjects(RDF.type, ODRL.Offer):
            if isinstance(subj, URIRef):
                offer_subjects.append(subj)
        if not offer_subjects:
            report.add(
                where=f"offers[{i}]",
                code="no_odrl_offer",
                message=f"file {offer.entry.path} declares no odrl:Offer",
            )
            continue
        if len(offer_subjects) > 1:
            report.add(
                where=f"offers[{i}]",
                code="multiple_odrl_offers",
                message=(
                    f"file {offer.entry.path} declares {len(offer_subjects)} "
                    "odrl:Offer instances; profile offers must contain exactly one"
                ),
            )
            continue
        try:
            parse_offer(offer.graph, offer_subjects[0])
        except SchemaViolation as err:
            report.add(
                where=f"offers[{i}]",
                code="offer_not_in_fdp_profile",
                message=err.message,
            )


def _check_containers_resolve(
    profile: DeploymentProfile, report: ValidationReport
) -> None:
    schema_ids = {s.entry.id for s in profile.schemas}
    container_ids = {c.id for c in profile.manifest.containers}

    for i, container in enumerate(profile.manifest.containers):
        if container.parent is not None and container.parent not in container_ids:
            report.add(
                where=f"containers[{i}]",
                code="container_parent_not_declared",
                message=(
                    f"container {container.id} references undeclared parent: "
                    f"{container.parent}"
                ),
            )
        if container.constrained_by is not None and container.constrained_by not in schema_ids:
            report.add(
                where=f"containers[{i}]",
                code="container_constrained_by_not_declared",
                message=(
                    f"container {container.id} is constrainedBy undeclared schema: "
                    f"{container.constrained_by}"
                ),
            )
        if container.type not in schema_ids:
            report.add(
                where=f"containers[{i}]",
                code="container_type_not_declared",
                message=(
                    f"container {container.id} declares type {container.type} which "
                    "is not declared as a schema"
                ),
            )


def _check_seed_records_validate(
    profile: DeploymentProfile, report: ValidationReport
) -> None:
    schemas_by_id = {s.entry.id: s for s in profile.schemas}
    for i, record in enumerate(profile.seed_records):
        schema = schemas_by_id.get(record.entry.kind)
        if schema is None:
            report.add(
                where=f"seedRecords[{i}]",
                code="seed_record_unknown_kind",
                message=(
                    f"seed record {record.entry.id} references undeclared schema kind: "
                    f"{record.entry.kind}"
                ),
            )
            continue
        try:
            conforms, _results, _txt = cast(
                tuple[bool, Graph, str],
                pyshacl.validate(  # pyright: ignore[reportUnknownMemberType]
                    data_graph=record.graph,
                    shacl_graph=schema.graph,
                    inference="none",
                    advanced=False,
                    meta_shacl=False,
                    inplace=False,
                ),
            )
        except Exception as err:
            report.add(
                where=f"seedRecords[{i}]",
                code="seed_record_validation_error",
                message=str(err),
            )
            continue
        if not conforms:
            report.add(
                where=f"seedRecords[{i}]",
                code="seed_record_does_not_conform",
                message=(
                    f"seed record {record.entry.id} does not conform to schema "
                    f"{record.entry.kind}"
                ),
            )


__all__ = ["ValidationIssue", "ValidationReport", "validate_profile"]
