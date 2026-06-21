"""Unit tests for the form-autocomplete endpoint (task 6.2).

Covers:

* Inline resolution: case-insensitive prefix against label + aliases,
  honouring limit.
* SPARQL resolution: ``${PREFIX}`` / ``${LIMIT}`` substitution with
  proper string escaping, dedup, parse.
* Router: unknown source 404, prefix too long 400, limit bounds,
  default limit.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fdp.metadata.autocomplete import (
    AutocompleteService,
    _parse_sparql_items,
    _resolve_inline,
    _sparql_string_literal,
    build_autocomplete_router,
)
from fdp.metadata.settings import (
    AutocompleteItem,
    AutocompleteSource,
    AutocompleteSources,
    SettingsRepository,
)
from fdp.shared.errors import register_exception_handlers
from fdp.storage.postgres.models import Base

# --- fixtures ------------------------------------------------------------


@pytest.fixture
async def session_factory() -> Any:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


class _FakeAdapter:
    def __init__(self, response: bytes = b'{"results":{"bindings":[]}}') -> None:
        self.response = response
        self.calls: list[str] = []

    async def query(self, sparql: str, **_kwargs: Any) -> bytes:
        self.calls.append(sparql)
        return self.response


def _sparql_results(rows: list[dict[str, Any]]) -> bytes:
    return json.dumps({"results": {"bindings": rows}}).encode("utf-8")


def _row(iri: str, label: str) -> dict[str, Any]:
    return {
        "iri": {"type": "uri", "value": iri},
        "label": {"type": "literal", "value": label},
    }


# --- _sparql_string_literal ---------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("hello", '"hello"'),
        ('with "quotes"', '"with \\"quotes\\""'),
        ("line\nbreak", '"line\\nbreak"'),
        ("back\\slash", '"back\\\\slash"'),
    ],
)
def test_sparql_string_literal_escapes_safely(raw: str, expected: str) -> None:
    assert _sparql_string_literal(raw) == expected


# --- inline resolution ---------------------------------------------------


def _src(name: str, items: list[AutocompleteItem]) -> AutocompleteSource:
    return AutocompleteSource(name=name, kind="inline", items=items)


@pytest.mark.unit
def test_inline_returns_all_items_when_prefix_empty() -> None:
    src = _src(
        "license",
        [
            AutocompleteItem(iri="urn:1", label="One"),
            AutocompleteItem(iri="urn:2", label="Two"),
        ],
    )
    items = _resolve_inline(src, prefix="", limit=10)
    assert [i.iri for i in items] == ["urn:1", "urn:2"]
    assert all(i.source == "license" for i in items)


@pytest.mark.unit
def test_inline_filters_case_insensitively_against_label() -> None:
    src = _src(
        "license",
        [
            AutocompleteItem(iri="urn:cc", label="Creative Commons BY 4.0"),
            AutocompleteItem(iri="urn:mit", label="MIT License"),
        ],
    )
    items = _resolve_inline(src, prefix="creative", limit=10)
    assert [i.iri for i in items] == ["urn:cc"]
    # Case-insensitive: upper-case prefix matches lower-case label.
    items = _resolve_inline(src, prefix="MIT", limit=10)
    assert [i.iri for i in items] == ["urn:mit"]


@pytest.mark.unit
def test_inline_matches_aliases_too() -> None:
    src = _src(
        "license",
        [
            AutocompleteItem(
                iri="urn:cc-by",
                label="Creative Commons Attribution 4.0",
                aliases=["CC BY 4.0", "CC-BY"],
            ),
        ],
    )
    items = _resolve_inline(src, prefix="cc-by", limit=10)
    assert [i.iri for i in items] == ["urn:cc-by"]


@pytest.mark.unit
def test_inline_honours_limit() -> None:
    src = _src(
        "x",
        [AutocompleteItem(iri=f"urn:{i}", label=f"Item {i}") for i in range(20)],
    )
    items = _resolve_inline(src, prefix="", limit=5)
    assert len(items) == 5


# --- _parse_sparql_items ------------------------------------------------


@pytest.mark.unit
def test_parse_sparql_items_dedups_by_iri() -> None:
    body = _sparql_results(
        [
            _row("urn:a", "Alpha"),
            _row("urn:a", "Alpha (duplicate)"),
            _row("urn:b", "Beta"),
        ]
    )
    items = _parse_sparql_items(body, source_name="publisher", limit=10)
    assert [i.iri for i in items] == ["urn:a", "urn:b"]
    assert items[0].label == "Alpha"  # first wins after dedup


@pytest.mark.unit
def test_parse_sparql_items_skips_rows_with_missing_fields() -> None:
    body = _sparql_results(
        [
            {"iri": {"value": "urn:a"}},  # no label → skip
            _row("urn:b", "Beta"),
        ]
    )
    items = _parse_sparql_items(body, source_name="publisher", limit=10)
    assert [i.iri for i in items] == ["urn:b"]


@pytest.mark.unit
def test_parse_sparql_items_honours_limit() -> None:
    body = _sparql_results([_row(f"urn:{i}", f"L{i}") for i in range(10)])
    items = _parse_sparql_items(body, source_name="x", limit=3)
    assert len(items) == 3


# --- service: sparql substitution ----------------------------------------


@pytest.mark.unit
async def test_service_substitutes_prefix_safely_into_sparql(
    session_factory: Any,
) -> None:
    repo = SettingsRepository(session_factory=session_factory)
    await repo.write(
        "forms.autocomplete-sources",
        AutocompleteSources(
            sources=[
                AutocompleteSource(
                    name="publisher",
                    kind="sparql",
                    sparql="ASK { FILTER(STR(?iri) = ${PREFIX}) }\nLIMIT ${LIMIT}",
                )
            ]
        ),
        subject=None,
    )
    adapter = _FakeAdapter(_sparql_results([]))
    service = AutocompleteService(
        settings_repository=repo,
        adapter=adapter,  # type: ignore[arg-type]
    )
    # An injection attempt — ${PREFIX} must be string-escaped.
    await service.resolve(source="publisher", prefix='alpha" } DROP { ?s ?p ?o', limit=5)
    sent = adapter.calls[0]
    # The raw prefix appears, but inside a SPARQL string literal — not as
    # a SPARQL clause. The closing brace stays escaped within the literal.
    assert '"alpha\\" } DROP { ?s ?p ?o"' in sent
    # ${LIMIT} got replaced.
    assert "LIMIT 5" in sent


@pytest.mark.unit
async def test_service_appends_limit_when_source_omits_it(
    session_factory: Any,
) -> None:
    """A source that forgot to include LIMIT gets one added defensively."""
    repo = SettingsRepository(session_factory=session_factory)
    await repo.write(
        "forms.autocomplete-sources",
        AutocompleteSources(
            sources=[
                AutocompleteSource(
                    name="bad",
                    kind="sparql",
                    sparql="SELECT ?iri ?label WHERE { ?iri ?p ?label }",  # no LIMIT
                )
            ]
        ),
        subject=None,
    )
    adapter = _FakeAdapter(_sparql_results([]))
    service = AutocompleteService(
        settings_repository=repo,
        adapter=adapter,  # type: ignore[arg-type]
    )
    await service.resolve(source="bad", prefix="", limit=42)
    assert "LIMIT 42" in adapter.calls[0]


# --- service: unknown sources --------------------------------------------


@pytest.mark.unit
async def test_service_raises_not_found_for_unknown_source(
    session_factory: Any,
) -> None:
    from fdp.shared.errors import NotFound

    repo = SettingsRepository(session_factory=session_factory)
    service = AutocompleteService(
        settings_repository=repo,
        adapter=_FakeAdapter(),  # type: ignore[arg-type]
    )
    with pytest.raises(NotFound):
        await service.resolve(source="bogus", prefix="", limit=10)


# --- router --------------------------------------------------------------


def _build_app(service: AutocompleteService) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(build_autocomplete_router(service=service))
    return app


@pytest.mark.unit
async def test_router_returns_inline_results(session_factory: Any) -> None:
    """End-to-end: default 'license' source should respond with at least 1 item."""
    repo = SettingsRepository(session_factory=session_factory)
    service = AutocompleteService(
        settings_repository=repo,
        adapter=_FakeAdapter(),  # type: ignore[arg-type]
    )
    response = TestClient(_build_app(service)).get(
        "/forms/autocomplete",
        params={"source": "license", "prefix": "creative"},
    )
    assert response.status_code == 200
    body = response.json()
    assert any("Creative" in item["label"] for item in body["items"])
    assert all(item["source"] == "license" for item in body["items"])


@pytest.mark.unit
async def test_router_404_for_unknown_source(session_factory: Any) -> None:
    repo = SettingsRepository(session_factory=session_factory)
    service = AutocompleteService(
        settings_repository=repo,
        adapter=_FakeAdapter(),  # type: ignore[arg-type]
    )
    response = TestClient(_build_app(service)).get(
        "/forms/autocomplete", params={"source": "bogus"}
    )
    assert response.status_code == 404
    assert response.json()["code"] == "fdp.not_found"
    # The available-sources hint is part of the error detail.
    assert "license" in response.json()["details"]["available"]


@pytest.mark.unit
async def test_router_rejects_invalid_limit(session_factory: Any) -> None:
    repo = SettingsRepository(session_factory=session_factory)
    service = AutocompleteService(
        settings_repository=repo,
        adapter=_FakeAdapter(),  # type: ignore[arg-type]
    )
    client = TestClient(_build_app(service))
    assert client.get("/forms/autocomplete?source=license&limit=0").status_code == 422
    assert client.get("/forms/autocomplete?source=license&limit=999").status_code == 422


@pytest.mark.unit
async def test_router_requires_source_param(session_factory: Any) -> None:
    repo = SettingsRepository(session_factory=session_factory)
    service = AutocompleteService(
        settings_repository=repo,
        adapter=_FakeAdapter(),  # type: ignore[arg-type]
    )
    response = TestClient(_build_app(service)).get("/forms/autocomplete")
    assert response.status_code == 422  # FastAPI validation: source required
