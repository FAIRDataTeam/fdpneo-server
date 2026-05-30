"""Runtime settings table.

Backs Phase 9 — admin-managed runtime configuration that overlays the
deployment-profile defaults. One row per setting key (e.g.
``forms.autocomplete-sources``, ``search.filters``); the value is a
JSONB blob whose shape is validated against the per-key Pydantic model
declared in :mod:`fdp.metadata.settings`.

The table is intentionally schema-light. Per-key shape validation lives
in the application layer where it can evolve with new keys without a
migration; the audit columns are kept simple (``updated_by`` /
``updated_at``) because settings updates are also logged structurally
via structlog.

Revision ID: 0006_runtime_settings
Revises: 0005_record_audit_table
Create Date: 2026-05-30

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_runtime_settings"
down_revision: str | None = "0005_record_audit_table"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "runtime_settings",
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column("value_json", postgresql.JSONB(), nullable=False),
        sa.Column("updated_by", sa.String(2048), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("runtime_settings")
