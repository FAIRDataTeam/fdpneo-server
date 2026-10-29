"""Every Postgres table is classified for full backups (ADR-0029 "Harder" bullet).

A new ORM table must be added to exactly one of STATE / DERIVED / EXCLUDED in
``storage.postgres.state_backup`` — otherwise a full backup would silently miss
it (or carry data it must not).
"""

from __future__ import annotations

import pytest

from fdpneo_server.storage.postgres.models import Base, register_all_models
from fdpneo_server.storage.postgres.state_backup import (
    DERIVED_TABLES,
    EXCLUDED_TABLES,
    STATE_TABLES,
)

pytestmark = pytest.mark.unit


def test_every_table_is_classified_exactly_once() -> None:
    register_all_models()
    tables = set(Base.metadata.tables)
    classified = [*STATE_TABLES, *DERIVED_TABLES, *EXCLUDED_TABLES]
    assert len(classified) == len(set(classified)), "a table is classified twice"
    unclassified = tables - set(classified)
    assert not unclassified, (
        f"classify these tables in storage/postgres/state_backup.py: {sorted(unclassified)}"
    )
    stale = set(classified) - tables
    assert not stale, f"classified tables that no longer exist: {sorted(stale)}"


def test_privacy_sensitive_raw_metrics_never_travel() -> None:
    assert "metrics_raw" in EXCLUDED_TABLES  # ADR-0002: visitor hashes stay short-lived
    assert "metrics_raw" not in STATE_TABLES
