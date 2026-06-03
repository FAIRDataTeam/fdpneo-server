"""Search index + saved queries.

Backs Phase 7.

* ``metadata_search`` — one row per indexable record, the Postgres
  full-text search index. ``search_text`` is a ``tsvector`` (GIN-indexed)
  built from title/description/keywords; ``state`` + ``anon_read`` are the
  stored visibility flags (ADR-0010) that gate anonymous results cheaply,
  with the authenticated "owner tail" applied at query time.
* ``search_saved_queries`` — named, reusable search definitions (Phase 7.3).
  Owner-scoped; an admin may mark one ``shared`` to expose it to everyone.

Revision ID: 0008_search
Revises: 0007_api_keys
Create Date: 2026-06-02

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_search"
down_revision: str | None = "0007_api_keys"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "metadata_search",
        sa.Column("record_iri", sa.String(2048), primary_key=True),
        sa.Column("type_iri", sa.String(2048), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("license", sa.String(2048), nullable=True),
        sa.Column("keywords", sa.Text(), nullable=True),
        sa.Column("search_text", postgresql.TSVECTOR(), nullable=True),
        sa.Column("state", sa.String(16), nullable=True),
        sa.Column("anon_read", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("language", sa.String(32), nullable=False, server_default="english"),
    )
    op.create_index(
        "ix_metadata_search_search_text",
        "metadata_search",
        ["search_text"],
        postgresql_using="gin",
    )
    op.create_index("ix_metadata_search_type_iri", "metadata_search", ["type_iri"])
    op.create_index("ix_metadata_search_license", "metadata_search", ["license"])
    op.create_index("ix_metadata_search_updated_at", "metadata_search", ["updated_at"])
    # The dominant anonymous query filters on these two together.
    op.create_index(
        "ix_metadata_search_visibility", "metadata_search", ["state", "anon_read"]
    )

    op.create_table(
        "search_saved_queries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_subject", sa.String(2048), nullable=False),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("query_json", postgresql.JSONB(), nullable=False),
        sa.Column("shared", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_search_saved_queries_owner", "search_saved_queries", ["owner_subject"]
    )
    op.create_index(
        "ix_search_saved_queries_shared", "search_saved_queries", ["shared"]
    )


def downgrade() -> None:
    op.drop_table("search_saved_queries")
    op.drop_index("ix_metadata_search_visibility", table_name="metadata_search")
    op.drop_index("ix_metadata_search_updated_at", table_name="metadata_search")
    op.drop_index("ix_metadata_search_license", table_name="metadata_search")
    op.drop_index("ix_metadata_search_type_iri", table_name="metadata_search")
    op.drop_index("ix_metadata_search_search_text", table_name="metadata_search")
    op.drop_table("metadata_search")
