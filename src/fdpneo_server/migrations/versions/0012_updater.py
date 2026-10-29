"""Host updater support (ADR-0030).

* ``api_keys.scope`` — ``NULL`` for ordinary keys, ``'updater'`` for the
  restricted key the host updater agent uses.
* ``update_request`` — admin-requested updates and their outcomes.
* ``updater_agent`` — single row with the agent's last poll.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0012_updater"
down_revision: str | None = "0011_release_check"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("api_keys", sa.Column("scope", sa.String(32), nullable=True))
    op.create_table(
        "update_request",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("component", sa.String(16), nullable=False),
        sa.Column("target_version", sa.String(32), nullable=False),
        sa.Column("from_version", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("requested_by", sa.String(2048), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("message", sa.String(4000), nullable=True),
        sa.Column("backup_location", sa.String(1024), nullable=True),
    )
    op.create_index("ix_update_request_status", "update_request", ["status"])
    op.create_table(
        "updater_agent",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("agent_version", sa.String(64), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("updater_agent")
    op.drop_index("ix_update_request_status", table_name="update_request")
    op.drop_table("update_request")
    op.drop_column("api_keys", "scope")
