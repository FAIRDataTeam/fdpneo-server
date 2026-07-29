"""Runtime settings (Phase 9.1 + 9.2 + 9.3 + 9.4 data shapes).

A small key-value store overlaying deployment-profile defaults with
admin-managed runtime overrides. Each registered key has a typed
Pydantic schema, a default value, and an audit-logged write path.

Keys currently registered:

* ``forms.autocomplete-sources`` — feeds the
  :mod:`fdpneo_server.metadata.autocomplete` endpoint (Phase 6.2). Each source
  declares a name, a kind (``inline`` | ``sparql``), and the payload.
* ``search.filters`` — declares the facet dimensions the (future)
  search UI exposes. The data shape is here so admins can stage the
  config; nothing consumes it yet (Phase 7 search itself is not
  shipped).

Auth model:

* ``GET /settings`` / ``GET /settings/{key}`` — public read. The client
  reads on every page load to know which sources / filters are
  available.
* ``PUT /settings/{key}`` / ``DELETE /settings/{key}`` — admin-only.
  Validated against the per-key Pydantic model; ``DELETE`` reverts to
  the registered default.

Audit:

Updates are logged via structlog (``settings_updated`` /
``settings_deleted`` events with key + subject). A persistent
settings-audit table is deferred — the structured logs cover the
common operator need without another schema migration.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any, Final, Literal, cast

import structlog
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import JSON, CursorResult, String, delete, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import DateTime, TypeDecorator

from fdpneo_server.identity.deps import require_auth
from fdpneo_server.shared.context import RequestContext
from fdpneo_server.shared.errors import BadRequest, Forbidden, NotFound
from fdpneo_server.storage.postgres.models import Base

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


log = structlog.get_logger(__name__)

_ADMIN_ROLE: Final = "admin"


# --- per-key Pydantic models ---------------------------------------------


class AutocompleteItem(BaseModel):
    """One entry in an ``inline`` autocomplete source."""

    iri: str
    label: str
    aliases: list[str] = Field(default_factory=list)


class AutocompleteSource(BaseModel):
    """One named source the form-autocomplete endpoint can query.

    Exactly one payload field is meaningful per ``kind``:

    * ``inline`` reads ``items`` and ignores ``sparql``.
    * ``sparql`` reads ``sparql`` and ignores ``items``. The query must
      project ``?iri`` and ``?label`` columns; an optional ``?prefix``
      variable receives the user's typed prefix (bound by the service,
      not interpolated raw).
    """

    name: str
    kind: Literal["inline", "sparql"]
    description: str | None = None
    items: list[AutocompleteItem] = Field(default_factory=list)
    sparql: str | None = None


class AutocompleteSources(BaseModel):
    """Top-level value for the ``forms.autocomplete-sources`` key."""

    sources: list[AutocompleteSource] = Field(default_factory=list)


class SearchFilter(BaseModel):
    """One facet dimension exposed on the search page.

    Phase 9.4: the data shape is here so admins can stage the config;
    nothing consumes it yet (search itself is Phase 7).
    """

    name: str
    label: str
    predicate: str
    type_filter: str | None = None


class SearchFilters(BaseModel):
    """Top-level value for the ``search.filters`` key."""

    filters: list[SearchFilter] = Field(default_factory=list)


# --- defaults ------------------------------------------------------------


def _default_autocomplete_sources() -> AutocompleteSources:
    """Default autocomplete sources shipped with v1.

    Three out-of-the-box sources:

    * ``license`` — inline list of common open data / open source
      licenses, what most ``dct:license`` fields land on.
    * ``mime-type`` — inline list of typical distribution media types.
    * ``publisher`` — SPARQL-backed lookup against any
      ``foaf:Organization`` already in the triple store, scoped by the
      typed-in prefix. This keeps publisher autocomplete useful even
      before an admin curates a list.
    """
    return AutocompleteSources(
        sources=[
            AutocompleteSource(
                name="license",
                kind="inline",
                description="Common open licenses",
                items=[
                    AutocompleteItem(
                        iri="https://creativecommons.org/licenses/by/4.0/",
                        label="Creative Commons Attribution 4.0",
                        aliases=["CC BY 4.0", "CC-BY-4.0"],
                    ),
                    AutocompleteItem(
                        iri="https://creativecommons.org/publicdomain/zero/1.0/",
                        label="Creative Commons CC0 1.0 Universal",
                        aliases=["CC0", "CC0 1.0"],
                    ),
                    AutocompleteItem(
                        iri="https://opensource.org/licenses/MIT",
                        label="MIT License",
                        aliases=["MIT"],
                    ),
                    AutocompleteItem(
                        iri="https://opensource.org/licenses/Apache-2.0",
                        label="Apache License 2.0",
                        aliases=["Apache 2.0", "Apache-2.0"],
                    ),
                    AutocompleteItem(
                        iri="https://opensource.org/licenses/BSD-3-Clause",
                        label="BSD 3-Clause License",
                        aliases=["BSD-3-Clause"],
                    ),
                ],
            ),
            AutocompleteSource(
                name="mime-type",
                kind="inline",
                description="Common distribution media types",
                items=[
                    AutocompleteItem(
                        iri="https://www.iana.org/assignments/media-types/text/csv",
                        label="text/csv",
                    ),
                    AutocompleteItem(
                        iri="https://www.iana.org/assignments/media-types/application/json",
                        label="application/json",
                    ),
                    AutocompleteItem(
                        iri="https://www.iana.org/assignments/media-types/text/turtle",
                        label="text/turtle",
                    ),
                    AutocompleteItem(
                        iri="https://www.iana.org/assignments/media-types/application/ld+json",
                        label="application/ld+json",
                    ),
                    AutocompleteItem(
                        iri="https://www.iana.org/assignments/media-types/application/x-parquet",
                        label="application/x-parquet",
                    ),
                ],
            ),
            AutocompleteSource(
                name="publisher",
                kind="sparql",
                description="Organizations known to this FDP",
                sparql=(
                    "SELECT ?iri ?label WHERE {\n"
                    "  GRAPH ?g {\n"
                    "    ?iri a <http://xmlns.com/foaf/0.1/Organization> .\n"
                    "    { ?iri <http://xmlns.com/foaf/0.1/name> ?label }\n"
                    "    UNION\n"
                    "    { ?iri <http://www.w3.org/2000/01/rdf-schema#label> ?label }\n"
                    "  }\n"
                    "  FILTER(isLiteral(?label))\n"
                    "}\n"
                ),
            ),
        ]
    )


# --- registry ------------------------------------------------------------


class _RegisteredKey(BaseModel):
    """One entry in the settings registry."""

    model_config = {"arbitrary_types_allowed": True}

    model_class: type[BaseModel]
    default: BaseModel


def _registry() -> dict[str, _RegisteredKey]:
    return {
        "forms.autocomplete-sources": _RegisteredKey(
            model_class=AutocompleteSources,
            default=_default_autocomplete_sources(),
        ),
        "search.filters": _RegisteredKey(
            model_class=SearchFilters,
            default=SearchFilters(),
        ),
    }


SETTINGS_REGISTRY: Final = _registry()


# --- storage --------------------------------------------------------------


class _AwareDateTime(TypeDecorator[datetime]):
    """``DateTime(timezone=True)`` that coerces naive results to UTC.

    The Postgres column stores tz-aware timestamps; SQLite (used in unit
    tests) returns naive datetimes for the same DDL. This decorator
    keeps a stable shape across backends.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        del dialect
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


class RuntimeSettingRow(Base):
    """One row per registered settings key."""

    __tablename__ = "runtime_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    # SQLite (used in unit tests) doesn't support JSONB; the variant
    # falls back to plain JSON there. The runtime application is
    # Postgres-only.
    value_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=False
    )
    updated_by: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        _AwareDateTime(),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )


class SettingsRepository:
    """Async CRUD over ``runtime_settings``.

    Validates every read against the registered Pydantic schema for the
    key. Unknown keys raise ``NotFound`` at the router boundary; the
    repository itself returns ``None`` so it can also be used to drive
    the "key has no override" branch.
    """

    def __init__(self, *, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        """Expose the session factory for collaborators that need their own session.

        The factory-reset service (Phase 10.4) shares this repository's
        Postgres session factory so its profile re-apply runs against the same
        engine as the settings truncation.
        """
        return self._session_factory

    async def read(self, key: str) -> BaseModel | None:
        registered = SETTINGS_REGISTRY.get(key)
        if registered is None:
            return None
        async with self._session_factory() as session:
            row = await self._fetch(session, key)
        if row is None:
            return None
        try:
            return registered.model_class.model_validate(row.value_json)
        except ValidationError:
            # Row's JSON has drifted from the schema. Surface as if the
            # override didn't exist; the operator should fix the row.
            log.warning(
                "settings_row_invalid_against_schema",
                key=key,
                value=row.value_json,
            )
            return None

    async def read_with_default(self, key: str) -> BaseModel:
        """Like :meth:`read` but falls back to the registered default."""
        registered = SETTINGS_REGISTRY[key]
        value = await self.read(key)
        return value if value is not None else registered.default

    async def read_all(self) -> dict[str, BaseModel]:
        """Return every registered key with its current effective value.

        Effective = override if present, else registered default. The
        client uses this to render a single bootstrap payload.
        """
        async with self._session_factory() as session:
            stmt = select(RuntimeSettingRow)
            rows = (await session.execute(stmt)).scalars().all()
        overrides: dict[str, dict[str, Any]] = {row.key: row.value_json for row in rows}
        result: dict[str, BaseModel] = {}
        for key, registered in SETTINGS_REGISTRY.items():
            raw = overrides.get(key)
            if raw is None:
                result[key] = registered.default
                continue
            try:
                result[key] = registered.model_class.model_validate(raw)
            except ValidationError:
                log.warning(
                    "settings_row_invalid_against_schema",
                    key=key,
                    value=raw,
                )
                result[key] = registered.default
        return result

    async def write(self, key: str, value: BaseModel, *, subject: str | None) -> None:
        """Upsert ``key`` with ``value``. ``value`` must match the registered schema.

        The repository accepts a *validated* model instance — the router
        is the place where caller-supplied JSON gets validated.
        """
        registered = SETTINGS_REGISTRY.get(key)
        if registered is None:
            raise NotFound(f"unknown settings key: {key}")
        if not isinstance(value, registered.model_class):
            raise BadRequest(
                f"value type mismatch for {key}",
                details={"expected": registered.model_class.__name__},
            )
        payload = value.model_dump(mode="json")
        now = datetime.now(UTC)
        async with self._session_factory() as session:
            row = await self._fetch(session, key)
            if row is None:
                session.add(
                    RuntimeSettingRow(
                        key=key,
                        value_json=payload,
                        updated_by=subject,
                        updated_at=now,
                    )
                )
            else:
                row.value_json = payload
                row.updated_by = subject
                row.updated_at = now
            await session.commit()
        log.info("settings_updated", key=key, subject=subject)

    async def delete(self, key: str) -> bool:
        """Remove the override for ``key`` so the default takes over again.

        Returns True if a row was removed. Unknown keys raise NotFound
        (the API surface is consistent: PUT and DELETE both 404 for
        unregistered keys).
        """
        if key not in SETTINGS_REGISTRY:
            raise NotFound(f"unknown settings key: {key}")
        async with self._session_factory() as session:
            result = cast(
                "CursorResult[Any]",
                await session.execute(
                    delete(RuntimeSettingRow).where(RuntimeSettingRow.key == key)
                ),
            )
            await session.commit()
        removed = (result.rowcount or 0) > 0
        if removed:
            log.info("settings_deleted", key=key)
        return removed

    async def clear_all(self, *, subject: str | None = None) -> int:
        """Truncate every runtime override so all keys revert to their default.

        Used by the factory-reset flow (Phase 10.4). Returns the number of
        override rows removed. Audit-logged via structlog.
        """
        async with self._session_factory() as session:
            result = cast("CursorResult[Any]", await session.execute(delete(RuntimeSettingRow)))
            await session.commit()
        removed = result.rowcount or 0
        log.info("settings_cleared_all", removed=removed, subject=subject)
        return removed

    @staticmethod
    async def _fetch(session: AsyncSession, key: str) -> RuntimeSettingRow | None:
        stmt = select(RuntimeSettingRow).where(RuntimeSettingRow.key == key)
        return (await session.execute(stmt)).scalar_one_or_none()


# --- response models ------------------------------------------------------


class SettingsResponse(BaseModel):
    """Shape returned by ``GET /settings``.

    The settings are wrapped in a top-level ``values`` dict keyed by
    registered key so adding a new key doesn't break clients that
    iterate.
    """

    values: dict[str, dict[str, Any]]


class SettingsValueResponse(BaseModel):
    """Shape returned by ``GET /settings/{key}``."""

    key: str
    value: dict[str, Any]


# --- router --------------------------------------------------------------


def build_settings_router(*, repository: SettingsRepository) -> APIRouter:
    """Construct the runtime-settings router.

    Auth model:
    * Reads are public (the client needs them before login).
    * Writes (``PUT`` / ``DELETE``) require the admin role.
    """
    router = APIRouter(tags=["settings"])

    @router.get(
        "/settings",
        response_model=SettingsResponse,
        name="settings_read_all",
    )
    async def read_all() -> SettingsResponse:  # pyright: ignore[reportUnusedFunction]
        merged = await repository.read_all()
        return SettingsResponse(
            values={key: model.model_dump(mode="json") for key, model in merged.items()},
        )

    @router.get(
        "/settings/{key}",
        response_model=SettingsValueResponse,
        name="settings_read_one",
    )
    async def read_one(key: str) -> SettingsValueResponse:  # pyright: ignore[reportUnusedFunction]
        if key not in SETTINGS_REGISTRY:
            raise NotFound(f"unknown settings key: {key}")
        value = await repository.read_with_default(key)
        return SettingsValueResponse(key=key, value=value.model_dump(mode="json"))

    @router.put(
        "/settings/{key}",
        response_model=SettingsValueResponse,
        name="settings_write_one",
    )
    async def write_one(  # pyright: ignore[reportUnusedFunction]
        key: str,
        body: dict[str, Any],
        ctx: Annotated[RequestContext, Depends(require_auth)],
    ) -> SettingsValueResponse:
        if _ADMIN_ROLE not in ctx.roles:
            raise Forbidden(
                "admin role required to modify settings",
                details={"required_role": _ADMIN_ROLE},
            )
        registered = SETTINGS_REGISTRY.get(key)
        if registered is None:
            raise NotFound(f"unknown settings key: {key}")
        try:
            value = registered.model_class.model_validate(body)
        except ValidationError as err:
            raise BadRequest(
                f"invalid value for settings key '{key}'",
                details={"errors": err.errors()},
            ) from err
        await repository.write(key, value, subject=ctx.subject)
        return SettingsValueResponse(key=key, value=value.model_dump(mode="json"))

    @router.delete(
        "/settings/{key}",
        status_code=204,
        name="settings_delete_one",
    )
    async def delete_one(  # pyright: ignore[reportUnusedFunction]
        key: str,
        ctx: Annotated[RequestContext, Depends(require_auth)],
    ) -> None:
        if _ADMIN_ROLE not in ctx.roles:
            raise Forbidden(
                "admin role required to modify settings",
                details={"required_role": _ADMIN_ROLE},
            )
        if key not in SETTINGS_REGISTRY:
            raise NotFound(f"unknown settings key: {key}")
        await repository.delete(key)

    return router


__all__ = [
    "SETTINGS_REGISTRY",
    "AutocompleteItem",
    "AutocompleteSource",
    "AutocompleteSources",
    "RuntimeSettingRow",
    "SearchFilter",
    "SearchFilters",
    "SettingsRepository",
    "SettingsResponse",
    "SettingsValueResponse",
    "build_settings_router",
]
