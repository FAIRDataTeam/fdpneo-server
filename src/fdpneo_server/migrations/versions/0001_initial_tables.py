"""Initial operational-state tables.

Reserves the six table names that the bounded contexts will own. Each
table starts with only a synthetic primary key; consuming modules add
their real columns in later, module-specific migrations.

Revision ID: 0001_initial_tables
Revises:
Create Date: 2026-05-19

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001_initial_tables"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TABLES: tuple[str, ...] = (
    "metrics_hourly",
    "metrics_daily",
    "authz_index",
    "policy_decisions_audit",
    "job_state",
    "profile_applied",
)


def upgrade() -> None:
    for name in _TABLES:
        op.create_table(
            name,
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        )


def downgrade() -> None:
    for name in reversed(_TABLES):
        op.drop_table(name)
