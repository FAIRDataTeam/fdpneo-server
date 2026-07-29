"""In-memory snapshot of the applied profile's resource definitions.

Mirrors the reference implementation's ``ResourceDefinitionService`` +
``ResourceDefinitionCache`` (Java/Spring). The cache is a *projection*:
:func:`build_cache_from_manifest` builds it from the profile manifest at
bootstrap, and :func:`fdpneo_server.metadata.profiles.rd_service.build_cache_from_repository`
rebuilds it from the resource-definition records stored in the triple store
(the runtime source of truth, ADR-0009). Both funnel through
:func:`resolve_cache`. Runtime mutation (admin adds an Ontology type) swaps a
freshly rebuilt cache onto ``app.state``; the cache object itself stays
immutable.

Two roles:

1. **Source of truth for the OpenAPI generator** (sub-task 15d). The
   generator iterates :meth:`ResourceDefinitionCache.all` and emits a
   tagged set of paths per definition, matching the RI's
   ``OpenApiGenerator.generatePathsForResourceDefinition``.
2. **ContainerRegistry implementation for the LDP router**. The cache
   satisfies the ``ContainerRegistry`` Protocol in
   :mod:`fdpneo_server.metadata.ldp.router` by duck typing — no explicit
   subclassing — so passing it as ``containers=cache`` makes POST to
   typed collections work and turns on SHACL validation per type.

URL → resource-definition resolution mirrors the RI's ``getByUrl``:
strip the configured base URL, take the first non-empty path segment
as the ``urlPrefix``. An empty path resolves to the root (Repository).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from fdpneo_server.metadata.profiles.iri import IRIExpander
from fdpneo_server.metadata.profiles.rd_records import ChildLinkRecord, ResourceDefinitionRecord

if TYPE_CHECKING:
    from fdpneo_server.metadata.profiles.manifest import ResourceDefinitionEntry


# --- value types ----------------------------------------------------------


@dataclass(frozen=True)
class ChildLinkInfo:
    """One resolved child link from one resource definition to another.

    All IRIs and target metadata have been resolved at cache-build time
    — readers don't need an :class:`IRIExpander` to use these.
    """

    relation_uri: str
    target_prefix: str
    target_name: str
    target_schema_iri: str
    title: str
    tags_uri: str | None


@dataclass(frozen=True)
class ResourceDefinition:
    """One metadata type the deployment exposes (post-CURIE-expansion)."""

    url_prefix: str
    name: str
    schema_iri: str
    children: tuple[ChildLinkInfo, ...]

    @property
    def is_root(self) -> bool:
        return self.url_prefix == ""


# --- cache ----------------------------------------------------------------


class ResourceDefinitionCache:
    """Snapshot of the applied resource definitions.

    Immutable once constructed. Rebuild via :func:`build_cache_from_manifest`
    after every successful :func:`fdpneo_server.metadata.profiles.apply_profile`.
    """

    def __init__(
        self,
        items: Iterable[ResourceDefinition],
        *,
        base_url: str,
    ) -> None:
        ordered = tuple(items)
        self._items: tuple[ResourceDefinition, ...] = ordered
        self._by_prefix: dict[str, ResourceDefinition] = {rd.url_prefix: rd for rd in ordered}
        self._base_url = base_url.rstrip("/")

    # --- read accessors ---------------------------------------------------

    def all(self) -> Sequence[ResourceDefinition]:
        return self._items

    @property
    def base_url(self) -> str:
        """The deployment base URL the definitions were resolved against."""
        return self._base_url

    def by_prefix(self, prefix: str) -> ResourceDefinition | None:
        return self._by_prefix.get(prefix)

    def root(self) -> ResourceDefinition | None:
        return self._by_prefix.get("")

    def for_url(self, url: str) -> ResourceDefinition | None:
        """Resolve a request URL to the responsible :class:`ResourceDefinition`.

        * ``<base_url>/`` or ``<base_url>`` → root RD.
        * ``<base_url>/catalog`` → catalog RD.
        * ``<base_url>/catalog/c-1`` → catalog RD (first segment wins).

        Returns ``None`` when the URL is not under ``base_url`` or the
        first segment doesn't name a declared resource definition.
        """
        prefix = self._extract_first_segment(url)
        if prefix is None:
            return None
        return self._by_prefix.get(prefix)

    # --- ContainerRegistry Protocol (duck typed) --------------------------

    def is_container(self, resource_iri: str) -> bool:
        """True iff ``resource_iri`` is a *collection* endpoint.

        Collections accept POST to add a new typed member. Member
        resources (those with an id segment after the prefix) are not
        containers under this Protocol — PUT/PATCH/DELETE only.
        """
        segs = self._segments(resource_iri)
        if segs is None:
            return False
        if len(segs) == 0:
            # Root: only a collection when the root RD declares
            # children — otherwise POST to / has nowhere to land.
            root = self.root()
            return root is not None and len(root.children) > 0
        if len(segs) == 1:
            return self._by_prefix.get(segs[0]) is not None
        return False

    def member_shape(self, container_iri: str) -> str | None:
        """Schema a new member of ``container_iri`` must satisfy.

        Mirrors the RI: POST to ``/<prefix>`` creates an instance of
        ``<prefix>``'s type, validated against that type's schema.
        """
        segs = self._segments(container_iri)
        if segs is None or len(segs) != 1:
            return None
        rd = self._by_prefix.get(segs[0])
        return rd.schema_iri if rd is not None else None

    def shape_for(self, resource_iri: str) -> str | None:
        """Schema the resource itself must satisfy (used on PUT / PATCH)."""
        segs = self._segments(resource_iri)
        if segs is None:
            return None
        prefix = segs[0] if segs else ""
        rd = self._by_prefix.get(prefix)
        return rd.schema_iri if rd is not None else None

    def member_relations(self, resource_iri: str) -> list[str]:
        """The typed member relations of the container at ``resource_iri``.

        A record is an LDP Direct Container iff its resource definition declares
        child links; this returns those links' ``relationUri`` values (a Catalog →
        ``[dcat:dataset, dcat:service]``, the root → ``[servesMetadata]``) which
        become the container's ``ldp:hasMemberRelation`` set. Empty for a leaf
        type (Distribution) or an unknown URL. Resolves by URL like
        :meth:`for_url`, so it works for both the root and member records.
        """
        rd = self.for_url(resource_iri)
        return [child.relation_uri for child in rd.children] if rd is not None else []

    def url_prefix_for(self, resource_iri: str) -> str | None:
        """The ``url_prefix`` of the type responsible for ``resource_iri``.

        ``""`` for the root, ``"catalog"`` for a catalog collection or member, and
        ``None`` when the IRI is not under a declared type (an internal/managed
        document). Used by the ADR-0022 affordance builder to name the type-level
        ``/spec`` view and gate the resource-definition-driven links.
        """
        rd = self.for_url(resource_iri)
        return rd.url_prefix if rd is not None else None

    def child_prefixes(self, resource_iri: str) -> list[str]:
        """The ``target_prefix`` of each child link the type at ``resource_iri``
        declares — the child types reachable via ``/page/{childPrefix}`` (ADR-0022
        member-page affordances). Empty for a leaf type or an unknown URL."""
        rd = self.for_url(resource_iri)
        return [child.target_prefix for child in rd.children] if rd is not None else []

    def containment_relation(self, parent_iri: str, child_iri: str) -> str | None:
        """The typed forward predicate a ``parent`` uses to point at ``child``.

        Resolves both IRIs to their resource definitions (by first path segment),
        then matches the child's type to one of the parent's declared child
        links: the link whose ``target_prefix`` equals the child's ``url_prefix``
        names the predicate (e.g. root → catalog ⇒ ``dcat:catalog``). Returns
        ``None`` when either side isn't a known type or the parent declares no
        link to the child's type — in which case only ``ldp:contains`` is
        maintained. Pure; used by the containment manager (forward membership).
        """
        parent = self.for_url(parent_iri)
        child = self.for_url(child_iri)
        if parent is None or child is None:
            return None
        for link in parent.children:
            if link.target_prefix == child.url_prefix:
                return link.relation_uri
        return None

    # --- internals --------------------------------------------------------

    def _segments(self, url: str) -> list[str] | None:
        """Return the non-empty path segments of ``url`` relative to base_url.

        Strips a trailing slash. ``None`` when the URL isn't under the
        configured base URL.
        """
        if not url.startswith(self._base_url):
            return None
        rest = url[len(self._base_url) :]
        rest = rest.split("?", 1)[0].split("#", 1)[0]
        return [s for s in rest.split("/") if s]

    def _extract_first_segment(self, url: str) -> str | None:
        segs = self._segments(url)
        if segs is None:
            return None
        return segs[0] if segs else ""


# --- factory --------------------------------------------------------------


def resolve_cache(
    records: Iterable[ResourceDefinitionRecord],
    *,
    base_url: str,
) -> ResourceDefinitionCache:
    """Resolve unresolved RD records into a :class:`ResourceDefinitionCache`.

    Pure. The cross-reference step turns each child link's *target prefix*
    into the target definition's name and schema IRI, so cache readers never
    have to look anything up. Shared by both the manifest path
    (:func:`build_cache_from_manifest`) and the triple-store path
    (:func:`fdpneo_server.metadata.profiles.rd_service.build_cache_from_repository`),
    which is why it takes already-expanded :class:`ResourceDefinitionRecord`
    rather than manifest entries or RDF.
    """
    records_list = list(records)
    name_by_prefix: dict[str, str] = {r.url_prefix: r.name for r in records_list}
    schema_by_prefix: dict[str, str] = {r.url_prefix: r.schema_iri for r in records_list}

    items: list[ResourceDefinition] = []
    for record in records_list:
        children = tuple(
            ChildLinkInfo(
                relation_uri=link.relation_uri,
                target_prefix=link.target_prefix,
                target_name=name_by_prefix.get(link.target_prefix, link.target_prefix),
                target_schema_iri=schema_by_prefix.get(link.target_prefix, ""),
                title=link.title,
                tags_uri=link.tags_uri,
            )
            for link in record.children
        )
        items.append(
            ResourceDefinition(
                url_prefix=record.url_prefix,
                name=record.name,
                schema_iri=record.schema_iri,
                children=children,
            )
        )

    return ResourceDefinitionCache(items, base_url=base_url)


def records_from_manifest(
    entries: Iterable[ResourceDefinitionEntry],
    *,
    expander: IRIExpander,
) -> list[ResourceDefinitionRecord]:
    """Expand manifest entries (CURIEs) into unresolved RD records (IRIs).

    The single conversion point from the profile manifest's edge model to
    the stored-record domain model; the bootstrap applier writes these to
    the triple store and :func:`build_cache_from_manifest` resolves them.
    """
    return [
        ResourceDefinitionRecord(
            url_prefix=entry.url_prefix,
            name=entry.name,
            # constrainedBy points at where the schema is *stored* (the schemas
            # namespace), not the class IRI — so it matches the applier's write
            # location (task 10.5) and resolves like any runtime schema. The
            # validator already enforces the schema is declared in schemas[].
            schema_iri=expander.schema_storage_iri(entry.schema_id),
            children=tuple(
                ChildLinkRecord(
                    # relation_uri is a vocabulary *predicate* (e.g. dcat:catalog),
                    # not a schema reference — it stays the expanded class IRI.
                    relation_uri=expander.schema_iri(link.relation_uri),
                    target_prefix=link.target,
                    title=link.title,
                    tags_uri=link.tags_uri,
                )
                for link in entry.children
            ),
        )
        for entry in entries
    ]


def build_cache_from_manifest(
    entries: Iterable[ResourceDefinitionEntry],
    *,
    expander: IRIExpander,
) -> ResourceDefinitionCache:
    """Translate manifest entries into a :class:`ResourceDefinitionCache`.

    Thin composition of :func:`records_from_manifest` (CURIE expansion) and
    :func:`resolve_cache` (cross-reference resolution). The validator
    already enforces that every reference resolves, so the lookup is safe.
    """
    return resolve_cache(
        records_from_manifest(entries, expander=expander),
        base_url=expander.base_url,
    )


__all__ = [
    "ChildLinkInfo",
    "ResourceDefinition",
    "ResourceDefinitionCache",
    "build_cache_from_manifest",
    "records_from_manifest",
    "resolve_cache",
]
