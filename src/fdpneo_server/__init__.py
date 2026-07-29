"""FAIR Data Point v2 server.

A FAIR-aligned metadata repository implementing the FDP specifications
(https://specs.fairdatapoint.org) with full W3C Linked Data Platform support,
SPARQL endpoint with access control, ODRL-based authorization, and
anonymous-by-design usage metrics.

See ``docs/architecture/README.md`` for the architecture; this docstring
intentionally does not duplicate it.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("fdpneo")
except PackageNotFoundError:  # pragma: no cover - source tree without an install
    __version__ = "0.0.0"
