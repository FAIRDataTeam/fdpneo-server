"""Authorization index columns and indexes.

Fleshes out the ``authz_index`` table that migration 0001 reserved. The
schema mirrors architecture §9.4: ``(subject_key, action, graph_uri,
decision, policy_version)`` plus a ``computed_at`` timestamp for TTL.

Revision ID: 0002_authz_index_columns
Revises: 0001_initial_tables
Create Date: 2026-05-20

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_authz_index_columns"
down_revision: str | None = "0001_initial_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("authz_index", sa.Column("subject_key", sa.String(256), nullable=False))
    op.add_column("authz_index", sa.Column("action", sa.String(32), nullable=False))
    op.add_column("authz_index", sa.Column("graph_uri", sa.String(1024), nullable=False))
    op.add_column("authz_index", sa.Column("decision", sa.String(16), nullable=False))
    op.add_column("authz_index", sa.Column("policy_version", sa.String(1024), nullable=True))
    op.add_column(
        "authz_index", sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False)
    )
    op.create_unique_constraint(
        "uq_authz_subject_action_graph",
        "authz_index",
        ["subject_key", "action", "graph_uri"],
    )
    op.create_index("ix_authz_graph_uri", "authz_index", ["graph_uri"])
    op.create_index("ix_authz_subject_key", "authz_index", ["subject_key"])


def downgrade() -> None:
    op.drop_index("ix_authz_subject_key", table_name="authz_index")
    op.drop_index("ix_authz_graph_uri", table_name="authz_index")
    op.drop_constraint("uq_authz_subject_action_graph", "authz_index", type_="unique")
    op.drop_column("authz_index", "computed_at")
    op.drop_column("authz_index", "policy_version")
    op.drop_column("authz_index", "decision")
    op.drop_column("authz_index", "graph_uri")
    op.drop_column("authz_index", "action")
    op.drop_column("authz_index", "subject_key")
