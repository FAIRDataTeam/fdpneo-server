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
from fdp.metadata.profiles.manifest import (
    ContainerEntry,
    DeploymentProfile,
    OfferEntry,
    ProfileManifest,
    ProfileMetadata,
    SchemaEntry,
    SeedRecord,
    load_profile,
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
    "ContainerEntry",
    "DeploymentProfile",
    "OfferEntry",
    "ProfileAppliedRow",
    "ProfileManifest",
    "ProfileMetadata",
    "ProfileStateRepository",
    "SchemaEntry",
    "SeedRecord",
    "ValidationIssue",
    "ValidationReport",
    "apply_profile",
    "load_profile",
    "validate_profile",
]
