"""Deployment profile bootstrap (architecture §12).

Subpackages:

* :mod:`manifest` — Pydantic models for ``profile.yaml`` plus
  :func:`load_profile`, which reads the YAML and every referenced
  Turtle file into in-memory graphs.
* :mod:`validator` — checks shapes parse, Offers conform to the FDP
  profile, seed records pass SHACL, and container references resolve.
* :mod:`state` — Postgres-backed ``profile_applied`` marker.
* :mod:`applier` — executes the schema → container → offer → seed
  records pipeline, with compensation rollback on failure.

The CLI (``fdp profile validate|apply|info|export``) and the
auto-bootstrap path in :mod:`fdpneo_server.main` are the two production
entrypoints into this package.
"""

from __future__ import annotations

from fdpneo_server.metadata.profiles.applier import (
    ApplyError,
    ApplyReport,
    apply_profile,
    resolve_runtime_state,
)
from fdpneo_server.metadata.profiles.iri import IRIExpander
from fdpneo_server.metadata.profiles.manifest import (
    ChildLink,
    DeploymentProfile,
    OfferEntry,
    ProfileManifest,
    ProfileMetadata,
    ResourceDefinitionEntry,
    SchemaEntry,
    SeedRecord,
    bundled_default_profile,
    load_profile,
)
from fdpneo_server.metadata.profiles.rd_records import (
    RD_SHAPE_IRI,
    ChildLinkRecord,
    ResourceDefinitionParseError,
    ResourceDefinitionRecord,
    predefined_shape_graph,
    rd_record_slug,
    record_from_graph,
    record_to_graph,
)
from fdpneo_server.metadata.profiles.rd_service import (
    ResourceDefinitionService,
    build_cache_from_repository,
    list_definition_iris,
    load_definition_records,
)
from fdpneo_server.metadata.profiles.registry import (
    ChildLinkInfo,
    ResourceDefinition,
    ResourceDefinitionCache,
    build_cache_from_manifest,
    records_from_manifest,
    resolve_cache,
)
from fdpneo_server.metadata.profiles.state import (
    ProfileAppliedRow,
    ProfileStateRepository,
)
from fdpneo_server.metadata.profiles.validator import (
    ValidationIssue,
    ValidationReport,
    validate_profile,
)

__all__ = [
    "RD_SHAPE_IRI",
    "ApplyError",
    "ApplyReport",
    "ChildLink",
    "ChildLinkInfo",
    "ChildLinkRecord",
    "DeploymentProfile",
    "IRIExpander",
    "OfferEntry",
    "ProfileAppliedRow",
    "ProfileManifest",
    "ProfileMetadata",
    "ProfileStateRepository",
    "ResourceDefinition",
    "ResourceDefinitionCache",
    "ResourceDefinitionEntry",
    "ResourceDefinitionParseError",
    "ResourceDefinitionRecord",
    "ResourceDefinitionService",
    "SchemaEntry",
    "SeedRecord",
    "ValidationIssue",
    "ValidationReport",
    "apply_profile",
    "build_cache_from_manifest",
    "build_cache_from_repository",
    "bundled_default_profile",
    "list_definition_iris",
    "load_definition_records",
    "load_profile",
    "predefined_shape_graph",
    "rd_record_slug",
    "record_from_graph",
    "record_to_graph",
    "records_from_manifest",
    "resolve_cache",
    "resolve_runtime_state",
    "validate_profile",
]
