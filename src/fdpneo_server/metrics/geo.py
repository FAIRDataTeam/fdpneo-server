"""Geo lookup behind a swappable port.

Architecture §11.1 expects an IP → country/region/city derivation via
the embedded MaxMind GeoLite2 database. We wrap the
:class:`geoip2.database.Reader` so consumers depend on the
:class:`GeoLookup` protocol rather than maxminddb's concrete types.

The :func:`open_geo_lookup` factory degrades gracefully: if the
configured ``geoip_database_path`` doesn't exist or can't be opened, it
logs a one-shot warning and returns a :class:`NullGeoLookup` so the
pipeline keeps running without geographic enrichment. This keeps dev
environments without GeoLite2 from hard-failing the metrics path.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, Self

import structlog

if TYPE_CHECKING:
    import geoip2.database

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class GeoResult:
    """The aggregate-safe slice of a geo lookup. All fields may be ``None``."""

    country_code: str | None
    region: str | None
    city: str | None


_EMPTY: GeoResult = GeoResult(country_code=None, region=None, city=None)


class GeoLookup(Protocol):
    """A swappable port for IP → geo derivation.

    Implementations must never raise on lookup; an unresolvable IP is a
    legitimate, expected outcome and returns ``_EMPTY``.
    """

    def lookup(self, ip: str | None) -> GeoResult: ...

    def close(self) -> None: ...


class NullGeoLookup:
    """Returns ``_EMPTY`` for every lookup. Used when GeoLite2 is absent."""

    def lookup(self, ip: str | None) -> GeoResult:
        del ip
        return _EMPTY

    def close(self) -> None:
        return None


class MaxMindGeoLookup:
    """Backed by :class:`geoip2.database.Reader` against a GeoLite2-City DB."""

    def __init__(self, reader: geoip2.database.Reader) -> None:
        self._reader = reader

    @classmethod
    def from_path(cls, path: str | Path) -> Self:
        """Open a GeoLite2-City database at ``path``. Raises if it fails."""
        import geoip2.database  # local import keeps the optional dep tidy

        return cls(geoip2.database.Reader(str(path)))

    def lookup(self, ip: str | None) -> GeoResult:
        if not ip:
            return _EMPTY
        import geoip2.errors

        try:
            response = self._reader.city(ip)
        except (geoip2.errors.AddressNotFoundError, ValueError):
            return _EMPTY
        subdivision = response.subdivisions.most_specific
        return GeoResult(
            country_code=response.country.iso_code,
            region=subdivision.name if subdivision else None,
            city=response.city.name,
        )

    def close(self) -> None:
        self._reader.close()


def open_geo_lookup(path: str | Path) -> GeoLookup:
    """Open the configured GeoLite2 DB, or fall back to a no-op lookup.

    Logs a warning the first time it falls back so operators can spot
    misconfigured deployments without breaking the pipeline.
    """
    resolved = Path(path)
    if not resolved.exists():
        log.warning("geoip_database_not_found", path=str(resolved))
        return NullGeoLookup()
    try:
        return MaxMindGeoLookup.from_path(resolved)
    except Exception as err:
        # Never let a bad GeoLite2 DB crash startup — fall back to no-op.
        log.warning("geoip_database_open_failed", path=str(resolved), error=str(err))
        return NullGeoLookup()


__all__ = [
    "GeoLookup",
    "GeoResult",
    "MaxMindGeoLookup",
    "NullGeoLookup",
    "open_geo_lookup",
]
