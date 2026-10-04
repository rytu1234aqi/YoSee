"""Read and write content items.

Duplicate detection stays in DedupService. This class only loads rows and
applies the decision. It flushes, and the caller commits.
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from server.ingestion.dedup import DedupAction, DedupKey, DedupService, MatchOn, canonical_url_hash
from server.ingestion.normalizer import NormalizedContentItem
from server.models.content import ContentItem


class ContentRepository:
    """SQLite access for ``ContentItem``."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._dedup = DedupService()

    def get_by_external_id(self, source_id: str, external_id: str) -> ContentItem | None:
        """Return the row for one source's external id, or ``None``."""

        cleaned = external_id.strip()
        if not cleaned:
            return None
        statement = select(ContentItem).where(
            ContentItem.source_id == source_id,
            ContentItem.external_id == cleaned,
        )
        return self._session.scalar(statement)

    def get_by_url_hash(self, url_hash: str) -> ContentItem | None:
        """Return the row for a global canonical URL hash, or ``None``."""

        cleaned = url_hash.strip()
        if not cleaned:
            return None
        statement = select(ContentItem).where(ContentItem.url_hash == cleaned)
        return self._session.scalar(statement)

    def upsert(
        self,
        item: NormalizedContentItem,
        *,
        fetched_at: datetime | None = None,
    ) -> ContentItem:
        """Insert the item, or update the row DedupService already knows.

        The stored canonical URL is the normalized one. A match keeps ``id`` and
        ``source_id``. Content fields, the URL, and ``fetched_at`` are refreshed.
        """

        seen_at = fetched_at if fetched_at is not None else datetime.now(UTC)
        key, existing = self._match(item)
        if existing is None:
            row = _new_row(item, key, seen_at)
            self._session.add(row)
        else:
            _apply_update(existing, item, key, seen_at)
            row = existing
        self._session.flush()
        return row

    def restore(self, record: dict[str, Any]) -> ContentItem:
        """Restore an exported row verbatim while retaining URL de-duplication."""
        item_id = str(record["id"])
        existing = self._session.get(ContentItem, item_id)
        if existing is not None:
            return existing
        from server.models.content import ContentType

        row = ContentItem(
            id=item_id,
            source_id=str(record["source_id"]),
            external_id=str(record["external_id"]) if record.get("external_id") else None,
            content_type=ContentType(str(record.get("content_type") or "ARTICLE")),
            title=str(record["title"]),
            summary=str(record["summary"]) if record.get("summary") else None,
            content_html=str(record["content_html"]) if record.get("content_html") else None,
            content_text=str(record["content_text"]) if record.get("content_text") else None,
            canonical_url=str(record["canonical_url"]),
            source_url=str(record["source_url"]) if record.get("source_url") else None,
            author=str(record["author"]) if record.get("author") else None,
            published_at=_date(record.get("published_at")),
            fetched_at=_date(record.get("fetched_at")) or datetime.now(UTC),
            url_hash=canonical_url_hash(str(record["canonical_url"])),
        )
        collision = self.get_by_url_hash(row.url_hash)
        if collision is not None:
            return collision
        self._session.add(row)
        self._session.flush()
        return row

    def list_latest(self, *, limit: int = 50) -> list[ContentItem]:
        """Return the newest items. Undated rows come last."""

        if limit < 1:
            raise ValueError("limit must be positive")
        statement = (
            select(ContentItem)
            .order_by(
                ContentItem.published_at.desc().nulls_last(),
                ContentItem.id.desc(),
            )
            .limit(limit)
        )
        return list(self._session.scalars(statement))

    def _match(self, item: NormalizedContentItem) -> tuple[DedupKey, ContentItem | None]:
        key = self._dedup.prepare(item)
        by_external = (
            self.get_by_external_id(key.source_id, key.external_id)
            if key.external_id is not None
            else None
        )
        by_url = self.get_by_url_hash(key.url_hash)
        known = _unique_rows(by_external, by_url)
        decision = self._dedup.decide(item, [_key_from_row(row) for row in known])
        if decision.action is DedupAction.INSERT or decision.matched_on is None:
            return decision.key, None
        if decision.matched_on is MatchOn.EXTERNAL_ID:
            return decision.key, by_external
        return decision.key, by_url


def _unique_rows(*rows: ContentItem | None) -> list[ContentItem]:
    unique: list[ContentItem] = []
    seen: set[str] = set()
    for row in rows:
        if row is None or row.id in seen:
            continue
        seen.add(row.id)
        unique.append(row)
    return unique


def _key_from_row(row: ContentItem) -> DedupKey:
    return DedupKey(
        source_id=row.source_id,
        external_id=row.external_id,
        canonical_url=row.canonical_url,
        url_hash=row.url_hash,
    )


def _new_row(item: NormalizedContentItem, key: DedupKey, fetched_at: datetime) -> ContentItem:
    return ContentItem(
        source_id=item.source_id,
        external_id=key.external_id,
        content_type=item.content_type,
        title=item.title,
        summary=item.summary,
        content_html=item.content_html,
        content_text=item.content_text,
        canonical_url=key.canonical_url,
        source_url=item.source_url,
        author=item.author,
        author_url=item.author_url,
        cover_url=item.cover_url,
        language=item.language,
        published_at=item.published_at,
        content_updated_at=item.updated_at,
        fetched_at=fetched_at,
        url_hash=key.url_hash,
        raw_metadata_json=dict(item.raw_metadata),
    )


def _apply_update(
    row: ContentItem,
    item: NormalizedContentItem,
    key: DedupKey,
    fetched_at: datetime,
) -> None:
    if row.external_id is None:
        row.external_id = key.external_id
    row.content_type = item.content_type
    row.title = item.title
    row.summary = item.summary
    row.content_html = item.content_html
    row.content_text = item.content_text
    row.canonical_url = key.canonical_url
    row.source_url = item.source_url
    row.author = item.author
    row.author_url = item.author_url
    row.cover_url = item.cover_url
    row.language = item.language
    row.published_at = item.published_at
    row.content_updated_at = item.updated_at
    row.fetched_at = fetched_at
    row.url_hash = key.url_hash
    row.raw_metadata_json = dict(item.raw_metadata)


def _date(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))

