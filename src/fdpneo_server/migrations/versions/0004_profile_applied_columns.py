"""Profile-applied marker columns.

Migration 0001 reserved ``profile_applied`` as an empty table; this
migration adds the columns the bootstrap pipeline writes after a
successful apply (architecture §12.2):

* ``name`` / ``version`` — manifest identity, used by ``fdp profile info``.
* ``applied_at`` — when the apply completed.
* ``manifest_checksum`` — SHA-256 of the canonicalized manifest, so
  operators can spot a divergence between the applied profile and what
  is currently on disk.

A partial unique index on a constant expression enforces "at most one
applied row at a time" — a clean re-apply is "wipe + insert", not
"update".

Revision ID: 0004_profile_applied_columns
Revises: 0003_metrics_columns
Create Date: 2026-05-27

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_profile_applied_columns"
down_revision: str | None = "0003_metrics_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("profile_applied", sa.Column("name", sa.String(256), nullable=False))
    op.add_column("profile_applied", sa.Column("version", sa.String(64), nullable=False))
    op.add_column(
        "profile_applied",
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.add_column("profile_applied", sa.Column("manifest_checksum", sa.String(64), nullable=False))
    # Forces a single applied profile at a time. Using a partial unique
    # index on a constant lets us express "at most one row" without
    # picking an arbitrary value to be unique on.
    op.execute("CREATE UNIQUE INDEX ux_profile_applied_singleton ON profile_applied ((true))")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ux_profile_applied_singleton")
    op.drop_column("profile_applied", "manifest_checksum")
    op.drop_column("profile_applied", "applied_at")
    op.drop_column("profile_applied", "version")
    op.drop_column("profile_applied", "name")
