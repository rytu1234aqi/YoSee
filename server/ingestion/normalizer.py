"""Shared content shape produced by every provider.

URL cleanup and the canonical URL hash live in DedupService. Reading time and
``fetched_at`` are still later pipeline steps. This module only defines the model
those steps, and every adapter, already agree on.
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from server.models.content import ContentType


class NormalizedContentItem(BaseModel):
    """One external item after provider-specific parsing.

    ``updated_at`` is the upstream revision time. The database stores that value on
    ``ContentItem.content_updated_at`` so it does not collide with the local row time.
    DedupService fills ``url_hash`` from the normalized canonical URL. ``fetched_at``
    is filled in when the item is persisted.
    """

    model_config = ConfigDict(extra="forbid")

    source_id: str
    external_id: str | None = None

    content_type: ContentType

    title: str
    summary: str | None = None

    content_html: str | None = None
    content_text: str | None = None

    canonical_url: str
    source_url: str | None = None

    author: str | None = None
    author_url: str | None = None

    cover_url: str | None = None

    language: str | None = None

    published_at: datetime | None = None
    updated_at: datetime | None = None

    raw_metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("source_id", "title", "canonical_url")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be empty")
        return stripped
