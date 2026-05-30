"""Unit tests for the labels endpoint (task 6.1).

Covers:

* IRI validation (``is_safe_iri``).
* SPARQL JSON parsing and the language/predicate preference scoring
  (:func:`_pick_best_labels`).
* The TTL cache: positive hits, negative hits, expiry.
* The router: batched lookup, ``?lang=`` handling, missing IRIs omitted,
  malformed IRIs silently dropped, ``max_iris_per_request`` enforcement.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fdp.metadata.labels import (
    LabelResolver,
    _LabelCache,
    _pick_best_labels,
    build_labels_router,
    is_safe_iri,
)
from fdp.shared.errors import register_exception_handlers

# --- is_safe_iri ----------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "iri",
    [
        "https://creativecommons.org/licenses/by/4.0/",
        "http://www.w3.org/ns/dcat#Catalog",
        "urn:isbn:9780262032933",
        "https://example.org/path?query=value&other=1",
    ],
)
def test_safe_iri_accepts_legal_iris(iri: str) -> None:
    assert is_safe_iri(iri) is True


@pytest.mark.unit
@pytest.mark.parametrize(
    "iri",
    [
        "",
        "has space",
        "has\ttab",
        "has\nnewline",
        "has<bracket",
        "has>bracket",
        'has"quote',
        "has{brace",
        "has|pipe",
        "x" * 2049,  # too long
    ],
)
def test_safe_iri_rejects_malformed(iri: str) -> None:
    assert is_safe_iri(iri) is False


# --- _pick_best_labels ----------------------------------------------------


def _sparql_response(rows: list[dict[str, Any]]) -> bytes:
    return json.dumps(
        {"head": {"vars": ["iri", "p", "label"]}, "results": {"bindings": rows}}
    ).encode("utf-8")


def _row(
    iri: str, predicate: str, label: str, *, lang: str | None = None
) -> dict[str, Any]:
    label_term: dict[str, Any] = {"type": "literal", "value": label}
    if lang is not None:
        label_term["xml:lang"] = lang
    return {
        "iri": {"type": "uri", "value": iri},
        "p": {"type": "uri", "value": predicate},
        "label": label_term,
    }


_RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
_SKOS_PREF = "http://www.w3.org/2004/02/skos/core#prefLabel"
_DCT_TITLE = "http://purl.org/dc/terms/title"


@pytest.mark.unit
def test_pick_best_returns_single_label_when_one_row() -> None:
    body = _sparql_response(
        [_row("urn:test", _RDFS_LABEL, "Hello", lang="en")]
    )
    result = _pick_best_labels(body, requested_language="en")
    assert result == {"urn:test": "Hello"}


@pytest.mark.unit
def test_pick_best_prefers_requested_language() -> None:
    body = _sparql_response(
        [
            _row("urn:test", _RDFS_LABEL, "Hola", lang="es"),
            _row("urn:test", _RDFS_LABEL, "Hello", lang="en"),
        ]
    )
    result = _pick_best_labels(body, requested_language="en")
    assert result == {"urn:test": "Hello"}


@pytest.mark.unit
def test_pick_best_falls_back_to_no_language_tag_when_lang_missing() -> None:
    body = _sparql_response(
        [
            _row("urn:test", _RDFS_LABEL, "Hola", lang="es"),
            _row("urn:test", _RDFS_LABEL, "Untagged"),  # no lang tag
        ]
    )
    result = _pick_best_labels(body, requested_language="en")
    assert result == {"urn:test": "Untagged"}


@pytest.mark.unit
def test_pick_best_falls_back_to_any_language_last() -> None:
    body = _sparql_response(
        [_row("urn:test", _RDFS_LABEL, "Hola", lang="es")]
    )
    result = _pick_best_labels(body, requested_language="en")
    assert result == {"urn:test": "Hola"}


@pytest.mark.unit
def test_pick_best_prefers_rdfs_label_over_other_predicates() -> None:
    body = _sparql_response(
        [
            _row("urn:test", _DCT_TITLE, "From title", lang="en"),
            _row("urn:test", _SKOS_PREF, "From skos", lang="en"),
            _row("urn:test", _RDFS_LABEL, "From rdfs", lang="en"),
        ]
    )
    result = _pick_best_labels(body, requested_language="en")
    assert result == {"urn:test": "From rdfs"}


@pytest.mark.unit
def test_pick_best_language_score_beats_predicate_score() -> None:
    """An en label under dct:title beats an es label under rdfs:label."""
    body = _sparql_response(
        [
            _row("urn:test", _RDFS_LABEL, "Hola", lang="es"),
            _row("urn:test", _DCT_TITLE, "Hello", lang="en"),
        ]
    )
    result = _pick_best_labels(body, requested_language="en")
    assert result == {"urn:test": "Hello"}


@pytest.mark.unit
def test_pick_best_handles_empty_results() -> None:
    assert _pick_best_labels(_sparql_response([]), requested_language="en") == {}


# --- _LabelCache -----------------------------------------------------------


@pytest.mark.unit
def test_cache_returns_miss_for_unseen_key() -> None:
    cache = _LabelCache(ttl_seconds=60)
    from fdp.metadata.labels import _MISS, _Sentinel

    result = cache.get("urn:test", "en")
    assert isinstance(result, _Sentinel)
    assert result is _MISS


@pytest.mark.unit
def test_cache_stores_positive_hit() -> None:
    cache = _LabelCache(ttl_seconds=60)
    cache.set("urn:test", "en", "Hello")
    assert cache.get("urn:test", "en") == "Hello"


@pytest.mark.unit
def test_cache_stores_negative_hit_distinct_from_miss() -> None:
    cache = _LabelCache(ttl_seconds=60)
    from fdp.metadata.labels import _Sentinel

    cache.set("urn:test", "en", None)
    result = cache.get("urn:test", "en")
    assert result is None
    assert not isinstance(result, _Sentinel)


@pytest.mark.unit
def test_cache_expires_after_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = _LabelCache(ttl_seconds=10)
    base = time.monotonic()
    # Freeze the clock at base + 0, write, then advance past TTL.
    clock = [base]
    monkeypatch.setattr(
        "fdp.metadata.labels.time.monotonic", lambda: clock[0]
    )
    cache.set("urn:test", "en", "Hello")
    assert cache.get("urn:test", "en") == "Hello"
    clock[0] = base + 20  # past TTL
    from fdp.metadata.labels import _Sentinel

    result = cache.get("urn:test", "en")
    assert isinstance(result, _Sentinel)


@pytest.mark.unit
def test_cache_keys_per_language() -> None:
    cache = _LabelCache(ttl_seconds=60)
    cache.set("urn:test", "en", "Hello")
    cache.set("urn:test", "es", "Hola")
    assert cache.get("urn:test", "en") == "Hello"
    assert cache.get("urn:test", "es") == "Hola"


# --- LabelResolver --------------------------------------------------------


class _FakeAdapter:
    """``TripleStoreAdapter`` stand-in returning a configured SPARQL JSON body."""

    def __init__(self, response: bytes) -> None:
        self.response = response
        self.calls: list[str] = []

    async def query(self, sparql: str, **_kwargs: Any) -> bytes:
        self.calls.append(sparql)
        return self.response


@pytest.mark.unit
async def test_resolver_returns_labels_for_iris_with_data() -> None:
    body = _sparql_response(
        [
            _row("urn:a", _RDFS_LABEL, "Alpha", lang="en"),
            _row("urn:b", _RDFS_LABEL, "Beta", lang="en"),
        ]
    )
    adapter = _FakeAdapter(body)
    resolver = LabelResolver(adapter=adapter)  # type: ignore[arg-type]
    result = await resolver.lookup(["urn:a", "urn:b", "urn:nolabel"], language="en")
    assert result == {"urn:a": "Alpha", "urn:b": "Beta"}


@pytest.mark.unit
async def test_resolver_caches_positive_results_to_skip_requery() -> None:
    body = _sparql_response([_row("urn:a", _RDFS_LABEL, "Alpha", lang="en")])
    adapter = _FakeAdapter(body)
    resolver = LabelResolver(adapter=adapter)  # type: ignore[arg-type]
    await resolver.lookup(["urn:a"], language="en")
    await resolver.lookup(["urn:a"], language="en")
    # Second call must not re-query — single SPARQL call to the adapter.
    assert len(adapter.calls) == 1


@pytest.mark.unit
async def test_resolver_caches_negative_results_to_skip_requery() -> None:
    """A previously-missing IRI must not re-query for the cache lifetime."""
    body = _sparql_response([])  # no labels at all
    adapter = _FakeAdapter(body)
    resolver = LabelResolver(adapter=adapter)  # type: ignore[arg-type]
    await resolver.lookup(["urn:missing"], language="en")
    await resolver.lookup(["urn:missing"], language="en")
    assert len(adapter.calls) == 1


@pytest.mark.unit
async def test_resolver_chunks_large_batches() -> None:
    """A batch over max_iris_per_query splits into multiple SPARQL calls."""
    body = _sparql_response([])
    adapter = _FakeAdapter(body)
    resolver = LabelResolver(adapter=adapter, max_iris_per_query=3)  # type: ignore[arg-type]
    iris = [f"urn:{i}" for i in range(7)]
    await resolver.lookup(iris, language="en")
    # 7 IRIs with batch size 3 → 3 calls (3 + 3 + 1).
    assert len(adapter.calls) == 3


# --- router ---------------------------------------------------------------


def _build_app(adapter: _FakeAdapter, *, max_iris: int = 100) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    resolver = LabelResolver(adapter=adapter)  # type: ignore[arg-type]
    app.include_router(
        build_labels_router(resolver=resolver, max_iris_per_request=max_iris)
    )
    return app


@pytest.mark.unit
def test_router_returns_labels_in_response_envelope() -> None:
    body = _sparql_response(
        [_row("urn:test", _RDFS_LABEL, "Hello", lang="en")]
    )
    response = TestClient(_build_app(_FakeAdapter(body))).get(
        "/labels", params=[("iri", "urn:test")]
    )
    assert response.status_code == 200
    assert response.json() == {"labels": {"urn:test": "Hello"}}


@pytest.mark.unit
def test_router_accepts_multiple_iri_params() -> None:
    body = _sparql_response(
        [
            _row("urn:a", _RDFS_LABEL, "Alpha", lang="en"),
            _row("urn:b", _RDFS_LABEL, "Beta", lang="en"),
        ]
    )
    response = TestClient(_build_app(_FakeAdapter(body))).get(
        "/labels", params=[("iri", "urn:a"), ("iri", "urn:b")]
    )
    assert response.json() == {"labels": {"urn:a": "Alpha", "urn:b": "Beta"}}


@pytest.mark.unit
def test_router_omits_iris_with_no_label() -> None:
    body = _sparql_response([_row("urn:a", _RDFS_LABEL, "Alpha", lang="en")])
    response = TestClient(_build_app(_FakeAdapter(body))).get(
        "/labels", params=[("iri", "urn:a"), ("iri", "urn:unknown")]
    )
    assert response.json() == {"labels": {"urn:a": "Alpha"}}


@pytest.mark.unit
def test_router_silently_drops_malformed_iris() -> None:
    """Malformed IRIs should not 400; they're just absent from the response."""
    body = _sparql_response([_row("urn:good", _RDFS_LABEL, "OK", lang="en")])
    adapter = _FakeAdapter(body)
    response = TestClient(_build_app(adapter)).get(
        "/labels",
        params=[("iri", "urn:good"), ("iri", "has space"), ("iri", "has<bracket")],
    )
    assert response.status_code == 200
    assert response.json() == {"labels": {"urn:good": "OK"}}
    # Only the well-formed IRI made it into the SPARQL.
    assert "urn:good" in adapter.calls[0]
    assert "has space" not in adapter.calls[0]
    assert "<bracket" not in adapter.calls[0]


@pytest.mark.unit
def test_router_returns_empty_when_no_iri_param() -> None:
    response = TestClient(_build_app(_FakeAdapter(b""))).get("/labels", params=[("iri", "")])
    # Empty iri value is dropped by validation; result is empty.
    assert response.status_code == 200
    assert response.json() == {"labels": {}}


@pytest.mark.unit
def test_router_enforces_max_iris_per_request() -> None:
    app = _build_app(_FakeAdapter(b""), max_iris=3)
    response = TestClient(app).get(
        "/labels",
        params=[("iri", f"urn:{i}") for i in range(5)],
    )
    assert response.status_code == 400
    assert response.json()["code"] == "fdp.bad_request"


@pytest.mark.unit
def test_router_passes_lang_param_to_resolver() -> None:
    """When ``?lang=es`` is given, the Spanish label should win."""
    body = _sparql_response(
        [
            _row("urn:test", _RDFS_LABEL, "Hello", lang="en"),
            _row("urn:test", _RDFS_LABEL, "Hola", lang="es"),
        ]
    )
    response = TestClient(_build_app(_FakeAdapter(body))).get(
        "/labels", params=[("iri", "urn:test"), ("lang", "es")]
    )
    assert response.json() == {"labels": {"urn:test": "Hola"}}
