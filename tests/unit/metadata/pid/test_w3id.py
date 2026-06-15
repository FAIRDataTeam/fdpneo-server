"""Tests for ``fdp.metadata.pid.w3id`` — redirect config generation."""

from __future__ import annotations

import pytest

from fdp.metadata.pid.w3id import build_w3id_config, w3id_prefix_from


class TestPrefixDerivation:
    def test_w3id_url(self) -> None:
        assert w3id_prefix_from("https://w3id.org/myfdp") == "myfdp"
        assert w3id_prefix_from("https://w3id.org/org/myfdp/") == "org/myfdp"

    def test_non_w3id_url(self) -> None:
        assert w3id_prefix_from("https://fdp.example.org") is None
        assert w3id_prefix_from("https://w3id.org") is None  # no prefix


class TestBuildConfig:
    def test_derives_prefix_and_target(self) -> None:
        cfg = build_w3id_config(
            identifier_base="https://w3id.org/myfdp",
            serving_base="https://fdp.example.org/",
        )
        assert cfg.prefix == "myfdp"
        assert cfg.target == "https://fdp.example.org"  # trailing slash stripped
        assert cfg.path == "myfdp/.htaccess"
        assert "RewriteEngine on" in cfg.htaccess
        assert "https://fdp.example.org/$1 [R=302,L,NE]" in cfg.htaccess
        assert "w3id.org/myfdp" in cfg.readme

    def test_explicit_prefix_overrides(self) -> None:
        cfg = build_w3id_config(
            identifier_base="https://example.org/notw3id",
            serving_base="https://fdp.example.org",
            prefix="custom",
        )
        assert cfg.prefix == "custom"
        assert cfg.path == "custom/.htaccess"

    def test_missing_prefix_raises(self) -> None:
        with pytest.raises(ValueError, match="W3ID prefix"):
            build_w3id_config(
                identifier_base="https://fdp.example.org",
                serving_base="https://fdp.example.org",
            )
