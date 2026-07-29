"""Persistent-identifier tooling (v0.3.0, ADR-0014).

Operator-facing helpers behind ``fdp pid``:

* :mod:`fdpneo_server.metadata.pid.w3id` — generate the W3ID redirect ``.htaccess``.
* :mod:`fdpneo_server.metadata.pid.github` — open/update the w3id.org PR (opt-in).
* :mod:`fdpneo_server.metadata.pid.verify` — confirm identifiers resolve to this FDP.
* :mod:`fdpneo_server.metadata.pid.rebase` — one-time adoption migration of existing IRIs.
"""

from __future__ import annotations

from fdpneo_server.metadata.pid.rebase import RebaseReport, rebase_identifiers
from fdpneo_server.metadata.pid.verify import ResolutionReport, verify_resolution
from fdpneo_server.metadata.pid.w3id import W3IDConfig, build_w3id_config, w3id_prefix_from

__all__ = [
    "RebaseReport",
    "ResolutionReport",
    "W3IDConfig",
    "build_w3id_config",
    "rebase_identifiers",
    "verify_resolution",
    "w3id_prefix_from",
]
