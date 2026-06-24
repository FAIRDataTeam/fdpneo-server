"""Unit tests for :mod:`fdp.shared.negotiation`."""

from __future__ import annotations

import pytest
from rdflib import Graph, Literal, URIRef

from fdp.shared.negotiation import (
    JSON_LD,
    N_TRIPLES,
    RDF_XML,
    TURTLE,
    normalize_content_type,
    parse,
    parse_accept,
    select_media_type,
    serialize,
)

EX = "https://example.org/"


def _sample_graph() -> Graph:
    g = Graph()
    g.add((URIRef(EX + "s"), URIRef(EX + "p"), Literal("v")))
    return g


@pytest.mark.unit
def test_parse_accept_returns_wildcard_when_header_missing() -> None:
    ranges = parse_accept(None)
    assert len(ranges) == 1
    assert ranges[0].media_type == "*/*"
    assert ranges[0].quality == 1.0


@pytest.mark.unit
def test_parse_accept_extracts_quality_values() -> None:
    ranges = parse_accept("text/turtle;q=0.9, application/ld+json;q=1.0, */*;q=0.1")
    by_type = {r.media_type: r.quality for r in ranges}
    assert by_type["text/turtle"] == 0.9
    assert by_type["application/ld+json"] == 1.0
    assert by_type["*/*"] == 0.1


@pytest.mark.unit
def test_select_returns_turtle_for_wildcard() -> None:
    assert select_media_type("*/*") == TURTLE
    assert select_media_type(None) == TURTLE


@pytest.mark.unit
def test_select_picks_highest_quality_supported_type() -> None:
    assert select_media_type("application/ld+json, text/turtle;q=0.5") == JSON_LD


@pytest.mark.unit
def test_select_returns_none_when_nothing_supported_matches() -> None:
    assert select_media_type("text/html, application/xml") is None


@pytest.mark.unit
def test_select_resolves_type_wildcards() -> None:
    # application/* should match the first supported application/* type — JSON-LD.
    assert select_media_type("application/*") == JSON_LD


@pytest.mark.unit
@pytest.mark.parametrize("media", [TURTLE, JSON_LD, RDF_XML, N_TRIPLES])
def test_serialize_then_parse_round_trips(media: str) -> None:
    original = _sample_graph()
    blob = serialize(original, media)
    parsed = parse(blob, media)
    assert (URIRef(EX + "s"), URIRef(EX + "p"), Literal("v")) in parsed


@pytest.mark.unit
def test_serialize_rejects_unsupported_media() -> None:
    with pytest.raises(ValueError, match="unsupported media type"):
        serialize(_sample_graph(), "text/csv")


@pytest.mark.unit
def test_parse_rejects_unsupported_media() -> None:
    with pytest.raises(ValueError, match="unsupported media type"):
        parse(b"x", "text/csv")


@pytest.mark.unit
def test_normalize_content_type_strips_parameters() -> None:
    assert normalize_content_type("text/turtle; charset=utf-8") == "text/turtle"
    assert normalize_content_type("APPLICATION/LD+JSON") == "application/ld+json"
    assert normalize_content_type(None) is None


# --- JSON-LD remote @context SSRF guard (audit F-01) ------------------------


_INLINE_JSONLD = (
    b'{"@context": {"title": "http://purl.org/dc/terms/title"},'
    b' "@id": "https://example.org/x", "title": "ok"}'
)


@pytest.mark.unit
def test_jsonld_inline_context_is_accepted() -> None:
    g = parse(_INLINE_JSONLD, JSON_LD, base="https://example.org/x")
    assert any("title" in str(p) for _, p, _ in g)


@pytest.mark.unit
@pytest.mark.parametrize(
    "body",
    [
        b'{"@context": "http://127.0.0.1:18080/evil", "@id": "", "x": 1}',  # remote string
        b'{"@context": ["http://evil/ctx", {"a": "b"}], "@id": ""}',  # remote in array
        b'{"@context": {"@import": "http://evil/ctx"}, "@id": ""}',  # remote @import
        b'{"nested": {"@context": "http://evil/ctx"}}',  # nested scoped context
    ],
)
def test_jsonld_remote_context_is_rejected(body: bytes) -> None:
    with pytest.raises(ValueError, match=r"SSRF|@context"):
        parse(body, JSON_LD, base="https://example.org/x")


@pytest.mark.unit
def test_jsonld_remote_context_rejected_before_any_parse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Prove the guard short-circuits *before* rdflib touches the network.
    import rdflib

    def _boom(*_a: object, **_k: object) -> None:
        raise AssertionError("graph.parse must not run for a remote @context")

    monkeypatch.setattr(rdflib.Graph, "parse", _boom)
    with pytest.raises(ValueError, match="SSRF"):
        parse(b'{"@context": "http://evil/ctx", "@id": ""}', JSON_LD)
