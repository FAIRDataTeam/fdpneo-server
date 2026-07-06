"""External label cache.

Backs Phase 21 (external/remote label resolution, ADR-0012 extension).

* ``metadata_external_labels`` — a ``(iri, language) -> label`` cache for labels
  dereferenced from external IRIs (ROR, DOI, ORCID, SKOS terms). A ``NULL``
  ``label`` is a cached *miss*; ``expires_at`` carries the (positive/negative)
  TTL and is indexed for cheap expiry sweeps.

Revision ID: 0009_external_labels
Revises: 0008_search
Create Date: 2026-07-06

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_external_labels"
down_revision: str | None = "0008_search"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "metadata_external_labels",
        sa.Column("iri", sa.String(2048), primary_key=True),
        sa.Column("language", sa.String(32), primary_key=True),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_host", sa.String(255), nullable=True),
    )
    op.create_index(
        "ix_metadata_external_labels_expires_at",
        "metadata_external_labels",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_metadata_external_labels_expires_at", table_name="metadata_external_labels")
    op.drop_table("metadata_external_labels")
