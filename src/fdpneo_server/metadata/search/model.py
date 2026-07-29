"""ORM models for the search index and saved queries (Phase 7)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, String, Text
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from fdpneo_server.storage.postgres.models import Base
from fdpneo_server.storage.postgres.types import AwareDateTime


class SearchRow(Base):
    """One indexable record in the full-text search index.

    ``search_text`` is a Postgres ``tsvector``; on SQLite (unit tests that touch
    other tables) it degrades to ``Text`` and is never queried — the FTS query
    path is Postgres-only and exercised by integration tests.
    """

    __tablename__ = "metadata_search"

    record_iri: Mapped[str] = mapped_column(String(2048), primary_key=True)
    type_iri: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    title: Mapped[str | None] = mapped_column(Text(), nullable=True)
    description: Mapped[str | None] = mapped_column(Text(), nullable=True)
    license: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    keywords: Mapped[str | None] = mapped_column(Text(), nullable=True)
    search_text: Mapped[Any] = mapped_column(
        TSVECTOR().with_variant(Text(), "sqlite"), nullable=True
    )
    state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    anon_read: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    updated_at: Mapped[datetime | None] = mapped_column(AwareDateTime(), nullable=True)
    language: Mapped[str] = mapped_column(String(32), nullable=False, default="english")


class SavedQueryRow(Base):
    """A named, reusable search definition (Phase 7.3)."""

    __tablename__ = "search_saved_queries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_subject: Mapped[str] = mapped_column(String(2048), nullable=False)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    query_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=False
    )
    shared: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), nullable=False)


__all__ = ["SavedQueryRow", "SearchRow"]
