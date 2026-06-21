"""Form autocomplete endpoint (task 6.2).

Powers the form-widget value pickers in ``fdp-client``: license,
publisher, MIME type, and whatever else an admin curates via
``PUT /settings/forms.autocomplete-sources``. Public read; the source
*management* surface (Phase 9.3) is admin-only and lives in
:mod:`fdp.metadata.settings`.

Source resolution per call:

1. Read ``forms.autocomplete-sources`` from the settings repository
   (which already merges the registered default with any admin
   override).
2. Find the source by name; 404 if unknown.
3. Resolve depending on the source's ``kind``:
   * ``inline`` — case-insensitive prefix match against ``label`` and
     ``aliases``. No I/O.
   * ``sparql`` — substitute the ``${PREFIX}`` and ``${LIMIT}``
     placeholders into the stored query (with proper SPARQL string
     escaping), execute via the triple store adapter, return
     ``?iri`` / ``?label`` bindings.

Security:

* SPARQL sources are admin-curated. The runtime substitutes the
  caller's ``prefix`` parameter as a properly-escaped SPARQL string
  literal — not by f-string concatenation — so a typed prefix cannot
  break the query.
* No outbound HTTP. A ``remote-vocabulary`` kind was deferred for a
  later iteration to avoid SSRF.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Annotated, Any, Final

import structlog
from fastapi import APIRouter, Query
from pydantic import BaseModel

from fdp.metadata.settings import (
    AutocompleteSource,
    AutocompleteSources,
    SettingsRepository,
)
from fdp.shared.errors import BadRequest, NotFound

if TYPE_CHECKING:
    from fdp.storage.triplestore.adapter import TripleStoreAdapter


log = structlog.get_logger(__name__)


_DEFAULT_LIMIT: Final = 25
_MAX_LIMIT: Final = 200
_MAX_PREFIX_LENGTH: Final = 256


# --- response models -----------------------------------------------------


class AutocompleteResultItem(BaseModel):
    """One item in the autocomplete response."""

    iri: str
    label: str
    source: str


class AutocompleteResponse(BaseModel):
    """Response shape for ``GET /forms/autocomplete``."""

    items: list[AutocompleteResultItem]


# --- service -------------------------------------------------------------


class AutocompleteService:
    """Resolves autocomplete queries against the configured sources.

    Stateless. The settings repository is read on every call so an
    admin's ``PUT /settings/forms.autocomplete-sources`` is visible
    immediately, with no cache invalidation.
    """

    def __init__(
        self,
        *,
        settings_repository: SettingsRepository,
        adapter: TripleStoreAdapter,
    ) -> None:
        self._settings = settings_repository
        self._adapter = adapter

    async def resolve(
        self, *, source: str, prefix: str, limit: int
    ) -> list[AutocompleteResultItem]:
        if len(prefix) > _MAX_PREFIX_LENGTH:
            raise BadRequest(
                "prefix too long",
                details={"max": _MAX_PREFIX_LENGTH},
            )
        sources_model = await self._settings.read_with_default("forms.autocomplete-sources")
        assert isinstance(sources_model, AutocompleteSources)
        src = next((s for s in sources_model.sources if s.name == source), None)
        if src is None:
            raise NotFound(
                f"unknown autocomplete source: {source}",
                details={
                    "available": [s.name for s in sources_model.sources],
                },
            )
        if src.kind == "inline":
            return _resolve_inline(src, prefix=prefix, limit=limit)
        if src.kind == "sparql":
            return await self._resolve_sparql(src, prefix=prefix, limit=limit)
        # Should be impossible — kind is a Literal — but the runtime
        # branch keeps the type checker honest if a future kind is
        # added without a matching resolver.
        raise BadRequest(
            f"source '{source}' has unsupported kind '{src.kind}'",
        )

    async def _resolve_sparql(
        self, source: AutocompleteSource, *, prefix: str, limit: int
    ) -> list[AutocompleteResultItem]:
        if not source.sparql:
            return []
        sparql = source.sparql.replace("${PREFIX}", _sparql_string_literal(prefix)).replace(
            "${LIMIT}", str(limit)
        )
        # If the source's SPARQL doesn't include its own LIMIT, append
        # one defensively so a malformed source can't fan out to
        # millions of bindings.
        if "${LIMIT}" not in source.sparql and " LIMIT " not in sparql.upper():
            sparql = f"{sparql.rstrip()}\nLIMIT {limit}\n"
        body = await self._adapter.query(sparql)
        return _parse_sparql_items(body, source_name=source.name, limit=limit)


# --- pure helpers --------------------------------------------------------


def _resolve_inline(
    source: AutocompleteSource, *, prefix: str, limit: int
) -> list[AutocompleteResultItem]:
    """Filter an inline source's items by case-insensitive prefix match."""
    needle = prefix.lower()
    matched: list[AutocompleteResultItem] = []
    for item in source.items:
        haystack = [item.label.lower(), *(a.lower() for a in item.aliases)]
        if not needle or any(h.startswith(needle) for h in haystack):
            matched.append(
                AutocompleteResultItem(
                    iri=item.iri,
                    label=item.label,
                    source=source.name,
                )
            )
            if len(matched) >= limit:
                break
    return matched


def _parse_sparql_items(
    body: bytes, *, source_name: str, limit: int
) -> list[AutocompleteResultItem]:
    payload: dict[str, Any] = json.loads(body)
    bindings: list[dict[str, Any]] = payload.get("results", {}).get("bindings", [])
    items: list[AutocompleteResultItem] = []
    seen_iris: set[str] = set()
    for row in bindings:
        iri = row.get("iri", {}).get("value")
        label = row.get("label", {}).get("value")
        if not iri or not label or iri in seen_iris:
            continue
        seen_iris.add(iri)
        items.append(AutocompleteResultItem(iri=iri, label=label, source=source_name))
        if len(items) >= limit:
            break
    return items


def _sparql_string_literal(value: str) -> str:
    """Render ``value`` as a SPARQL ``"..."`` string literal.

    JSON's escape rules are a subset of SPARQL's, so ``json.dumps``
    produces a literal that the SPARQL parser accepts unchanged. This
    is the safe substitution path for caller-supplied prefixes.
    """
    return json.dumps(value)


# --- router --------------------------------------------------------------


def build_autocomplete_router(*, service: AutocompleteService) -> APIRouter:
    """Construct ``GET /forms/autocomplete``.

    Public read. The companion management surface is admin-only and
    lives at ``PUT /settings/forms.autocomplete-sources``.
    """
    router = APIRouter(tags=["forms"])

    @router.get(
        "/forms/autocomplete",
        response_model=AutocompleteResponse,
        name="forms_autocomplete",
    )
    async def autocomplete(  # pyright: ignore[reportUnusedFunction]
        source: Annotated[
            str,
            Query(
                min_length=1,
                max_length=128,
                description="Name of the configured autocomplete source.",
            ),
        ],
        prefix: Annotated[
            str,
            Query(
                description="Case-insensitive prefix to match.",
                max_length=_MAX_PREFIX_LENGTH,
            ),
        ] = "",
        limit: Annotated[
            int,
            Query(
                ge=1,
                le=_MAX_LIMIT,
                description="Maximum items returned.",
            ),
        ] = _DEFAULT_LIMIT,
    ) -> AutocompleteResponse:
        items = await service.resolve(source=source, prefix=prefix, limit=limit)
        return AutocompleteResponse(items=items)

    return router


__all__ = [
    "AutocompleteResponse",
    "AutocompleteResultItem",
    "AutocompleteService",
    "build_autocomplete_router",
]
