"""Triple store adapter — SPARQL 1.1 Protocol port.

The FDP communicates with the configured triple store exclusively through
SPARQL 1.1 Protocol (ADR-0005). This package exposes a single class,
:class:`TripleStoreAdapter`, that wraps the protocol in a small async API.
"""

from __future__ import annotations

from fdp.storage.triplestore.adapter import TripleStoreAdapter

__all__ = ["TripleStoreAdapter"]
