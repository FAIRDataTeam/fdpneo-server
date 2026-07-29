"""Persistent-identifier canonicalization (v0.3.0, ADR-0014).

A FAIR persistent identifier decouples a record's identity from the host that
serves it. Records are minted under a stable **identifier base** (a PID
namespace such as ``https://w3id.org/myfdp``); the deployment is reachable at one
or more **serving origins** (``base_url``) that a redirector — W3ID, PURL,
Handle — ultimately points to.

A request therefore arrives on a serving origin (post-redirect) but must resolve
the record whose canonical IRI is rooted at the identifier base. This module is
the single place that maps an inbound request URL to that canonical IRI, and the
predicate that decides whether an IRI already belongs to the identifier base.

Pure and dependency-free on purpose: the LDP router, the registry, and the PID
tooling all reason about the same mapping, so it lives in the shared kernel.

In development ``identifier_base == base_url``, so :func:`canonicalize` is the
identity and nothing changes for localhost deployments.
"""

from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import urlsplit

__all__ = ["canonicalize", "is_under", "relative_path"]


def _strip(url: str) -> str:
    return url.rstrip("/")


def relative_path(url: str, bases: Iterable[str]) -> str:
    """Return ``url``'s path relative to whichever known base it sits under.

    ``bases`` is any iterable of candidate base URLs (serving origins and/or the
    identifier base). The longest base that is a path-boundary prefix of ``url``
    wins, so a sub-path deployment (``https://example.org/fdp``) is handled. The
    result always starts with ``/`` (``/`` for the root). When no base matches —
    a request on an unexpected host — fall back to the URL's own path so the
    record identity is still rooted by path, not by the stray origin.
    """
    clean = url.split("?", 1)[0].split("#", 1)[0]
    best: str | None = None
    for base in bases:
        candidate = _strip(str(base))
        if clean == candidate:
            return "/"
        if clean.startswith(candidate + "/") and (best is None or len(candidate) > len(best)):
            best = candidate
    if best is not None:
        remainder = clean[len(best) :]
        return remainder or "/"
    path = urlsplit(clean).path or "/"
    return path if path.startswith("/") else "/" + path


def canonicalize(request_url: str, *, identifier_base: str, serving_origins: Iterable[str]) -> str:
    """Map an inbound request URL to its canonical identifier-base IRI.

    Args:
        request_url: The absolute URL the request arrived on (query/fragment
            ignored).
        identifier_base: The persistent PID namespace records are minted under.
        serving_origins: Iterable of origins requests may arrive on (typically
            ``{base_url}``). The identifier base is always considered too, so a
            request that already carries the canonical URL maps to itself.

    Returns:
        ``identifier_base`` + the request's base-relative path, with no trailing
        slash (the root maps to ``identifier_base`` exactly), matching the
        record-graph URI convention.
    """
    base = _strip(identifier_base)
    bases = [base, *(str(origin) for origin in serving_origins)]
    path = relative_path(request_url, bases)
    if path == "/":
        return base
    return _strip(base + path)


def is_under(iri: str, identifier_base: str) -> bool:
    """True when ``iri`` is the identifier base itself or nested beneath it."""
    base = _strip(identifier_base)
    clean = _strip(iri)
    return clean == base or clean.startswith(base + "/")
