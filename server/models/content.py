"""Unified content record. Every provider eventually persists this shape."""

from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from server.database.base import Base, TimestampMixin, empty_json, new_id

if TYPE_CHECKING:
    from server.models.source import Source


class ContentType(StrEnum):
    """Kinds of content a source can produce. Stored as strings, not a native DB enum."""

    ARTICLE = "ARTICLE"
    POST = "POST"
    QUESTION = "QUESTION"
    DISCUSSION = "DISCUSSION"
    VIDEO = "VIDEO"
    NEWSLETTER = "NEWSLETTER"


class ContentItem(Base, TimestampMixin):
    """Normalized content row.

    ``content_html`` and ``content_text`` are null when a source only supplies metadata.
    ``content_updated_at`` is the upstream revision time. ``updated_at`` is the local row time.
    Dedup keys are ``(source_id, external_id)`` and ``url_hash`` (hash of the canonical URL).
    """

    __tablename__ = "content_items"
    __table_args__ = (
        UniqueConstraint(
            "source_id",
            "external_id",
            name="uq_content_items_source_external_id",
        ),
        UniqueConstraint("url_hash", name="uq_content_items_url_hash"),
        Index("ix_content_items_published_at", "published_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(512))
    content_type: Mapped[ContentType] = mapped_column(
        Enum(ContentType, native_enum=False, length=32),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    content_html: Mapped[str | None] = mapped_column(Text)
    content_text: Mapped[str | None] = mapped_column(Text)
    canonical_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    source_url: Mapped[str | None] = mapped_column(String(2048))
    author: Mapped[str | None] = mapped_column(String(512))
    author_url: Mapped[str | None] = mapped_column(String(2048))
    cover_url: Mapped[str | None] = mapped_column(String(2048))
    language: Mapped[str | None] = mapped_column(String(32))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    content_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reading_time_minutes: Mapped[int | None] = mapped_column(Integer)
    url_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    raw_metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        default=empty_json,
        nullable=False,
    )
    parser_version: Mapped[str | None] = mapped_column(String(64))

    source: Mapped["Source"] = relationship(back_populates="items")
