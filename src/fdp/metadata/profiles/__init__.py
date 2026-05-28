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
auto-bootstrap path in :mod:`fdp.main` are the two production
entrypoints into this package.
"""

from __future__ import annotations

from fdp.metadata.profiles.applier import ApplyError, ApplyReport, apply_profile
from fdp.metadata.profiles.iri import IRIExpander
from fdp.metadata.profiles.manifest import (
    ChildLink,
    DeploymentProfile,
    OfferEntry,
    ProfileManifest,
    ProfileMetadata,
    ResourceDefinitionEntry,
    SchemaEntry,
    SeedRecord,
    load_profile,
)
from fdp.metadata.profiles.registry import (
    ChildLinkInfo,
    ResourceDefinition,
    ResourceDefinitionCache,
    build_cache_from_manifest,
)
from fdp.metadata.profiles.state import (
    ProfileAppliedRow,
    ProfileStateRepository,
)
from fdp.metadata.profiles.validator import (
    ValidationIssue,
    ValidationReport,
    validate_profile,
)

__all__ = [
    "ApplyError",
    "ApplyReport",
    "ChildLink",
    "ChildLinkInfo",
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
    "SchemaEntry",
    "SeedRecord",
    "ValidationIssue",
    "ValidationReport",
    "apply_profile",
    "build_cache_from_manifest",
    "load_profile",
    "validate_profile",
]
