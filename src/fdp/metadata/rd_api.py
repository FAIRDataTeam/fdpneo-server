"""Resource-definition catalog + admin API (ADR-0009, mirrors the RI's
``ResourceDefinitionController``).

Two surfaces under ``/resource-definitions``:

* **Public read** — ``GET /resource-definitions`` (the type catalog) and
  ``GET /resource-definitions/{slug}``. Anonymous-readable so the client can
  discover the deployment's types at runtime (the dynamic-catalog client task
  consumes this) and so create forms know which child types a container holds.
  Reads are served from the in-memory cache (``app.state.resource_definitions``)
  via ``cache_provider``.

* **Admin mutation** — ``POST`` (create), ``PUT /{slug}`` (replace, which is
  how a child link is added to an existing type — e.g. wiring Catalog → a new
  Ontology type), and ``DELETE /{slug}``. Mutations run through
  :class:`ResourceDefinitionService`, whose ``on_rebuilt`` callback (wired in
  ``main``) swaps the cache, clears the cached OpenAPI, and warms the
  validator/authz caches — so a new type's LDP endpoints and OpenAPI paths
  light up with no restart.

**Authorization.** Resource definitions are *deployment configuration*, not
metadata records, so — like the runtime-settings surface — mutations require
the ``admin`` role rather than going through the ODRL PDP (which protects
individual records via Offers). Reads are public.

**Addressing.** Records are addressed by their stable slug (the URL prefix,
or a name slug for the root) — the same slug that forms the storage IRI — so
the root (empty prefix) is still reachable at ``/resource-definitions/repository``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Final

import structlog
from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from fdp.identity.deps import require_auth
from fdp.metadata.profiles import (
    ChildLinkRecord,
    ResourceDefinitionRecord,
    rd_record_slug,
)
from fdp.shared.context import RequestContext
from fdp.shared.errors import BadRequest, Conflict, Forbidden, NotFound
from fdp.shared.reserved import RESERVED_API_PREFIX

if TYPE_CHECKING:
    from collections.abc import Callable

    from fdp.metadata.profiles import (
        ChildLinkInfo,
        ResourceDefinition,
        ResourceDefinitionCache,
        ResourceDefinitionService,
    )

log = structlog.get_logger(__name__)

_ADMIN_ROLE: Final = "admin"

# All fixed FDP endpoints live under the single reserved API segment
# (``shared.reserved``), so that is the only first path segment a resource
# definition may not claim — every other root word is free for user types.
RESERVED_PREFIXES: Final = frozenset({RESERVED_API_PREFIX})


# --- DTOs ------------------------------------------------------------------


class ChildLinkInput(BaseModel):
    """A child link in a create/replace request (unresolved form)."""

    model_config = ConfigDict(populate_by_name=True)

    relation_uri: str = Field(alias="relationUri")
    target: str
    """URL prefix of the target resource definition."""
    title: str = ""
    tags_uri: str | None = Field(default=None, alias="tagsUri")


class ResourceDefinitionInput(BaseModel):
    """Create/replace request body for a resource definition."""

    model_config = ConfigDict(populate_by_name=True)

    url_prefix: str = Field(alias="urlPrefix")
    name: str
    schema_iri: str = Field(alias="schema")
    children: list[ChildLinkInput] = Field(default_factory=list)

    def to_record(self) -> ResourceDefinitionRecord:
        return ResourceDefinitionRecord(
            url_prefix=self.url_prefix,
            name=self.name,
            schema_iri=self.schema_iri,
            children=tuple(
                ChildLinkRecord(
                    relation_uri=c.relation_uri,
                    target_prefix=c.target,
                    title=c.title,
                    tags_uri=c.tags_uri,
                )
                for c in self.children
            ),
        )


class ChildLinkView(BaseModel):
    """A resolved child link in a read response."""

    model_config = ConfigDict(populate_by_name=True)

    relation_uri: str = Field(serialization_alias="relationUri")
    target: str
    target_name: str = Field(serialization_alias="targetName")
    target_schema_iri: str = Field(serialization_alias="targetSchema")
    title: str
    tags_uri: str | None = Field(default=None, serialization_alias="tagsUri")


class ResourceDefinitionLinks(BaseModel):
    """Absolute, followable URLs for a resource definition (ADR-0022 §4).

    Self-describing so a client need not concatenate ``urlPrefix`` onto a base it
    must already know. Built from the *serving* base (ADR-0014 split), the host a
    client actually calls.
    """

    self_: str = Field(serialization_alias="self")
    container: str
    spec: str


class ResourceDefinitionView(BaseModel):
    """A resolved resource definition in a read response."""

    model_config = ConfigDict(populate_by_name=True)

    slug: str
    url_prefix: str = Field(serialization_alias="urlPrefix")
    name: str
    schema_iri: str = Field(serialization_alias="schema")
    is_root: bool = Field(serialization_alias="isRoot")
    children: list[ChildLinkView]
    links: ResourceDefinitionLinks


class ResourceDefinitionListView(BaseModel):
    """Response for ``GET /resource-definitions`` — the full type catalog."""

    definitions: list[ResourceDefinitionView]


def _child_view(link: ChildLinkInfo) -> ChildLinkView:
    return ChildLinkView(
        relation_uri=link.relation_uri,
        target=link.target_prefix,
        target_name=link.target_name,
        target_schema_iri=link.target_schema_iri,
        title=link.title,
        tags_uri=link.tags_uri,
    )


def _links(rd: ResourceDefinition, base_url: str) -> ResourceDefinitionLinks:
    base = base_url.rstrip("/")
    slug = rd_record_slug(rd.url_prefix, rd.name)
    # The root's container is the base itself (empty url_prefix); its spec is the
    # root-level /spec view. Non-root types hang off /{urlPrefix}.
    container = base if rd.is_root else f"{base}/{rd.url_prefix}"
    return ResourceDefinitionLinks(
        self_=f"{base}/{RESERVED_API_PREFIX}/resource-definitions/{slug}",
        container=container,
        spec=f"{container}/spec",
    )


def _view(rd: ResourceDefinition, base_url: str) -> ResourceDefinitionView:
    return ResourceDefinitionView(
        slug=rd_record_slug(rd.url_prefix, rd.name),
        url_prefix=rd.url_prefix,
        name=rd.name,
        schema_iri=rd.schema_iri,
        is_root=rd.is_root,
        children=[_child_view(c) for c in rd.children],
        links=_links(rd, base_url),
    )


# --- router ----------------------------------------------------------------


def build_resource_definition_router(
    *,
    service: ResourceDefinitionService,
    cache_provider: Callable[[], ResourceDefinitionCache | None],
    base_url: str,
    prefix: str = "/resource-definitions",
) -> APIRouter:
    """Construct the resource-definition catalog + admin router.

    ``cache_provider`` returns the current cache (``app.state.resource_definitions``)
    so reads reflect runtime mutations without rebuilding the router. ``base_url``
    is the serving base (ADR-0014) from which each view's absolute ``links`` are
    built (ADR-0022 §4).
    """
    router = APIRouter(prefix=prefix, tags=["resource-definitions"])

    def _cache() -> ResourceDefinitionCache:
        cache = cache_provider()
        if cache is None:
            raise NotFound("no resource definitions are configured")
        return cache

    def _find(slug: str) -> ResourceDefinition:
        for rd in _cache().all():
            if rd_record_slug(rd.url_prefix, rd.name) == slug:
                return rd
        raise NotFound(f"no resource definition: {slug}")

    def _require_admin(ctx: RequestContext) -> None:
        if _ADMIN_ROLE not in ctx.roles:
            raise Forbidden(
                "admin role required to manage resource definitions",
                details={"required_role": _ADMIN_ROLE},
            )

    async def _validate_writable(record: ResourceDefinitionRecord) -> None:
        """Reserved-prefix and schema-existence checks shared by create/replace."""
        if record.url_prefix in RESERVED_PREFIXES:
            raise BadRequest(
                f"url prefix '{record.url_prefix}' is reserved",
                details={"reserved": sorted(RESERVED_PREFIXES)},
            )
        if not await service.schema_exists(record.schema_iri):
            raise BadRequest(
                "schema does not exist; publish the SHACL shape first",
                details={"schema": record.schema_iri},
            )

    @router.get("", response_model=ResourceDefinitionListView, name="rd_list")
    async def list_definitions() -> ResourceDefinitionListView:  # pyright: ignore[reportUnusedFunction]
        return ResourceDefinitionListView(
            definitions=[_view(rd, base_url) for rd in _cache().all()]
        )

    @router.get("/{slug}", response_model=ResourceDefinitionView, name="rd_get")
    async def get_definition(slug: str) -> ResourceDefinitionView:  # pyright: ignore[reportUnusedFunction]
        return _view(_find(slug), base_url)

    @router.post("", response_model=ResourceDefinitionView, status_code=201, name="rd_create")
    async def create_definition(  # pyright: ignore[reportUnusedFunction]
        body: ResourceDefinitionInput,
        ctx: Annotated[RequestContext, Depends(require_auth)],
    ) -> ResourceDefinitionView:
        """Register a new metadata type (admin only).

        Exposing a type is a two-step act (ADR-0009): **first** publish the
        type's SHACL shape as a record (e.g. ``PUT`` the shape graph to a
        base-relative IRI such as ``{base}/shapes/Ontology`` through the LDP
        API), **then** register a resource definition here whose ``schema``
        points at that shape IRI. This call rejects a ``schema`` that does not
        resolve to a published SHACL shape, so the order matters. On success
        the type's LDP endpoints (``/{urlPrefix}`` …) and OpenAPI paths appear
        immediately — no restart.
        """
        _require_admin(ctx)
        record = body.to_record()
        slug = rd_record_slug(record.url_prefix, record.name)
        if any(rd_record_slug(rd.url_prefix, rd.name) == slug for rd in _cache().all()):
            raise Conflict(
                f"a resource definition already exists at '{slug}'",
                details={"slug": slug},
            )
        await _validate_writable(record)
        await service.put(record, subject=ctx.subject)
        return _view(_find(slug), base_url)

    @router.put("/{slug}", response_model=ResourceDefinitionView, name="rd_replace")
    async def replace_definition(  # pyright: ignore[reportUnusedFunction]
        slug: str,
        body: ResourceDefinitionInput,
        ctx: Annotated[RequestContext, Depends(require_auth)],
    ) -> ResourceDefinitionView:
        """Replace an existing definition (admin only).

        The whole definition is replaced, including its ``children`` — this is
        how a child link is added to an existing type (e.g. give Catalog a
        ``children`` entry targeting a new Ontology type so catalogs can hold
        ontology records). The body's ``urlPrefix``/``name`` must keep the same
        slug; renaming is a create, not a replace. As with create, ``schema``
        must resolve to a published SHACL shape.
        """
        _require_admin(ctx)
        _find(slug)  # 404 if it doesn't exist
        record = body.to_record()
        if rd_record_slug(record.url_prefix, record.name) != slug:
            raise BadRequest(
                "url prefix / name in body would change the resource's slug; "
                "create a new definition instead of renaming",
                details={"slug": slug},
            )
        await _validate_writable(record)
        await service.put(record, subject=ctx.subject)
        return _view(_find(slug), base_url)

    @router.delete("/{slug}", status_code=204, name="rd_delete")
    async def delete_definition(  # pyright: ignore[reportUnusedFunction]
        slug: str,
        ctx: Annotated[RequestContext, Depends(require_auth)],
    ) -> None:
        _require_admin(ctx)
        rd = _find(slug)
        if rd.is_root:
            raise BadRequest(
                "the root Repository definition cannot be deleted",
                details={"slug": slug},
            )
        await service.delete(
            ResourceDefinitionRecord(
                url_prefix=rd.url_prefix, name=rd.name, schema_iri=rd.schema_iri
            )
        )

    return router


__all__ = [
    "RESERVED_PREFIXES",
    "ChildLinkInput",
    "ChildLinkView",
    "ResourceDefinitionInput",
    "ResourceDefinitionListView",
    "ResourceDefinitionView",
    "build_resource_definition_router",
]
