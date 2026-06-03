"""Content negotiation for LDP RDF Sources.

Supports Turtle (default), JSON-LD, RDF/XML and N-Triples. The order of
:data:`SUPPORTED_TYPES` is the server's preference when the client sends a
wildcard ``*/*``.

The helpers are pure: no I/O, no exceptions outside ``ValueError`` for
unknown media types. The router decides what HTTP status to return.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType

from rdflib import Graph

TURTLE = "text/turtle"
JSON_LD = "application/ld+json"
RDF_XML = "application/rdf+xml"
N_TRIPLES = "application/n-triples"
SPARQL_UPDATE = "application/sparql-update"

SUPPORTED_TYPES: tuple[str, ...] = (TURTLE, JSON_LD, RDF_XML, N_TRIPLES)
"""Server-preferred order used when the client sends a wildcard."""

_RDFLIB_FORMAT = MappingProxyType(
    {
        TURTLE: "turtle",
        JSON_LD: "json-ld",
        RDF_XML: "xml",
        N_TRIPLES: "nt",
    }
)


@dataclass(frozen=True)
class MediaRange:
    """One parsed entry from an ``Accept`` header."""

    media_type: str
    quality: float


def parse_accept(header: str | None) -> tuple[MediaRange, ...]:
    """Parse an ``Accept`` header into ranges. Missing header is treated as ``*/*``."""
    if not header:
        return (MediaRange("*/*", 1.0),)
    ranges: list[MediaRange] = []
    for raw in header.split(","):
        token = raw.strip()
        if not token:
            continue
        parts = token.split(";")
        media = parts[0].strip().lower()
        quality = 1.0
        for param in parts[1:]:
            cleaned = param.strip().lower()
            if cleaned.startswith("q="):
                try:
                    quality = float(cleaned[2:])
                except ValueError:
                    quality = 0.0
        ranges.append(MediaRange(media, quality))
    return tuple(ranges)


def select_media_type(accept: str | None) -> str | None:
    """Return the best supported media type for ``accept``, or ``None`` if none."""
    ranges = parse_accept(accept)
    candidates = sorted(
        (r for r in ranges if r.quality > 0.0),
        key=lambda r: r.quality,
        reverse=True,
    )
    for r in candidates:
        match = _match(r.media_type)
        if match is not None:
            return match
    return None


def _match(media_type: str) -> str | None:
    if media_type == "*/*":
        return SUPPORTED_TYPES[0]
    if media_type.endswith("/*"):
        type_prefix = media_type[:-1]  # keep the trailing slash
        for supported in SUPPORTED_TYPES:
            if supported.startswith(type_prefix):
                return supported
        return None
    if media_type in _RDFLIB_FORMAT:
        return media_type
    return None


def serialize(graph: Graph, media_type: str) -> bytes:
    """Serialize ``graph`` to bytes in ``media_type``.

    Raises :class:`ValueError` if ``media_type`` is outside the supported set.
    """
    fmt = _RDFLIB_FORMAT.get(media_type)
    if fmt is None:
        raise ValueError(f"unsupported media type: {media_type}")
    output = graph.serialize(format=fmt)
    if isinstance(output, bytes):
        return output
    return output.encode("utf-8")


def parse(body: bytes, media_type: str, *, base: str | None = None) -> Graph:
    """Parse ``body`` as RDF in ``media_type``.

    ``base`` is the document base against which relative IRIs (notably the
    empty ``<>``, meaning "this resource") resolve — per LDP it is the target
    resource's URI. Without it rdflib invents a ``file://`` base and the
    record's own triples end up under a bogus subject, invisible to any
    subject-keyed read (search, dashboard, ``/expanded``).

    Raises :class:`ValueError` for unsupported media types; the rdflib
    parser raises its own exceptions for malformed input.
    """
    fmt = _RDFLIB_FORMAT.get(media_type)
    if fmt is None:
        raise ValueError(f"unsupported media type: {media_type}")
    graph = Graph()
    graph.parse(data=body.decode("utf-8"), format=fmt, publicID=base)
    return graph


def normalize_content_type(header: str | None) -> str | None:
    """Strip parameters from a ``Content-Type`` header.

    Returns ``None`` when the header is missing so the router can decide a
    default. Returns the lowercased base media type otherwise (e.g.
    ``"text/turtle; charset=utf-8"`` becomes ``"text/turtle"``).
    """
    if header is None:
        return None
    return header.split(";", 1)[0].strip().lower()


__all__ = [
    "JSON_LD",
    "N_TRIPLES",
    "RDF_XML",
    "SPARQL_UPDATE",
    "SUPPORTED_TYPES",
    "TURTLE",
    "MediaRange",
    "normalize_content_type",
    "parse",
    "parse_accept",
    "select_media_type",
    "serialize",
]
