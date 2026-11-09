"""RSS 2.0 and Atom adapter.

HTTP stays in Fetcher. This module parses the document and maps entries onto
``NormalizedContentItem``. It does not write the database.
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from time import struct_time
from typing import Any
from urllib.parse import urljoin, urlparse

import feedparser

from server.ingestion.errors import ParseError, ProviderError
from server.ingestion.fetcher import Fetcher
from server.ingestion.normalizer import NormalizedContentItem
from server.ingestion.providers.base import DiscoverPage, SourceAdapter
from server.models.content import ContentType
from server.models.source import Source

_HTML_TYPES = frozenset({"text/html", "application/xhtml+xml", "html"})


@dataclass(frozen=True, slots=True)
class RssEntry:
    """One feed entry after parsing, before the shared content model."""

    source_id: str
    feed_url: str
    title: str
    link: str
    external_id: str | None
    summary: str | None
    author: str | None
    content_html: str | None
    content_text: str | None
    published_at: datetime | None
    updated_at: datetime | None
    language: str | None
    tags: tuple[str, ...]
    cover_url: str | None


class RSSProvider(SourceAdapter):
    """Read one RSS 2.0 or Atom document into normalized articles.

    ``feed_url`` comes from ``source.config_json``. A feed is a single document,
    so ``cursor`` is ignored. A 304 returns no items and sets ``not_modified``.
    """

    def __init__(self, fetcher: Fetcher) -> None:
        self._fetcher = fetcher

    async def discover(self, source: Source, cursor: str | None = None) -> DiscoverPage:
        """Fetch the feed. Pass the source cache validators for a conditional GET."""

        del cursor
        feed_url = _feed_url(source)
        response = await self._fetcher.get(
            feed_url,
            etag=source.etag,
            last_modified=source.last_modified,
        )
        page = DiscoverPage(
            items=[],
            not_modified=response.not_modified,
            etag=response.etag,
            last_modified=response.last_modified,
        )
        if response.not_modified:
            return page
        return DiscoverPage(
            items=_parse_entries(response.body, feed_url, source.id),
            etag=response.etag,
            last_modified=response.last_modified,
        )

    async def fetch_detail(self, raw_item: Any) -> Any:
        """RSS and Atom entries are complete in the feed document."""

        return raw_item

    async def normalize(self, raw_item: Any) -> NormalizedContentItem:
        """Map one parsed entry onto the shared content model."""

        if not isinstance(raw_item, RssEntry):
            raise ParseError("RSS normalize expected a parsed feed entry", url="")
        return NormalizedContentItem(
            source_id=raw_item.source_id,
            external_id=raw_item.external_id,
            content_type=ContentType.ARTICLE,
            title=raw_item.title,
            summary=raw_item.summary,
            content_html=raw_item.content_html,
            content_text=raw_item.content_text,
            canonical_url=raw_item.link,
            source_url=raw_item.feed_url,
            author=raw_item.author,
            cover_url=raw_item.cover_url,
            language=raw_item.language,
            published_at=raw_item.published_at,
            updated_at=raw_item.updated_at,
            raw_metadata={"feed_url": raw_item.feed_url, "tags": list(raw_item.tags)},
        )


def _feed_url(source: Source) -> str:
    config = source.config_json
    raw = config.get("feed_url") if isinstance(config, dict) else None
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    raise ProviderError(
        f"source {source.id} is missing config_json.feed_url",
        source_id=source.id,
    )


def _parse_entries(body: bytes, feed_url: str, source_id: str) -> list[RssEntry]:
    parsed: Any = feedparser.parse(body)
    entries: list[Any] = list(getattr(parsed, "entries", []) or [])
    # A partial parse that still has entries is usable. Only an empty failure is fatal.
    if getattr(parsed, "bozo", False) and not entries:
        reason = getattr(parsed, "bozo_exception", None)
        message = f"could not parse feed {feed_url}: {reason or 'unreadable feed'}"
        if isinstance(reason, BaseException):
            raise ParseError(message, url=feed_url) from reason
        raise ParseError(message, url=feed_url)
    feed = getattr(parsed, "feed", {})
    language = _text(feed.get("language") if isinstance(feed, dict) else None)
    return [_entry(item, feed_url, source_id, language) for item in entries]


def _entry(item: Any, feed_url: str, source_id: str, language: str | None) -> RssEntry:
    content_html, content_text = _content(item.get("content") if "content" in item else None)
    return RssEntry(
        source_id=source_id,
        feed_url=feed_url,
        title=_text(item.get("title") if "title" in item else None) or "",
        link=_link(item, feed_url),
        external_id=_text(item.get("id") if "id" in item else None),
        summary=_text(item.get("summary") if "summary" in item else None),
        author=_author(item),
        content_html=content_html,
        content_text=content_text,
        published_at=_when(item, "published_parsed"),
        updated_at=_when(item, "updated_parsed"),
        language=language,
        tags=_tags(item.get("tags") if "tags" in item else None),
        cover_url=_cover_url(item, feed_url),
    )


def _link(item: Any, feed_url: str) -> str:
    raw = _text(item.get("link")) if "link" in item else None
    if raw is None:
        raw = _first_href(item.get("links") if "links" in item else None)
    if raw is None:
        return ""
    return urljoin(feed_url, raw)


def _first_href(links: Any) -> str | None:
    if not isinstance(links, list):
        return None
    fallback: str | None = None
    for link in links:
        if not isinstance(link, dict):
            continue
        href = _text(link.get("href"))
        if href is None:
            continue
        if link.get("rel") in (None, "alternate"):
            return href
        fallback = fallback or href
    return fallback


def _author(item: Any) -> str | None:
    authors = item.get("authors") if "authors" in item else None
    if isinstance(authors, list):
        for author in authors:
            if isinstance(author, dict):
                name = _text(author.get("name"))
                if name:
                    return name
    if "author" not in item:
        return None
    return _text(item.get("author"))


def _content(raw: Any) -> tuple[str | None, str | None]:
    if isinstance(raw, str):
        return _text(raw), None
    if not isinstance(raw, list):
        return None, None
    html: str | None = None
    plain: str | None = None
    for block in raw:
        value, kind = _content_block(block)
        if value is None:
            continue
        if kind in _HTML_TYPES:
            html = html or value
        elif kind == "text/plain":
            plain = plain or value
        else:
            html = html or value
    return html, plain


def _content_block(block: Any) -> tuple[str | None, str]:
    if isinstance(block, str):
        return _text(block), "text/html"
    if not isinstance(block, dict):
        return None, ""
    kind = str(block.get("type") or "text/html").split(";", 1)[0].strip().lower()
    return _text(block.get("value")), kind or "text/html"


def _when(item: Any, key: str) -> datetime | None:
    # FeedParserDict aliases updated to published. Membership is the real field.
    if key not in item:
        return None
    value = item.get(key)
    if not isinstance(value, struct_time):
        return None
    return datetime(
        value.tm_year,
        value.tm_mon,
        value.tm_mday,
        value.tm_hour,
        value.tm_min,
        value.tm_sec,
        tzinfo=UTC,
    )


def _tags(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    tags: list[str] = []
    for tag in raw:
        term = _text(tag.get("term")) if isinstance(tag, dict) else _text(tag)
        if term and term not in tags:
            tags.append(term)
    return tuple(tags)


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


_COVER_SKIP_WORDS = (
    "icon",
    "avatar",
    "emoji",
    "pixel",
    "tracking",
    "badge",
    "sponsor",
    "logo",
    "1x1",
    "gravatar",
)
# \x27 is a single quote, which cannot appear in this single-quoted raw string.
_IMG_SRC_RE = re.compile(
    r'<img\b[^>]*?\ssrc\s*=\s*(["\x27])(.*?)\1', re.IGNORECASE | re.DOTALL
)


def _cover_url(item: Any, feed_url: str) -> str | None:
    """Pick the first usable image for a feed entry; never raises on bad data."""

    try:
        for raw in _cover_candidates(item):
            resolved = _clean_cover_url(raw, feed_url)
            if resolved is not None:
                return resolved
    except (AttributeError, TypeError, ValueError):
        return None
    return None


def _cover_candidates(item: Any) -> Iterator[str]:
    if not isinstance(item, dict):
        return
    # 1. media:thumbnail — feedparser gives a list of dicts carrying a url.
    for block in _dicts(item.get("media_thumbnail")):
        url = _text(block.get("url"))
        if url:
            yield url
    # 2. media:content — only the image entries.
    for block in _dicts(item.get("media_content")):
        medium = str(block.get("medium") or "").lower()
        kind = str(block.get("type") or "").lower()
        if medium == "image" or kind.startswith("image/"):
            url = _text(block.get("url"))
            if url:
                yield url
    # 3. enclosure image links. Atom links carry rel=enclosure; the separate
    #    enclosures list already implies it and has no rel field.
    for group, needs_rel in (
        (item.get("links"), True),
        (item.get("enclosures"), False),
    ):
        for link in _dicts(group):
            if needs_rel and link.get("rel") != "enclosure":
                continue
            if not str(link.get("type") or "").lower().startswith("image/"):
                continue
            for key in ("href", "url"):
                url = _text(link.get(key))
                if url:
                    yield url
    # 4. the first <img src> in the summary / content HTML.
    summary = item.get("summary")
    if isinstance(summary, str):
        yield from _img_sources(summary)
    content = item.get("content")
    if isinstance(content, str):
        yield from _img_sources(content)
    for block in _dicts(content):
        value = block.get("value")
        if isinstance(value, str):
            yield from _img_sources(value)


def _img_sources(html: str) -> Iterator[str]:
    for match in _IMG_SRC_RE.finditer(html):
        url = match.group(2).strip()
        if url:
            yield url


def _dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [block for block in value if isinstance(block, dict)]


def _clean_cover_url(raw: str, feed_url: str) -> str | None:
    stripped = raw.strip()
    if not stripped:
        return None
    try:
        resolved = urljoin(feed_url, stripped)
        parsed = urlparse(resolved)
    except (TypeError, ValueError):
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    if any(word in resolved.lower() for word in _COVER_SKIP_WORDS):
        return None
    return resolved
