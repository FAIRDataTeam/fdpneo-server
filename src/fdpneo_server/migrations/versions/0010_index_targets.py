"""Runtime-managed FDP Index ping targets (ADR-0025).

One row per admin-registered index target. The effective ping set is these rows
unioned with the read-only ``FDP_INDEX_PING_TARGETS`` env list (env entries have
no row — their ping status is kept in memory only). ``url`` is stored normalized
(lowercase scheme+host, no trailing slash) and unique, so duplicates are a 409
at the API rather than a constraint error. The ``last_*`` columns record the
outcome of the most recent ping batch that included the target.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0010_index_targets"
down_revision: str | None = "0009_external_labels"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "index_targets",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("url", sa.String(2048), nullable=False, unique=True),
        sa.Column("note", sa.String(512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(2048), nullable=True),
        sa.Column("last_ping_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_status_code", sa.Integer(), nullable=True),
        sa.Column("last_ok", sa.Boolean(), nullable=True),
        sa.Column("last_detail", sa.String(1024), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("index_targets")
