"""Record-modification audit table.

Adds ``record_audit`` — one row per ``RecordCreated`` /
``RecordModified`` / ``RecordDeleted`` event the audit subscriber
sees. Distinct from ``policy_decisions_audit`` (which migration 0001
reserved for policy-evaluation trails); record-modification audit is
the who/what/when of LDP writes.

Revision ID: 0005_record_audit_table
Revises: 0004_profile_applied_columns
Create Date: 2026-05-28

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_record_audit_table"
down_revision: str | None = "0004_profile_applied_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "record_audit",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("record_iri", sa.String(2048), nullable=False),
        sa.Column("operation", sa.String(16), nullable=False),
        sa.Column("subject", sa.String(2048), nullable=True),
        sa.Column("etag", sa.String(64), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_record_audit_record_iri", "record_audit", ["record_iri"])
    op.create_index("ix_record_audit_occurred_at", "record_audit", ["occurred_at"])
    op.create_index("ix_record_audit_subject", "record_audit", ["subject"])


def downgrade() -> None:
    op.drop_index("ix_record_audit_subject", table_name="record_audit")
    op.drop_index("ix_record_audit_occurred_at", table_name="record_audit")
    op.drop_index("ix_record_audit_record_iri", table_name="record_audit")
    op.drop_table("record_audit")
