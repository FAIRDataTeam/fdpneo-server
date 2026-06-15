"""Tests for ``fdp.shared.identifiers`` — PID canonicalization (ADR-0014)."""

from __future__ import annotations

import pytest

from fdp.shared.identifiers import canonicalize, is_under, relative_path

W3ID = "https://w3id.org/myfdp"
SERVING = "http://localhost:8000"


class TestCanonicalize:
    def test_dev_identity_when_bases_coincide(self) -> None:
        # identifier_base == serving origin → canonicalize is the identity.
        url = f"{SERVING}/catalog/abc"
        assert canonicalize(url, identifier_base=SERVING, serving_origins=[SERVING]) == url

    def test_maps_serving_path_under_identifier_base(self) -> None:
        url = f"{SERVING}/catalog/abc"
        assert (
            canonicalize(url, identifier_base=W3ID, serving_origins=[SERVING])
            == f"{W3ID}/catalog/abc"
        )

    def test_root_maps_to_identifier_base_exactly(self) -> None:
        for url in (f"{SERVING}/", SERVING):
            assert canonicalize(url, identifier_base=W3ID, serving_origins=[SERVING]) == W3ID

    def test_request_already_canonical_maps_to_itself(self) -> None:
        url = f"{W3ID}/catalog/abc"
        assert (
            canonicalize(url, identifier_base=W3ID, serving_origins=[SERVING])
            == f"{W3ID}/catalog/abc"
        )

    def test_query_and_fragment_are_dropped(self) -> None:
        url = f"{SERVING}/catalog/abc?foo=bar#frag"
        assert (
            canonicalize(url, identifier_base=W3ID, serving_origins=[SERVING])
            == f"{W3ID}/catalog/abc"
        )

    def test_unknown_host_falls_back_to_path(self) -> None:
        # A request on an unexpected origin still roots identity by path.
        url = "https://stray.example.org/catalog/abc"
        assert (
            canonicalize(url, identifier_base=W3ID, serving_origins=[SERVING])
            == f"{W3ID}/catalog/abc"
        )

    def test_subpath_deployment(self) -> None:
        serving = "https://example.org/fdp"
        url = f"{serving}/catalog/abc"
        assert (
            canonicalize(url, identifier_base=W3ID, serving_origins=[serving])
            == f"{W3ID}/catalog/abc"
        )

    def test_longest_base_wins(self) -> None:
        # Both the bare origin and a sub-path are candidates; the sub-path is the
        # real serving root and must win so the prefix isn't double-counted.
        bases = ["https://example.org", "https://example.org/fdp"]
        url = "https://example.org/fdp/catalog/abc"
        assert (
            canonicalize(url, identifier_base=W3ID, serving_origins=bases) == f"{W3ID}/catalog/abc"
        )

    def test_trailing_slash_on_identifier_base(self) -> None:
        url = f"{SERVING}/catalog/abc"
        assert (
            canonicalize(url, identifier_base=W3ID + "/", serving_origins=[SERVING])
            == f"{W3ID}/catalog/abc"
        )


class TestIsUnder:
    @pytest.mark.parametrize(
        ("iri", "expected"),
        [
            (W3ID, True),
            (W3ID + "/", True),
            (f"{W3ID}/catalog/abc", True),
            ("https://w3id.org/other/abc", False),
            ("https://doi.org/10.1234/foo", False),
            ("https://w3id.org/myfdpXX/abc", False),  # boundary: not a path child
        ],
    )
    def test_membership(self, iri: str, expected: bool) -> None:
        assert is_under(iri, W3ID) is expected


class TestRelativePath:
    def test_root(self) -> None:
        assert relative_path(SERVING, [SERVING]) == "/"
        assert relative_path(SERVING + "/", [SERVING]) == "/"

    def test_nested(self) -> None:
        assert relative_path(f"{SERVING}/a/b", [SERVING]) == "/a/b"
