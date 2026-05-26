"""Unit tests for :mod:`fdp.metrics.geo`."""

from __future__ import annotations

from pathlib import Path

import pytest

from fdp.metrics.geo import GeoResult, NullGeoLookup, open_geo_lookup


@pytest.mark.unit
def test_null_lookup_returns_empty_for_any_ip() -> None:
    lookup = NullGeoLookup()
    result = lookup.lookup("203.0.113.5")
    assert result == GeoResult(None, None, None)


@pytest.mark.unit
def test_null_lookup_returns_empty_for_none() -> None:
    assert NullGeoLookup().lookup(None) == GeoResult(None, None, None)


@pytest.mark.unit
def test_null_lookup_close_is_a_noop() -> None:
    NullGeoLookup().close()  # must not raise


@pytest.mark.unit
def test_open_geo_lookup_falls_back_when_path_missing(tmp_path: Path) -> None:
    missing = tmp_path / "nope.mmdb"
    lookup = open_geo_lookup(missing)
    assert isinstance(lookup, NullGeoLookup)


@pytest.mark.unit
def test_open_geo_lookup_falls_back_when_path_is_unreadable(tmp_path: Path) -> None:
    # An empty file at the expected location isn't a valid MaxMind DB.
    bogus = tmp_path / "bad.mmdb"
    bogus.write_bytes(b"not a mmdb")
    lookup = open_geo_lookup(bogus)
    assert isinstance(lookup, NullGeoLookup)
