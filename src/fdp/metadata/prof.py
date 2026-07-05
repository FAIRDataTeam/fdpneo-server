"""PROF conformance profiles (ADR-0019) — the self-describing validation binding.

A metadata record is self-describing at rest via ``dct:conformsTo`` → a
``prof:Profile``. In v1 a profile is the **1:1 wrapper of a SHACL schema** (same
slug): ``…/fdp-api/profiles/{slug}`` wraps ``…/fdp-api/schemas/{slug}``, with a
single ``prof:hasResource`` whose ``prof:hasRole`` is ``role:validation`` and
whose ``prof:hasArtifact`` points at an **immutable schema version snapshot**
(ADR-0019 §4). The resource definition keeps ``ldp:constrainedBy`` on the schema;
the profile is *derived* from it, so records reach the schema through the profile
without any RD churn (ADR-0019 §2, as amended).

Profiles are **provisioned and maintained from schemas** (never edited directly),
so the API surface is read-only:

* ``GET /profiles``               — list current profiles (public).
* ``GET /profiles/{id}``          — the current profile as Turtle (public).
* ``GET /profiles/{id}/{version}``— an immutable versioned snapshot (public).

:func:`provision_profile` is the single writer, called by the schema service on
every publish (and by the migration/bootstrap backfill).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Final

import structlog
from fastapi import APIRouter, Response
from pydantic import BaseModel
from rdflib import BNode, Graph, URIRef
from rdflib.namespace import RDF

from fdp.shared.errors import NotFound
from fdp.shared.graphs import (
    profile_graph_uri,
    profile_namespace,
    profile_version_graph_uri,
    schema_version_graph_uri,
    split_schema_iri,
)
from fdp.shared.namespaces import OWL, PROF, ROLE

if TYPE_CHECKING:
    from fdp.metadata.repository import MetadataRepository
    from fdp.storage.triplestore import TripleStoreAdapter

log = structlog.get_logger(__name__)

_TURTLE: Final = "text/turtle"
_SPARQL_JSON: Final = "application/sparql-results+json"
_NT: Final = "application/n-triples"


# --- response models -------------------------------------------------------


class ProfileInfo(BaseModel):
    """Summary of a current profile."""

    id: str
    iri: str
    validation_artifact: str | None = None
    """The ``prof:hasArtifact`` of the ``role:validation`` resource (a schema version)."""


class ProfileListView(BaseModel):
    profiles: list[ProfileInfo]


# --- graph builder + provisioning (the only writer) ------------------------


def build_profile_graph(subject: str | URIRef, validation_artifact: str | URIRef) -> Graph:
    """Build a minimal ``prof:Profile`` whose validation resource is ``artifact``.

    ``subject`` is the profile IRI the triples describe (the stable IRI for the
    current profile, or the version IRI for a snapshot).
    """
    graph = Graph()
    profile = URIRef(str(subject))
    graph.add((profile, RDF.type, PROF.Profile))
    resource = BNode()
    graph.add((profile, PROF.hasResource, resource))
    graph.add((resource, RDF.type, PROF.ResourceDescriptor))
    graph.add((resource, PROF.hasRole, ROLE.validation))
    graph.add((resource, PROF.hasArtifact, URIRef(str(validation_artifact))))
    return graph


async def provision_profile(
    adapter: TripleStoreAdapter, *, base_url: str, slug: str, version: int | str
) -> str:
    """Create/update the profile wrapping schema ``slug`` at ``version``.

    Writes the stable profile (``profiles/{slug}``, the ``dct:conformsTo`` target)
    pointing at the schema version snapshot, plus an immutable profile snapshot
    (``profiles/{slug}/{version}``, the ``fdp-o:validatedAgainst`` target). Bare
    graphs (no meta lifecycle): profiles are derived, not independently published.
    Idempotent for a given ``(slug, version)``. Returns the stable profile IRI.
    """
    base = base_url.rstrip("/")
    stable = str(profile_graph_uri(base, slug))
    snapshot = str(profile_version_graph_uri(base, slug, str(version)))
    artifact = str(schema_version_graph_uri(base, slug, str(version)))
    await adapter.replace_graph(
        stable, build_profile_graph(stable, artifact).serialize(format="nt"), mime=_NT
    )
    await adapter.replace_graph(
        snapshot, build_profile_graph(snapshot, artifact).serialize(format="nt"), mime=_NT
    )
    log.info("profile_provisioned", profile=stable, version=version, artifact=artifact)
    return stable


async def ensure_conformance(
    adapter: TripleStoreAdapter, repository: MetadataRepository, *, schema_iri: str
) -> tuple[str, str] | None:
    """Resolve ``(stable profile IRI, profile version IRI)`` for a record whose
    validating schema is ``schema_iri`` (ADR-0019 §1/§3).

    The stable profile IRI is the record's ``dct:conformsTo`` target; the version
    IRI is its ``fdp-o:validatedAgainst`` target. The profile (and the schema
    version snapshot it wraps) is provisioned **on demand** — so a schema seeded
    at bootstrap, which never went through the schema service, self-heals on the
    first record write of its type (the migration in 20.6 does this eagerly).

    Returns ``None`` when ``schema_iri`` is not a managed schema (an external
    shape has no derived profile) or has no stored shape to wrap.
    """
    split = split_schema_iri(schema_iri)
    if split is None:
        return None
    base, slug = split
    stable_profile = str(profile_graph_uri(base, slug))
    existing = await repository.get_graph(stable_profile)
    if len(existing) > 0:
        artifact = next(iter(existing.objects(None, PROF.hasArtifact)), None)
        if artifact is not None:
            version = str(artifact).rsplit("/", 1)[-1]
            return stable_profile, str(profile_version_graph_uri(base, slug, version))
    # Lazy provision from the stable schema (bootstrap-seeded, or pre-feature).
    version = await _current_schema_version(repository, schema_iri)
    artifact_iri = str(schema_version_graph_uri(base, slug, version))
    if len(await repository.get_graph(artifact_iri)) == 0:
        shape = await repository.get_graph(schema_iri)
        if len(shape) == 0:
            return None
        await adapter.replace_graph(artifact_iri, shape.serialize(format="nt"), mime=_NT)
    await provision_profile(adapter, base_url=base, slug=slug, version=version)
    return stable_profile, str(profile_version_graph_uri(base, slug, version))


async def _current_schema_version(repository: MetadataRepository, schema_iri: str) -> str:
    """The schema's current ``owl:versionInfo`` (from its meta graph), or ``"1"``."""
    meta = await repository.get_meta(schema_iri)
    value = next(iter(meta.objects(URIRef(schema_iri), OWL.versionInfo)), None)
    return str(value) if value is not None else "1"


# --- read-only service -----------------------------------------------------


class ProfileService:
    """Read access to provisioned PROF profiles. Stateless; safe to share."""

    def __init__(
        self, *, repository: MetadataRepository, adapter: TripleStoreAdapter, base_url: str
    ) -> None:
        self._repo = repository
        self._adapter = adapter
        self._base = base_url.rstrip("/")

    async def get_turtle(self, profile_id: str, *, version: str | None = None) -> str:
        iri = (
            profile_version_graph_uri(self._base, profile_id, version)
            if version is not None
            else profile_graph_uri(self._base, profile_id)
        )
        graph = await self._repo.get_graph(iri)
        if len(graph) == 0:
            suffix = f"/{version}" if version is not None else ""
            raise NotFound(f"no profile: {profile_id}{suffix}")
        return graph.serialize(format="turtle")

    async def list_profiles(self) -> list[ProfileInfo]:
        query = (
            "SELECT ?g (SAMPLE(?art) AS ?artifact) WHERE { GRAPH ?g {"
            f" ?p a <{PROF.Profile}> ."
            f" OPTIONAL {{ ?p <{PROF.hasResource}> ?r . ?r <{PROF.hasRole}> <{ROLE.validation}> ;"
            f" <{PROF.hasArtifact}> ?art }} }}"
            f' FILTER(STRSTARTS(STR(?g), "{profile_namespace(self._base)}/")) }} GROUP BY ?g'
        )
        rows = await self._select(query)
        prefix = f"{profile_namespace(self._base)}/"
        items: list[ProfileInfo] = []
        for row in rows:
            iri = row.get("g", {}).get("value")
            if not iri:
                continue
            # Skip immutable version snapshots (<stable>/<version>).
            if "/" in iri[len(prefix) :]:
                continue
            items.append(
                ProfileInfo(
                    id=iri.rsplit("/", 1)[-1],
                    iri=iri,
                    validation_artifact=row.get("artifact", {}).get("value"),
                )
            )
        items.sort(key=lambda p: p.id)
        return items

    async def _select(self, query: str) -> list[dict[str, dict[str, str]]]:
        body = await self._adapter.query(query, accept=_SPARQL_JSON)
        payload = json.loads(body)
        return payload.get("results", {}).get("bindings", [])


# --- router ----------------------------------------------------------------


def build_profile_router(*, service: ProfileService, prefix: str = "/profiles") -> APIRouter:
    """Build the read-only profile router (all reads public, ADR-0019)."""
    router = APIRouter(prefix=prefix, tags=["profiles"])

    @router.get("", response_model=ProfileListView, name="profile_list")
    async def list_profiles() -> ProfileListView:  # pyright: ignore[reportUnusedFunction]
        return ProfileListView(profiles=await service.list_profiles())

    @router.get("/{profile_id}", name="profile_get")
    async def get_profile(profile_id: str) -> Response:  # pyright: ignore[reportUnusedFunction]
        turtle = await service.get_turtle(profile_id)
        return Response(content=turtle, media_type=_TURTLE)

    @router.get("/{profile_id}/{version}", name="profile_get_version")
    async def get_profile_version(  # pyright: ignore[reportUnusedFunction]
        profile_id: str, version: str
    ) -> Response:
        turtle = await service.get_turtle(profile_id, version=version)
        return Response(content=turtle, media_type=_TURTLE)

    return router


__all__ = [
    "ProfileInfo",
    "ProfileListView",
    "ProfileService",
    "build_profile_graph",
    "build_profile_router",
    "ensure_conformance",
    "provision_profile",
]
