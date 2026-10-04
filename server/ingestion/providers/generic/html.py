"""Config-driven HTML list adapter.

HTTP stays in Fetcher. Selectors come from ``source.config_json``. This module
maps one listing page onto ``NormalizedContentItem`` and does not write the database.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag
from soupsieve import SelectorSyntaxError

from server.ingestion.errors import ParseError, ProviderError
from server.ingestion.fetcher import Fetcher
from server.ingestion.normalizer import NormalizedContentItem
from server.ingestion.providers.base import DiscoverPage, SourceAdapter
from server.models.content import ContentType
from server.models.source import Source


@dataclass(frozen=True, slots=True)
class HtmlField:
    """One CSS extraction. ``attr`` is ``None`` when the value is the element text."""

    selector: str
    attr: str | None = None


@dataclass(frozen=True, slots=True)
class HtmlEntry:
    """One listing row after parsing, before the shared content model."""

    source_id: str
    list_url: str
    title: str
    link: str
    summary: str | None
    cover_url: str | None
    author: str | None
    published_at: datetime | None


class GenericHTMLProvider(SourceAdapter):
    """Read one HTML listing into normalized articles.

    The list URL, item selector, and fields live in ``source.config_json``. A feed
    is one page, so ``cursor`` is ignored. A 304 returns no items. An item with a
    blank title or URL is skipped by the shared collector. A page that matches no
    items raises, because that usually means the selectors no longer fit.
    """

    def __init__(self, fetcher: Fetcher) -> None:
        self._fetcher = fetcher

    async def discover(self, source: Source, cursor: str | None = None) -> DiscoverPage:
        """Fetch the listing. Pass the source cache validators for a conditional GET."""

        del cursor
        settings = _settings(source)
        response = await self._fetcher.get(
            settings.list_url,
            etag=source.etag,
            last_modified=source.last_modified,
        )
        if response.not_modified:
            return DiscoverPage(
                items=[],
                not_modified=True,
                etag=response.etag,
                last_modified=response.last_modified,
            )
        items = _entries(response.body, settings, source.id)
        return DiscoverPage(
            items=items,
            etag=response.etag,
            last_modified=response.last_modified,
        )

    async def fetch_detail(self, raw_item: Any) -> Any:
        """Listing fields are complete. Article pages are not fetched."""

        return raw_item

    async def normalize(self, raw_item: Any) -> NormalizedContentItem:
        """Map one listing row onto the shared content model."""

        if not isinstance(raw_item, HtmlEntry):
            raise ParseError("HTML normalize expected a parsed listing row", url="")
        return NormalizedContentItem(
            source_id=raw_item.source_id,
            content_type=ContentType.ARTICLE,
            title=raw_item.title,
            summary=raw_item.summary,
            canonical_url=raw_item.link,
            source_url=raw_item.list_url,
            author=raw_item.author,
            cover_url=raw_item.cover_url,
            published_at=raw_item.published_at,
            raw_metadata={"list_url": raw_item.list_url},
        )


@dataclass(frozen=True, slots=True)
class _HtmlSettings:
    list_url: str
    item: str
    title: HtmlField
    url: HtmlField
    summary: HtmlField | None
    cover: HtmlField | None
    author: HtmlField | None
    published_at: HtmlField | None


def _settings(source: Source) -> _HtmlSettings:
    config = source.config_json if isinstance(source.config_json, dict) else {}
    return _HtmlSettings(
        list_url=_list_url(config, source.id),
        item=_item_selector(config, source.id),
        title=_required_field(config, "title", source.id, default_attr=None),
        url=_required_field(config, "url", source.id, default_attr="href"),
        summary=_optional_field(config, "summary", source.id, default_attr=None),
        cover=_optional_field(config, "cover", source.id, default_attr="src"),
        author=_optional_field(config, "author", source.id, default_attr=None),
        published_at=_optional_field(config, "published_at", source.id, default_attr=None),
    )


def _list_url(config: dict[str, Any], source_id: str) -> str:
    listing = config.get("list")
    url = listing.get("url") if isinstance(listing, dict) else None
    if isinstance(url, str) and url.strip():
        return url.strip()
    raise ProviderError(f"source {source_id} is missing config_json.list.url", source_id=source_id)


def _item_selector(config: dict[str, Any], source_id: str) -> str:
    selectors = config.get("selectors")
    item = selectors.get("item") if isinstance(selectors, dict) else None
    if isinstance(item, str) and item.strip():
        return item.strip()
    raise ProviderError(
        f"source {source_id} is missing config_json.selectors.item",
        source_id=source_id,
    )


def _required_field(
    config: dict[str, Any],
    name: str,
    source_id: str,
    *,
    default_attr: str | None,
) -> HtmlField:
    field = _optional_field(config, name, source_id, default_attr=default_attr)
    if field is None:
        raise ProviderError(
            f"source {source_id} is missing config_json.fields.{name}",
            source_id=source_id,
        )
    return field


def _optional_field(
    config: dict[str, Any],
    name: str,
    source_id: str,
    *,
    default_attr: str | None,
) -> HtmlField | None:
    fields = config.get("fields")
    raw = fields.get(name) if isinstance(fields, dict) else None
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ProviderError(
            f"source {source_id} field {name} must be a mapping",
            source_id=source_id,
        )
    selector = raw.get("selector")
    if not isinstance(selector, str) or not selector.strip():
        raise ProviderError(
            f"source {source_id} is missing config_json.fields.{name}.selector",
            source_id=source_id,
        )
    attr = raw.get("attr", default_attr)
    if attr is not None and (not isinstance(attr, str) or not attr.strip()):
        raise ProviderError(
            f"source {source_id} field {name} attr must be a non-empty string",
            source_id=source_id,
        )
    return HtmlField(selector.strip(), attr.strip() if isinstance(attr, str) else None)


def _entries(body: bytes, settings: _HtmlSettings, source_id: str) -> list[HtmlEntry]:
    soup = BeautifulSoup(body, "lxml")
    rows = _select(soup, settings.item, settings.list_url)
    if not rows:
        raise ParseError(
            f"HTML listing {settings.list_url} matched no items",
            url=settings.list_url,
        )
    return [_entry(row, settings, source_id) for row in rows]


def _entry(row: Tag, settings: _HtmlSettings, source_id: str) -> HtmlEntry:
    return HtmlEntry(
        source_id=source_id,
        list_url=settings.list_url,
        title=_value(row, settings.title, settings.list_url) or "",
        link=_absolute(settings.list_url, _value(row, settings.url, settings.list_url)),
        summary=_maybe(row, settings.summary, settings.list_url),
        cover_url=_absolute(settings.list_url, _maybe(row, settings.cover, settings.list_url))
        or None,
        author=_maybe(row, settings.author, settings.list_url),
        published_at=_when(_maybe(row, settings.published_at, settings.list_url)),
    )


def _maybe(row: Tag, field: HtmlField | None, page_url: str) -> str | None:
    if field is None:
        return None
    return _value(row, field, page_url)


def _value(row: Tag, field: HtmlField, page_url: str) -> str | None:
    found = _select_one(row, field.selector, page_url)
    if found is None:
        return None
    if field.attr is None:
        text = found.get_text(" ", strip=True)
        return text or None
    raw = found.get(field.attr)
    if isinstance(raw, list):
        raw = raw[0] if raw else None
    if not isinstance(raw, str):
        return None
    stripped = raw.strip()
    return stripped or None


def _select(node: BeautifulSoup | Tag, selector: str, page_url: str) -> list[Tag]:
    try:
        found = node.select(selector)
    except SelectorSyntaxError as exc:
        raise ParseError(f"invalid selector {selector!r} for {page_url}", url=page_url) from exc
    return [item for item in found if isinstance(item, Tag)]


def _select_one(node: Tag, selector: str, page_url: str) -> Tag | None:
    try:
        found = node.select_one(selector)
    except SelectorSyntaxError as exc:
        raise ParseError(f"invalid selector {selector!r} for {page_url}", url=page_url) from exc
    return found if isinstance(found, Tag) else None


def _absolute(base: str, value: str | None) -> str:
    if not value:
        return ""
    return urljoin(base, value)


def _when(value: str | None) -> datetime | None:
    if value is None:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed
