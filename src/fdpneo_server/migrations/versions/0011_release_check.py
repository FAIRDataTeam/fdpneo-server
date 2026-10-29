"""Release-check cache for update notifications (ADR-0028).

A single row (``id = 1``) holding the outcome of the last GitHub release
check: per component the repository, the ETag of the last response and the
published releases with their upgrade metadata. Shared by replicas, so a
restart or a second replica does not refetch.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011_release_check"
down_revision: str | None = "0010_index_targets"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "release_check",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("payload_json", postgresql.JSONB(), nullable=False),
        sa.Column("error", sa.String(1024), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("release_check")
