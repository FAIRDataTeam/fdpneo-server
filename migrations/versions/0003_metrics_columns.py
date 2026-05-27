"""Metrics tables — raw, hourly, daily columns and indexes.

Creates the ``metrics_raw`` table that holds anonymized samples
between collection and the hourly rollup, and fleshes out the
``metrics_hourly`` / ``metrics_daily`` tables that migration 0001
reserved. Schema mirrors :mod:`fdp.metrics.repository`.

Dimension uniqueness is declared with ``NULLS NOT DISTINCT`` (Postgres
15+) so an UPSERT keyed on the dimension tuple merges rows that share
NULL values for optional dimensions (resource_iri, country_code,
region, city).

Revision ID: 0003_metrics_columns
Revises: 0002_authz_index_columns
Create Date: 2026-05-27

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_metrics_columns"
down_revision: str | None = "0002_authz_index_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_AGG_COLUMNS: tuple[tuple[str, sa.types.TypeEngine[int]], ...] = (
    ("request_count", sa.BigInteger()),
    ("unique_visitors", sa.BigInteger()),
    ("latency_ms_sum", sa.BigInteger()),
    ("status_2xx_count", sa.BigInteger()),
    ("status_3xx_count", sa.BigInteger()),
    ("status_4xx_count", sa.BigInteger()),
    ("status_5xx_count", sa.BigInteger()),
)


def upgrade() -> None:
    # --- metrics_raw: create from scratch (not reserved in 0001) ----------
    op.create_table(
        "metrics_raw",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("bucket", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("resource_iri", sa.String(2048), nullable=True),
        sa.Column("country_code", sa.String(2), nullable=True),
        sa.Column("region", sa.String(128), nullable=True),
        sa.Column("city", sa.String(128), nullable=True),
        sa.Column("visitor_hash", sa.String(32), nullable=True),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_metrics_raw_bucket", "metrics_raw", ["bucket"])
    op.create_index("ix_metrics_raw_recorded_at", "metrics_raw", ["recorded_at"])

    # --- metrics_hourly: add dimension and aggregate columns --------------
    op.add_column(
        "metrics_hourly",
        sa.Column("bucket", sa.DateTime(timezone=True), nullable=False),
    )
    op.add_column("metrics_hourly", sa.Column("event_type", sa.String(32), nullable=False))
    op.add_column("metrics_hourly", sa.Column("resource_iri", sa.String(2048), nullable=True))
    op.add_column("metrics_hourly", sa.Column("country_code", sa.String(2), nullable=True))
    op.add_column("metrics_hourly", sa.Column("region", sa.String(128), nullable=True))
    op.add_column("metrics_hourly", sa.Column("city", sa.String(128), nullable=True))
    for name, type_ in _AGG_COLUMNS:
        op.add_column(
            "metrics_hourly",
            sa.Column(name, type_, nullable=False, server_default="0"),
        )
    op.create_unique_constraint(
        "uq_metrics_hourly_dimensions",
        "metrics_hourly",
        ["bucket", "event_type", "resource_iri", "country_code", "region", "city"],
        postgresql_nulls_not_distinct=True,
    )
    op.create_index("ix_metrics_hourly_bucket", "metrics_hourly", ["bucket"])

    # --- metrics_daily: add dimension and aggregate columns ---------------
    op.add_column("metrics_daily", sa.Column("bucket", sa.Date(), nullable=False))
    op.add_column("metrics_daily", sa.Column("event_type", sa.String(32), nullable=False))
    op.add_column("metrics_daily", sa.Column("resource_iri", sa.String(2048), nullable=True))
    op.add_column("metrics_daily", sa.Column("country_code", sa.String(2), nullable=True))
    op.add_column("metrics_daily", sa.Column("region", sa.String(128), nullable=True))
    op.add_column("metrics_daily", sa.Column("city", sa.String(128), nullable=True))
    for name, type_ in _AGG_COLUMNS:
        op.add_column(
            "metrics_daily",
            sa.Column(name, type_, nullable=False, server_default="0"),
        )
    op.create_unique_constraint(
        "uq_metrics_daily_dimensions",
        "metrics_daily",
        ["bucket", "event_type", "resource_iri", "country_code", "region", "city"],
        postgresql_nulls_not_distinct=True,
    )
    op.create_index("ix_metrics_daily_bucket", "metrics_daily", ["bucket"])


def downgrade() -> None:
    op.drop_index("ix_metrics_daily_bucket", table_name="metrics_daily")
    op.drop_constraint("uq_metrics_daily_dimensions", "metrics_daily", type_="unique")
    for name, _ in reversed(_AGG_COLUMNS):
        op.drop_column("metrics_daily", name)
    op.drop_column("metrics_daily", "city")
    op.drop_column("metrics_daily", "region")
    op.drop_column("metrics_daily", "country_code")
    op.drop_column("metrics_daily", "resource_iri")
    op.drop_column("metrics_daily", "event_type")
    op.drop_column("metrics_daily", "bucket")

    op.drop_index("ix_metrics_hourly_bucket", table_name="metrics_hourly")
    op.drop_constraint("uq_metrics_hourly_dimensions", "metrics_hourly", type_="unique")
    for name, _ in reversed(_AGG_COLUMNS):
        op.drop_column("metrics_hourly", name)
    op.drop_column("metrics_hourly", "city")
    op.drop_column("metrics_hourly", "region")
    op.drop_column("metrics_hourly", "country_code")
    op.drop_column("metrics_hourly", "resource_iri")
    op.drop_column("metrics_hourly", "event_type")
    op.drop_column("metrics_hourly", "bucket")

    op.drop_index("ix_metrics_raw_recorded_at", table_name="metrics_raw")
    op.drop_index("ix_metrics_raw_bucket", table_name="metrics_raw")
    op.drop_table("metrics_raw")
