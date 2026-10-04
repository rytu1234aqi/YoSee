"""Configured external content source."""

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from server.database.base import Base, TimestampMixin, empty_json

if TYPE_CHECKING:
    from server.models.content import ContentItem
    from server.models.fetch_run import FetchRun


class Source(Base, TimestampMixin):
    """One configured site, feed, or API.

    ``id`` is the stable key from source config (for example ``redblobgames``), not a
    generated UUID. ``etag`` and ``last_modified`` are the HTTP cache validators.
    Category is not a table yet, so ``default_category_id`` is only a stored reference.
    """

    __tablename__ = "sources"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    provider_type: Mapped[str] = mapped_column(String(64), nullable=False)
    homepage_url: Mapped[str | None] = mapped_column(String(2048))
    icon_url: Mapped[str | None] = mapped_column(String(2048))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    default_category_id: Mapped[str | None] = mapped_column(String(128))
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=empty_json, nullable=False)
    fetch_interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    last_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    etag: Mapped[str | None] = mapped_column(String(512))
    last_modified: Mapped[str | None] = mapped_column(String(256))

    items: Mapped[list["ContentItem"]] = relationship(back_populates="source")
    fetch_runs: Mapped[list["FetchRun"]] = relationship(back_populates="source")
