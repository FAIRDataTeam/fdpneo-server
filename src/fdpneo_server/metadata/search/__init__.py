"""Metadata search (Phase 7).

A Postgres full-text index over the knowledge-graph records, kept current by an
event-bus subscriber, queried through ``POST /search`` with visibility gating
that respects both ODRL read and publication state (ADR-0010). Saved queries
live alongside under ``/me/saved-queries``.
"""

from __future__ import annotations
