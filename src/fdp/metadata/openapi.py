"""Dynamic OpenAPI generation from the applied resource definitions.

Mirrors the FDP reference implementation's ``OpenApiGenerator`` /
``OpenApiService`` pair: the runtime LDP router stays a single
``/{path:path}`` catch-all, but the documented OpenAPI surface
enumerates one typed set of paths per resource definition. A profile
that declares an ``Ontology`` type therefore lights up
``/ontology``, ``/ontology/{id}``, ``/ontology/{id}/spec``, etc., with
``Metadata: Ontology`` tagged operations — no Python code change
needed.

Path set per non-root resource definition (matching the RI):

* ``POST   /{urlPrefix}`` — create a member of that type.
* ``GET   /{urlPrefix}/{id}`` — read.
* ``PUT   /{urlPrefix}/{id}`` — replace.
* ``DELETE /{urlPrefix}/{id}`` — delete.
* ``GET   /{urlPrefix}/{id}/spec`` — the SHACL shape that validates members.
* ``GET   /{urlPrefix}/{id}/expanded`` — record + parents (``dct:isPartOf`` walk).
* ``GET   /{urlPrefix}/{id}/page/{childPrefix}`` — paginated children of a
  declared child type, one per ``ResourceDefinition.children`` entry.

Root resource definition (``urlPrefix == ""``) gets the same set
minus the ``/{urlPrefix}`` prefix and minus the POST:

* ``GET / PUT / DELETE  /``
* ``GET  /spec`` / ``GET  /expanded`` / ``GET  /page/{childPrefix}``

Operations the RI also emits but that we defer to v1.x are documented
in the plan: ``/meta``, ``/meta/state`` (publication-state workflow),
``/members``, ``/members/{userUuid}`` (per-record sharing).

Every operation carries the extension
``x-fdp-resource-definition: <urlPrefix>`` so the service can find and
remove paths when a definition is replaced or deleted (same hook the
RI uses for live updates).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fdp.metadata.profiles.registry import (
        ResourceDefinition,
        ResourceDefinitionCache,
    )


# --- media type catalogue --------------------------------------------------


_RDF_MEDIA_TYPES: tuple[str, ...] = (
    "text/turtle",
    "application/ld+json",
    "application/rdf+xml",
    "application/n-triples",
)


def _rdf_content() -> dict[str, dict[str, Any]]:
    """OpenAPI ``content`` block for the supported RDF serializations."""
    return {mt: {"schema": {"type": "string"}} for mt in _RDF_MEDIA_TYPES}


def _rdf_responses(*, ok_status: str = "200") -> dict[str, dict[str, Any]]:
    rdf_response = {
        "description": "RDF graph",
        "content": _rdf_content(),
    }
    return {
        ok_status: rdf_response,
        "400": {"description": "Bad Request"},
        "401": {"description": "Unauthorized"},
        "403": {"description": "Forbidden"},
        "404": {"description": "Not Found"},
    }


def _rdf_request_body(description: str) -> dict[str, Any]:
    return {
        "description": description,
        "required": True,
        "content": _rdf_content(),
    }


def _delete_responses() -> dict[str, dict[str, Any]]:
    return {
        "204": {"description": "No Content"},
        "401": {"description": "Unauthorized"},
        "403": {"description": "Forbidden"},
        "404": {"description": "Not Found"},
    }


def _id_param() -> dict[str, Any]:
    return {
        "name": "id",
        "in": "path",
        "required": True,
        "schema": {"type": "string"},
        "description": "Resource identifier (path segment).",
    }


def _child_prefix_param() -> dict[str, Any]:
    return {
        "name": "childPrefix",
        "in": "path",
        "required": True,
        "schema": {"type": "string"},
        "description": "URL prefix of the child resource type to list.",
    }


# --- per-RD path emission --------------------------------------------------


_TAG_PREFIX = "Metadata: "
_RD_EXTENSION = "x-fdp-resource-definition"


def _tag_name(rd: ResourceDefinition) -> str:
    return f"{_TAG_PREFIX}{rd.name}"


def _rd_extension(rd: ResourceDefinition) -> dict[str, str]:
    return {_RD_EXTENSION: rd.url_prefix or "(root)"}


def _crud_operations(rd: ResourceDefinition, *, with_id_param: bool) -> dict[str, Any]:
    """GET / PUT / DELETE for a resource. ``id`` path param when applicable."""
    tag = _tag_name(rd)
    params = [_id_param()] if with_id_param else []
    return {
        "get": {
            "operationId": f"get{rd.name}",
            "summary": f"Retrieve a {rd.name}",
            "description": f"Fetch the RDF graph for one {rd.name} record.",
            "tags": [tag],
            "parameters": params,
            "responses": _rdf_responses(),
        },
        "put": {
            "operationId": f"replace{rd.name}",
            "summary": f"Replace a {rd.name}",
            "description": f"Replace the RDF graph for one {rd.name} record.",
            "tags": [tag],
            "parameters": params,
            "requestBody": _rdf_request_body(f"Replacement {rd.name} graph"),
            "responses": _rdf_responses(),
        },
        "delete": {
            "operationId": f"delete{rd.name}",
            "summary": f"Delete a {rd.name}",
            "description": f"Remove a {rd.name} and its sibling /meta and /audit graphs.",
            "tags": [tag],
            "parameters": params,
            "responses": _delete_responses(),
        },
    }


def _post_operation(rd: ResourceDefinition) -> dict[str, Any]:
    """POST on a collection: create a member of this type."""
    return {
        "post": {
            "operationId": f"create{rd.name}",
            "summary": f"Create a {rd.name}",
            "description": (
                f"Create a new {rd.name} member. The server mints the resource "
                "IRI; pass the ``Slug`` header to suggest a suffix."
            ),
            "tags": [_tag_name(rd)],
            "requestBody": _rdf_request_body(f"New {rd.name} graph"),
            "responses": _rdf_responses(ok_status="201"),
        }
    }


def _spec_operation(rd: ResourceDefinition) -> dict[str, Any]:
    return {
        "get": {
            "operationId": f"get{rd.name}Spec",
            "summary": f"Get SHACL shape for {rd.name}",
            "description": (
                f"Return the SHACL shape graph that validates {rd.name} "
                f"instances ({rd.schema_iri})."
            ),
            "tags": [_tag_name(rd)],
            "parameters": ([_id_param()] if not rd.is_root else []),
            "responses": _rdf_responses(),
        }
    }


def _expanded_operation(rd: ResourceDefinition) -> dict[str, Any]:
    return {
        "get": {
            "operationId": f"get{rd.name}Expanded",
            "summary": f"Get {rd.name} with parents",
            "description": (
                f"Return the {rd.name} record together with every ancestor "
                "reachable via dct:isPartOf."
            ),
            "tags": [_tag_name(rd)],
            "parameters": ([_id_param()] if not rd.is_root else []),
            "responses": _rdf_responses(),
        }
    }


def _page_operation(rd: ResourceDefinition) -> dict[str, Any]:
    """Paginated children listing. One operation parametrised by childPrefix.

    The RI emits one path with a ``{childPrefix}`` segment rather than a
    distinct path per declared child link. We follow the same approach.
    """
    return {
        "get": {
            "operationId": f"get{rd.name}ChildPage",
            "summary": f"List a page of {rd.name} children",
            "description": (
                f"Paginated listing of {rd.name} members of a given child "
                "type. ``childPrefix`` is the URL prefix of the target type."
            ),
            "tags": [_tag_name(rd)],
            "parameters": (
                ([_id_param()] if not rd.is_root else []) + [_child_prefix_param()]
            ),
            "responses": _rdf_responses(),
        }
    }


def _emit_paths_for(rd: ResourceDefinition, paths: dict[str, Any]) -> None:
    """Add the per-RD path set to ``paths``."""
    ext = _rd_extension(rd)
    if rd.is_root:
        crud_path = "/"
        spec_path = "/spec"
        expanded_path = "/expanded"
        page_path = "/page/{childPrefix}"
        crud_item = {**_crud_operations(rd, with_id_param=False), **ext}
        paths[crud_path] = crud_item
    else:
        prefix = "/" + rd.url_prefix
        collection_path = prefix
        crud_path = f"{prefix}/{{id}}"
        spec_path = f"{prefix}/{{id}}/spec"
        expanded_path = f"{prefix}/{{id}}/expanded"
        page_path = f"{prefix}/{{id}}/page/{{childPrefix}}"
        paths[collection_path] = {**_post_operation(rd), **ext}
        paths[crud_path] = {**_crud_operations(rd, with_id_param=True), **ext}

    # Children-bearing types get /spec, /expanded, /page (the root may
    # have no children, in which case /page is omitted).
    paths[spec_path] = {**_spec_operation(rd), **ext}
    paths[expanded_path] = {**_expanded_operation(rd), **ext}
    if rd.children:
        paths[page_path] = {**_page_operation(rd), **ext}


# --- public entry point ----------------------------------------------------


def inject_resource_definition_paths(
    spec: dict[str, Any],
    cache: ResourceDefinitionCache,
) -> None:
    """Mutate ``spec`` (FastAPI's OpenAPI dict) with per-type paths and tags.

    Idempotent: any path previously emitted with the ``x-fdp-resource-
    definition`` extension is removed first, so calling this after a
    profile re-apply produces the correct surface. Static (non-FDP)
    paths emitted by FastAPI's route inspection are left untouched.
    """
    paths: dict[str, Any] = spec.setdefault("paths", {})
    tags: list[dict[str, Any]] = spec.setdefault("tags", [])

    # 1. Drop any FDP-generated paths from a prior call so re-injection
    #    after a profile change yields the new surface.
    fdp_paths = [
        p for p, item in paths.items() if isinstance(item, dict) and _RD_EXTENSION in item
    ]
    for p in fdp_paths:
        paths.pop(p, None)

    # 2. Drop FDP-generated tags too — they belong to types that may no
    #    longer exist.
    spec["tags"] = [
        t for t in tags if not (
            isinstance(t, dict) and t.get("name", "").startswith(_TAG_PREFIX)
        )
    ]
    tags = spec["tags"]

    # 3. Emit new paths + tags.
    for rd in cache.all():
        _emit_paths_for(rd, paths)
        tags.append({"name": _tag_name(rd), "x-fdp-tag-priority": 10})


__all__ = ["inject_resource_definition_paths"]
