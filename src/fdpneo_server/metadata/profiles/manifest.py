"""Manifest parsing for ``profile.yaml`` (architecture §12.1).

The Pydantic models here are a one-to-one mapping of the YAML
structure: ``metadata``, ``schemas``, ``offers``, ``containers``,
``seedRecords``, plus the optional meta-metadata schema. The
:func:`load_profile` entry point reads the YAML, resolves every
``path`` against the bundle root, and returns a
:class:`DeploymentProfile` whose Turtle payloads are pre-loaded into
``rdflib.Graph`` instances.

This module performs *no* validation beyond YAML/Pydantic structural
checks and file existence — semantic checks (SHACL shapes parse, ODRL
Offers conform, references resolve) live in :mod:`.validator`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field
from rdflib import Graph

from fdpneo_server.shared.errors import BadRequest

# --- manifest models -------------------------------------------------------


class _StrictBase(BaseModel):
    """Pydantic base that rejects unknown keys.

    Profile YAML typos otherwise silently no-op; a strict model forces
    the bundle author to fix them.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ProfileMetadata(_StrictBase):
    """The ``metadata`` block on the manifest."""

    name: str
    version: str
    description: str | None = None
    authors: list[dict[str, str]] = Field(default_factory=list)
    homepage: str | None = None


class SchemaEntry(_StrictBase):
    """One SHACL schema declared by the profile."""

    id: str
    path: str


class MetaMetadataSchemaEntry(_StrictBase):
    """The single meta-metadata SHACL shape; identified by IRI in code, not the manifest."""

    path: str


class OfferEntry(_StrictBase):
    """One ODRL Offer template the profile seeds."""

    id: str
    path: str
    is_system_default: bool = Field(default=False, alias="isSystemDefault")


class ChildLink(_StrictBase):
    """A typed link from one resource definition to another.

    Mirrors :class:`ResourceDefinitionChild` in the reference
    implementation. The link's ``relation_uri`` is the predicate the
    parent uses to point at members of the target type — for example
    ``dcat:dataset`` links a Catalog to its Datasets.
    """

    relation_uri: str = Field(alias="relationUri")
    target: str
    """``url_prefix`` of the target :class:`ResourceDefinitionEntry`."""
    title: str = ""
    """Human label rendered in the per-type listing endpoint
    (``/.../page/<target>``)."""
    tags_uri: str | None = Field(default=None, alias="tagsUri")


class ResourceDefinitionEntry(_StrictBase):
    """One metadata *type* this deployment exposes.

    Carries the URL prefix (route segment), the SHACL schema for
    instances, and the typed child links the OpenAPI generator turns
    into per-type paths. Exactly one entry must declare ``urlPrefix:
    ""`` — that one is the FDP Repository at the API root.
    """

    url_prefix: str = Field(alias="urlPrefix")
    name: str
    schema_id: str = Field(alias="schema")
    children: list[ChildLink] = Field(default_factory=list)

    @property
    def is_root(self) -> bool:
        return self.url_prefix == ""


class SeedRecord(_StrictBase):
    """One pre-populated record (a Turtle file rooted at an IRI)."""

    id: str
    path: str
    kind: str
    """The ``id`` of the schema this record must satisfy."""


class ProfileManifest(_StrictBase):
    """Top-level model for ``profile.yaml``."""

    api_version: Literal["fdp/v1"] = Field(alias="apiVersion")
    kind: Literal["DeploymentProfile"]
    metadata: ProfileMetadata
    extends: str | None = None
    schemas: list[SchemaEntry] = Field(default_factory=list)
    meta_metadata_schema: MetaMetadataSchemaEntry | None = Field(
        default=None, alias="metaMetadataSchema"
    )
    offers: list[OfferEntry] = Field(default_factory=list)
    resource_definitions: list[ResourceDefinitionEntry] = Field(
        default_factory=list, alias="resourceDefinitions"
    )
    seed_records: list[SeedRecord] = Field(default_factory=list, alias="seedRecords")


# --- loaded profile (manifest + Turtle payloads) ---------------------------


@dataclass(frozen=True)
class LoadedSchema:
    entry: SchemaEntry
    graph: Graph


@dataclass(frozen=True)
class LoadedOffer:
    entry: OfferEntry
    graph: Graph


@dataclass(frozen=True)
class LoadedSeedRecord:
    entry: SeedRecord
    graph: Graph


@dataclass(frozen=True)
class DeploymentProfile:
    """A profile bundle with every Turtle file parsed into memory."""

    manifest: ProfileManifest
    bundle_root: Path
    schemas: tuple[LoadedSchema, ...]
    meta_metadata_schema: Graph | None
    offers: tuple[LoadedOffer, ...]
    seed_records: tuple[LoadedSeedRecord, ...]
    manifest_checksum: str
    """SHA-256 of the canonicalized manifest payload."""

    @property
    def name(self) -> str:
        return self.manifest.metadata.name

    @property
    def version(self) -> str:
        return self.manifest.metadata.version


# --- loader ---------------------------------------------------------------


_MANIFEST_FILENAME = "profile.yaml"


def bundled_default_profile() -> Path:
    """Location of the default DCAT profile bundle shipped inside the package.

    ``fdp profile apply`` and the auto-bootstrap fall back to this bundle
    when no path is configured, so an installed wheel can bootstrap itself
    without a source checkout.
    """
    from importlib.resources import files

    return Path(str(files("fdpneo_server") / "profiles" / "default"))


def load_profile(bundle_root: Path | str) -> DeploymentProfile:
    """Read ``<bundle_root>/profile.yaml`` and load every referenced Turtle file.

    Raises :class:`BadRequest` (the ``fdp.bad_request`` code) on any
    structural problem: missing manifest, malformed YAML, unknown keys,
    or a referenced file that is not present on disk. Semantic checks
    (SHACL parses, Offer conformance, etc.) belong in
    :mod:`.validator`.
    """
    root = Path(bundle_root)
    manifest_path = root / _MANIFEST_FILENAME
    if not manifest_path.is_file():
        raise BadRequest(
            f"profile manifest not found: {manifest_path}",
            details={"bundle_root": str(root)},
        )

    raw_text = manifest_path.read_text(encoding="utf-8")
    try:
        raw_obj = yaml.safe_load(raw_text)
    except yaml.YAMLError as err:
        raise BadRequest(
            f"profile.yaml is not valid YAML: {err}",
            details={"bundle_root": str(root)},
        ) from err

    if not isinstance(raw_obj, dict):
        raise BadRequest(
            "profile.yaml must be a mapping at the top level",
            details={"bundle_root": str(root)},
        )

    try:
        manifest = ProfileManifest.model_validate(raw_obj)
    except Exception as err:  # pydantic.ValidationError
        raise BadRequest(
            f"profile.yaml failed structural validation: {err}",
            details={"bundle_root": str(root)},
        ) from err

    schemas = tuple(
        LoadedSchema(entry=entry, graph=_load_turtle(root / entry.path))
        for entry in manifest.schemas
    )
    meta_schema_graph: Graph | None = None
    if manifest.meta_metadata_schema is not None:
        meta_schema_graph = _load_turtle(root / manifest.meta_metadata_schema.path)
    offers = tuple(
        LoadedOffer(entry=entry, graph=_load_turtle(root / entry.path)) for entry in manifest.offers
    )
    seed_records = tuple(
        LoadedSeedRecord(entry=entry, graph=_load_turtle(root / entry.path))
        for entry in manifest.seed_records
    )

    checksum = _manifest_checksum(manifest)
    return DeploymentProfile(
        manifest=manifest,
        bundle_root=root,
        schemas=schemas,
        meta_metadata_schema=meta_schema_graph,
        offers=offers,
        seed_records=seed_records,
        manifest_checksum=checksum,
    )


def _load_turtle(path: Path) -> Graph:
    if not path.is_file():
        raise BadRequest(
            f"profile references missing file: {path}",
            details={"path": str(path)},
        )
    graph = Graph()
    text = path.read_text(encoding="utf-8")
    graph.parse(data=text, format="turtle")
    return graph


def _manifest_checksum(manifest: ProfileManifest) -> str:
    """SHA-256 over a stable JSON serialization of the manifest model."""
    payload = manifest.model_dump(by_alias=True, mode="json")
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = [
    "ChildLink",
    "DeploymentProfile",
    "LoadedOffer",
    "LoadedSchema",
    "LoadedSeedRecord",
    "MetaMetadataSchemaEntry",
    "OfferEntry",
    "ProfileManifest",
    "ProfileMetadata",
    "ResourceDefinitionEntry",
    "SchemaEntry",
    "SeedRecord",
    "load_profile",
]
