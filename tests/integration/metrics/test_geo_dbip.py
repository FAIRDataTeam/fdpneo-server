"""Proves the bundled DB-IP IP-to-City Lite database is a drop-in for the
MaxMind-format geo lookup.

DB-IP's lite build is binary-compatible with the MaxMind DB format and names its
``database_type`` ``DBIP-City-Lite`` (contains ``"City"``), so geoip2's
``Reader.city()`` accepts it and the field schema the lookup reads
(``country.iso_code``, ``subdivisions.most_specific``, ``city.name``) resolves.

Skipped unless the database has been fetched (``scripts/fetch-geoip.sh``), so it
never fails CI or unit runs that don't have the file. Fetch it and this runs:

    scripts/fetch-geoip.sh
    .venv/bin/pytest tests/integration/metrics/test_geo_dbip.py
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from fdp.metrics.geo import MaxMindGeoLookup, open_geo_lookup

_DB_PATH = Path(
    os.environ.get("FDP_METRICS_GEOIP_DATABASE_PATH", "data/geoip/GeoLite2-City.mmdb")
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _DB_PATH.exists(),
        reason=f"GeoIP DB not present at {_DB_PATH}; run scripts/fetch-geoip.sh",
    ),
]


def test_dbip_database_opens_as_maxmind_lookup() -> None:
    """A real DB-IP file opens as the MaxMind-backed lookup, not the no-op."""
    lookup = open_geo_lookup(_DB_PATH)
    try:
        assert isinstance(lookup, MaxMindGeoLookup)
    finally:
        lookup.close()


def test_dbip_resolves_a_known_public_ip_to_a_country() -> None:
    """A well-known public IP resolves to a country code via the real DB."""
    lookup = open_geo_lookup(_DB_PATH)
    try:
        # 8.8.8.8 (Google public DNS) resolves to the US in every geo DB.
        result = lookup.lookup("8.8.8.8")
        assert result.country_code == "US"
    finally:
        lookup.close()


def test_dbip_unresolvable_ip_returns_empty() -> None:
    """Private-range IPs aren't in the DB and must yield an empty result."""
    lookup = open_geo_lookup(_DB_PATH)
    try:
        result = lookup.lookup("10.0.0.1")
        assert result.country_code is None
    finally:
        lookup.close()
