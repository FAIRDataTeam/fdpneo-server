"""In-memory snapshot of the applied profile's resource definitions.

Mirrors the reference implementation's ``ResourceDefinitionService`` +
``ResourceDefinitionCache`` (Java/Spring). One difference: our cache is
populated at apply-time from the parsed profile manifest, not stored
in Postgres as its own table. Live mutation (admin adds an Ontology
type at runtime) is out of v1 scope; a future increment can persist
the cache and add an admin REST surface.

Two roles:

1. **Source of truth for the OpenAPI generator** (sub-task 15d). The
   generator iterates :meth:`ResourceDefinitionCache.all` and emits a
   tagged set of paths per definition, matching the RI's
   ``OpenApiGenerator.generatePathsForResourceDefinition``.
2. **ContainerRegistry implementation for the LDP router**. The cache
   satisfies the ``ContainerRegistry`` Protocol in
   :mod:`fdp.metadata.ldp.router` by duck typing — no explicit
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

from fdp.metadata.profiles.iri import IRIExpander

if TYPE_CHECKING:
    from fdp.metadata.profiles.manifest import ResourceDefinitionEntry


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
    after every successful :func:`fdp.metadata.profiles.apply_profile`.
    """

    def __init__(
        self,
        items: Iterable[ResourceDefinition],
        *,
        base_url: str,
    ) -> None:
        ordered = tuple(items)
        self._items: tuple[ResourceDefinition, ...] = ordered
        self._by_prefix: dict[str, ResourceDefinition] = {
            rd.url_prefix: rd for rd in ordered
        }
        self._base_url = base_url.rstrip("/")

    # --- read accessors ---------------------------------------------------

    def all(self) -> Sequence[ResourceDefinition]:
        return self._items

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


def build_cache_from_manifest(
    entries: Iterable[ResourceDefinitionEntry],
    *,
    expander: IRIExpander,
) -> ResourceDefinitionCache:
    """Translate manifest entries into a :class:`ResourceDefinitionCache`.

    CURIE-expansion happens here (schemas and child relation URIs);
    cross-reference resolution (``children[].target`` → target
    definition's name/schema) happens here too, so cache readers never
    have to look back at the manifest. The validator (sub-task 15a)
    already enforces that every reference resolves, so we can trust
    the lookup.
    """
    entries_list = list(entries)
    name_by_prefix: dict[str, str] = {e.url_prefix: e.name for e in entries_list}
    schema_by_prefix: dict[str, str] = {
        e.url_prefix: expander.schema_iri(e.schema_id) for e in entries_list
    }

    items: list[ResourceDefinition] = []
    for entry in entries_list:
        children = tuple(
            ChildLinkInfo(
                relation_uri=expander.schema_iri(link.relation_uri),
                target_prefix=link.target,
                target_name=name_by_prefix.get(link.target, link.target),
                target_schema_iri=schema_by_prefix.get(link.target, ""),
                title=link.title,
                tags_uri=link.tags_uri,
            )
            for link in entry.children
        )
        items.append(
            ResourceDefinition(
                url_prefix=entry.url_prefix,
                name=entry.name,
                schema_iri=schema_by_prefix[entry.url_prefix],
                children=children,
            )
        )

    return ResourceDefinitionCache(items, base_url=expander.base_url)


__all__ = [
    "ChildLinkInfo",
    "ResourceDefinition",
    "ResourceDefinitionCache",
    "build_cache_from_manifest",
]
