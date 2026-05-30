"""Unit tests for runtime settings (Phase 9.1 + 9.2 + 9.3 data shape).

Covers the repository (read/write/delete + default fallback +
schema-drift handling), the router (public reads, admin-only writes,
404 for unknown keys, 422 for invalid payloads), and the two seeded
registry keys (autocomplete sources + search filters).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fdp.identity.deps import current_context
from fdp.metadata.settings import (
    SETTINGS_REGISTRY,
    AutocompleteItem,
    AutocompleteSource,
    AutocompleteSources,
    RuntimeSettingRow,
    SearchFilter,
    SearchFilters,
    SettingsRepository,
    build_settings_router,
)
from fdp.shared.context import RequestContext
from fdp.shared.errors import register_exception_handlers
from fdp.storage.postgres.models import Base

# --- fixtures ------------------------------------------------------------


@pytest.fixture
async def session_factory() -> Any:
    """In-memory SQLite with the runtime_settings table created."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _ctx(*, roles: frozenset[str] = frozenset()) -> RequestContext:
    return RequestContext(
        subject="https://idp/alice",
        roles=roles,
        trace_id="t-1",
        request_timestamp=datetime(2026, 5, 30, 12, 0, tzinfo=UTC),
    )


def _build_app(
    repository: SettingsRepository, *, ctx: RequestContext | None = None
) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(build_settings_router(repository=repository))
    if ctx is not None:
        app.dependency_overrides[current_context] = lambda: ctx
    return app


# --- registry ------------------------------------------------------------


@pytest.mark.unit
def test_registry_seeded_with_expected_keys() -> None:
    assert set(SETTINGS_REGISTRY) == {
        "forms.autocomplete-sources",
        "search.filters",
    }


@pytest.mark.unit
def test_default_autocomplete_sources_include_license_publisher_mime() -> None:
    defaults = SETTINGS_REGISTRY["forms.autocomplete-sources"].default
    assert isinstance(defaults, AutocompleteSources)
    names = {s.name for s in defaults.sources}
    assert {"license", "mime-type", "publisher"}.issubset(names)


# --- repository ---------------------------------------------------------


@pytest.mark.unit
async def test_read_returns_none_when_no_override(session_factory: Any) -> None:
    repo = SettingsRepository(session_factory=session_factory)
    assert await repo.read("forms.autocomplete-sources") is None


@pytest.mark.unit
async def test_read_with_default_falls_back_to_registered_default(
    session_factory: Any,
) -> None:
    repo = SettingsRepository(session_factory=session_factory)
    value = await repo.read_with_default("forms.autocomplete-sources")
    assert isinstance(value, AutocompleteSources)
    # Default ships at least one inline source.
    assert any(s.kind == "inline" for s in value.sources)


@pytest.mark.unit
async def test_write_then_read_round_trips_typed_value(session_factory: Any) -> None:
    repo = SettingsRepository(session_factory=session_factory)
    override = AutocompleteSources(
        sources=[
            AutocompleteSource(
                name="custom",
                kind="inline",
                items=[AutocompleteItem(iri="urn:c", label="Custom")],
            )
        ]
    )
    await repo.write(
        "forms.autocomplete-sources", override, subject="https://idp/admin"
    )
    read_back = await repo.read("forms.autocomplete-sources")
    assert read_back == override


@pytest.mark.unit
async def test_write_upserts_in_place(session_factory: Any) -> None:
    """Two writes to the same key must not produce two rows."""
    repo = SettingsRepository(session_factory=session_factory)
    first = AutocompleteSources(sources=[])
    second = AutocompleteSources(
        sources=[AutocompleteSource(name="x", kind="inline")]
    )
    await repo.write("forms.autocomplete-sources", first, subject=None)
    await repo.write("forms.autocomplete-sources", second, subject=None)
    async with session_factory() as session:
        from sqlalchemy import select

        rows = (
            (await session.execute(select(RuntimeSettingRow))).scalars().all()
        )
    assert len(rows) == 1


@pytest.mark.unit
async def test_delete_removes_override(session_factory: Any) -> None:
    repo = SettingsRepository(session_factory=session_factory)
    await repo.write(
        "forms.autocomplete-sources",
        AutocompleteSources(sources=[]),
        subject=None,
    )
    removed = await repo.delete("forms.autocomplete-sources")
    assert removed is True
    assert await repo.read("forms.autocomplete-sources") is None
    # Subsequent delete is a no-op (returns False).
    assert await repo.delete("forms.autocomplete-sources") is False


@pytest.mark.unit
async def test_delete_unknown_key_raises_not_found(session_factory: Any) -> None:
    from fdp.shared.errors import NotFound

    repo = SettingsRepository(session_factory=session_factory)
    with pytest.raises(NotFound):
        await repo.delete("not.a.real.key")


@pytest.mark.unit
async def test_read_all_merges_overrides_with_defaults(
    session_factory: Any,
) -> None:
    repo = SettingsRepository(session_factory=session_factory)
    # Override only the autocomplete key — search.filters should
    # surface the default.
    override = AutocompleteSources(
        sources=[AutocompleteSource(name="custom", kind="inline")]
    )
    await repo.write("forms.autocomplete-sources", override, subject=None)
    merged = await repo.read_all()
    assert isinstance(merged["forms.autocomplete-sources"], AutocompleteSources)
    assert merged["forms.autocomplete-sources"].sources[0].name == "custom"
    assert isinstance(merged["search.filters"], SearchFilters)


@pytest.mark.unit
async def test_read_drops_silently_when_stored_json_violates_schema(
    session_factory: Any,
) -> None:
    """A row whose JSON has drifted from the schema must not crash reads."""
    # `sources` must be a list; a string is a structural type error
    # that AutocompleteSources will reject.
    async with session_factory() as session:
        session.add(
            RuntimeSettingRow(
                key="forms.autocomplete-sources",
                value_json={"sources": "not-a-list"},
                updated_by=None,
                updated_at=datetime.now(UTC),
            )
        )
        await session.commit()
    repo = SettingsRepository(session_factory=session_factory)
    # ``read`` returns None (treated like no override); ``read_with_default``
    # therefore falls back to the registered default.
    assert await repo.read("forms.autocomplete-sources") is None
    fallback = await repo.read_with_default("forms.autocomplete-sources")
    assert isinstance(fallback, AutocompleteSources)


# --- router ---------------------------------------------------------------


@pytest.mark.unit
async def test_get_settings_returns_all_keys_with_defaults(
    session_factory: Any,
) -> None:
    repo = SettingsRepository(session_factory=session_factory)
    app = _build_app(repo)
    body = TestClient(app).get("/settings").json()
    assert set(body["values"]) == {"forms.autocomplete-sources", "search.filters"}


@pytest.mark.unit
async def test_get_settings_key_returns_default_when_no_override(
    session_factory: Any,
) -> None:
    repo = SettingsRepository(session_factory=session_factory)
    app = _build_app(repo)
    body = TestClient(app).get("/settings/forms.autocomplete-sources").json()
    assert body["key"] == "forms.autocomplete-sources"
    names = [s["name"] for s in body["value"]["sources"]]
    assert "license" in names


@pytest.mark.unit
async def test_get_settings_key_404_for_unknown_key(
    session_factory: Any,
) -> None:
    repo = SettingsRepository(session_factory=session_factory)
    app = _build_app(repo)
    response = TestClient(app).get("/settings/not.a.real.key")
    assert response.status_code == 404
    assert response.json()["code"] == "fdp.not_found"


@pytest.mark.unit
async def test_put_settings_requires_admin_role(session_factory: Any) -> None:
    repo = SettingsRepository(session_factory=session_factory)
    app = _build_app(repo, ctx=_ctx(roles=frozenset({"fdp-steward"})))
    response = TestClient(app).put(
        "/settings/forms.autocomplete-sources", json={"sources": []}
    )
    assert response.status_code == 403
    assert response.json()["code"] == "fdp.forbidden"


@pytest.mark.unit
async def test_put_settings_validates_against_schema(session_factory: Any) -> None:
    repo = SettingsRepository(session_factory=session_factory)
    app = _build_app(repo, ctx=_ctx(roles=frozenset({"fdp-admin"})))
    response = TestClient(app).put(
        "/settings/forms.autocomplete-sources",
        json={"sources": [{"name": "x"}]},  # missing 'kind'
    )
    assert response.status_code == 400
    assert response.json()["code"] == "fdp.bad_request"


@pytest.mark.unit
async def test_put_settings_writes_when_admin(session_factory: Any) -> None:
    repo = SettingsRepository(session_factory=session_factory)
    app = _build_app(repo, ctx=_ctx(roles=frozenset({"fdp-admin"})))
    override = {
        "sources": [
            {
                "name": "custom",
                "kind": "inline",
                "items": [{"iri": "urn:c", "label": "Custom", "aliases": []}],
                "description": None,
                "sparql": None,
            }
        ]
    }
    response = TestClient(app).put(
        "/settings/forms.autocomplete-sources", json=override
    )
    assert response.status_code == 200
    persisted = await repo.read("forms.autocomplete-sources")
    assert isinstance(persisted, AutocompleteSources)
    assert persisted.sources[0].name == "custom"


@pytest.mark.unit
async def test_delete_settings_reverts_to_default(session_factory: Any) -> None:
    repo = SettingsRepository(session_factory=session_factory)
    # Seed an override first.
    await repo.write(
        "forms.autocomplete-sources",
        AutocompleteSources(sources=[AutocompleteSource(name="x", kind="inline")]),
        subject=None,
    )
    app = _build_app(repo, ctx=_ctx(roles=frozenset({"fdp-admin"})))
    response = TestClient(app).delete("/settings/forms.autocomplete-sources")
    assert response.status_code == 204
    # Next read sees the default's "license" source again.
    body = TestClient(_build_app(repo)).get("/settings/forms.autocomplete-sources").json()
    assert "license" in [s["name"] for s in body["value"]["sources"]]


@pytest.mark.unit
async def test_delete_settings_requires_admin(session_factory: Any) -> None:
    repo = SettingsRepository(session_factory=session_factory)
    app = _build_app(repo, ctx=_ctx(roles=frozenset({"fdp-steward"})))
    assert (
        TestClient(app)
        .delete("/settings/forms.autocomplete-sources")
        .status_code
        == 403
    )


@pytest.mark.unit
async def test_search_filters_schema_round_trip(session_factory: Any) -> None:
    """The 9.4 data shape lands here even though no endpoint consumes it yet."""
    repo = SettingsRepository(session_factory=session_factory)
    value = SearchFilters(
        filters=[
            SearchFilter(
                name="license",
                label="License",
                predicate="http://purl.org/dc/terms/license",
            )
        ]
    )
    await repo.write("search.filters", value, subject=None)
    read_back = await repo.read("search.filters")
    assert read_back == value
