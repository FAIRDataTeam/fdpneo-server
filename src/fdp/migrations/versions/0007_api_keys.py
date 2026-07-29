"""API keys + subject-principal tables.

Backs Phase 11.1 (ADR-0011) — long-lived API-key credentials for
machine-to-machine access, bound to an existing OIDC subject.

* ``api_keys`` — one row per issued key. Stores ``key_hash`` (sha256 of the
  ``fdpk_…`` token, never the token itself), the owner subject, a
  display-only prefix, the roles/groups snapshot captured at mint time
  (audit + fallback), and the lifecycle columns (expiry, last-used, revoked).
* ``subject_principal`` — one row per subject recording the roles/groups the
  IdP most recently asserted (upserted by the auth middleware on JWT login).
  API-key authentication resolves *live* roles from here so a long-lived key
  reflects the owner's current authorization rather than a frozen snapshot.

Revision ID: 0007_api_keys
Revises: 0006_runtime_settings
Create Date: 2026-06-02

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_api_keys"
down_revision: str | None = "0006_runtime_settings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "api_keys",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_subject", sa.String(2048), nullable=False),
        sa.Column("label", sa.String(256), nullable=False),
        sa.Column("key_hash", sa.String(64), nullable=False),
        sa.Column("display_prefix", sa.String(32), nullable=False),
        sa.Column("roles_json", postgresql.JSONB(), nullable=False),
        sa.Column("groups_json", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Hash lookup is the auth hot path → unique index (one key per hash).
    op.create_index("ux_api_keys_key_hash", "api_keys", ["key_hash"], unique=True)
    # Self-service listing is "my keys".
    op.create_index("ix_api_keys_owner_subject", "api_keys", ["owner_subject"])

    op.create_table(
        "subject_principal",
        sa.Column("subject", sa.String(2048), primary_key=True),
        sa.Column("roles_json", postgresql.JSONB(), nullable=False),
        sa.Column("groups_json", postgresql.JSONB(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("subject_principal")
    op.drop_index("ix_api_keys_owner_subject", table_name="api_keys")
    op.drop_index("ux_api_keys_key_hash", table_name="api_keys")
    op.drop_table("api_keys")
