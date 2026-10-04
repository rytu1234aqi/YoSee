"""Stack Exchange API adapter.

HTTP stays in Fetcher. One fetch reads a single page of questions and maps them
onto ``NormalizedContentItem``. It does not write the database.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from html import unescape
from typing import Any
from urllib.parse import urlencode

from bs4 import BeautifulSoup

from server.ingestion.errors import ParseError, ProviderError, RateLimitError
from server.ingestion.fetcher import Fetcher
from server.ingestion.normalizer import NormalizedContentItem
from server.ingestion.providers.base import DiscoverPage, SourceAdapter
from server.models.content import ContentType
from server.models.source import Source

_API = "https://api.stackexchange.com/2.3/questions"
_SORTS = frozenset({"activity", "creation", "votes", "hot"})
_SUMMARY_LIMIT = 280


@dataclass(frozen=True, slots=True)
class StackQuestion:
    """One question after parsing, before the shared content model."""

    source_id: str
    site: str
    request_url: str
    question_id: str | None
    title: str
    link: str
    body_html: str | None
    body_text: str | None
    author: str | None
    author_url: str | None
    tags: tuple[str, ...]
    published_at: datetime | None
    updated_at: datetime | None
    score: int | None
    answer_count: int | None


class StackExchangeProvider(SourceAdapter):
    """Read one page of questions from the Stack Exchange API.

    ``site`` comes from ``source.config_json``. ``sort`` defaults to ``activity``
    and ``pagesize`` defaults to 50. ``cursor`` is a page number. A 304 returns
    no items. Further pages are left for a later run so one fetch stays one request.
    """

    def __init__(self, fetcher: Fetcher) -> None:
        self._fetcher = fetcher

    async def discover(self, source: Source, cursor: str | None = None) -> DiscoverPage:
        """Fetch one page. Pass the source cache validators for a conditional GET."""

        site, sort, pagesize = _settings(source)
        page_number = _page(cursor)
        url = questions_url(site=site, sort=sort, pagesize=pagesize, page=page_number)
        response = await self._fetcher.get(
            url,
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
        payload = _payload(response.body, url)
        has_more = bool(payload.get("has_more"))
        return DiscoverPage(
            items=_questions(payload, source.id, site, url),
            next_cursor=str(page_number + 1) if has_more else None,
            etag=response.etag,
            last_modified=response.last_modified,
        )

    async def fetch_detail(self, raw_item: Any) -> Any:
        """The questions endpoint already includes the body."""

        return raw_item

    async def normalize(self, raw_item: Any) -> NormalizedContentItem:
        """Map one question onto the shared content model."""

        if not isinstance(raw_item, StackQuestion):
            raise ParseError("Stack Exchange normalize expected a parsed question", url="")
        summary = raw_item.body_text
        if summary is not None and len(summary) > _SUMMARY_LIMIT:
            summary = summary[:_SUMMARY_LIMIT].rstrip()
        return NormalizedContentItem(
            source_id=raw_item.source_id,
            external_id=raw_item.question_id,
            content_type=ContentType.QUESTION,
            title=raw_item.title,
            summary=summary,
            content_html=raw_item.body_html,
            content_text=raw_item.body_text,
            canonical_url=raw_item.link,
            source_url=raw_item.request_url,
            author=raw_item.author,
            author_url=raw_item.author_url,
            published_at=raw_item.published_at,
            updated_at=raw_item.updated_at,
            raw_metadata={
                "site": raw_item.site,
                "tags": list(raw_item.tags),
                "score": raw_item.score,
                "answer_count": raw_item.answer_count,
            },
        )


def questions_url(*, site: str, sort: str, pagesize: int, page: int) -> str:
    """Return the questions URL. Parameter order is stable."""

    query = urlencode(
        [
            ("site", site),
            ("order", "desc"),
            ("sort", sort),
            ("pagesize", pagesize),
            ("page", page),
            ("filter", "withbody"),
        ]
    )
    return f"{_API}?{query}"


def _settings(source: Source) -> tuple[str, str, int]:
    config = source.config_json if isinstance(source.config_json, dict) else {}
    site = config.get("site")
    if not isinstance(site, str) or not site.strip():
        raise ProviderError(f"source {source.id} is missing config_json.site", source_id=source.id)
    sort = config.get("sort", "activity")
    if not isinstance(sort, str) or sort not in _SORTS:
        raise ProviderError(
            f"source {source.id} has an unsupported sort {sort!r}",
            source_id=source.id,
        )
    pagesize = config.get("pagesize", 50)
    if not isinstance(pagesize, int) or isinstance(pagesize, bool) or not 1 <= pagesize <= 100:
        raise ProviderError(
            f"source {source.id} pagesize must be an integer from 1 to 100",
            source_id=source.id,
        )
    return site.strip(), sort, pagesize


def _page(cursor: str | None) -> int:
    if cursor is None or not cursor.strip():
        return 1
    if cursor.strip().isdigit() and int(cursor) >= 1:
        return int(cursor)
    raise ProviderError(f"page cursor {cursor!r} is not a positive integer", source_id="")


def _payload(body: bytes, url: str) -> dict[str, Any]:
    try:
        raw = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ParseError(f"could not parse Stack Exchange response {url}", url=url) from exc
    if not isinstance(raw, dict):
        raise ParseError(f"Stack Exchange response {url} was not an object", url=url)
    error_id = raw.get("error_id")
    if error_id is not None:
        message = _text(raw.get("error_message")) or "Stack Exchange request failed"
        if error_id == 502 or raw.get("error_name") == "throttle_violation":
            raise RateLimitError(message, url=url, status_code=502)
        raise ParseError(f"{message} ({url})", url=url)
    if not isinstance(raw.get("items"), list):
        raise ParseError(f"Stack Exchange response {url} has no items", url=url)
    return raw


def _questions(payload: dict[str, Any], source_id: str, site: str, url: str) -> list[StackQuestion]:
    items = payload.get("items")
    if not isinstance(items, list):
        return []
    return [_question(item, source_id, site, url) for item in items if isinstance(item, dict)]


def _question(item: dict[str, Any], source_id: str, site: str, url: str) -> StackQuestion:
    body_html = _text(item.get("body"))
    body_text = _plain(body_html)
    owner_raw = item.get("owner")
    owner: dict[str, Any] = owner_raw if isinstance(owner_raw, dict) else {}
    author = _text(owner.get("display_name"))
    tags_raw = item.get("tags")
    tags: list[Any] = tags_raw if isinstance(tags_raw, list) else []
    return StackQuestion(
        source_id=source_id,
        site=site,
        request_url=url,
        question_id=_question_id(item.get("question_id")),
        title=unescape(_text(item.get("title")) or ""),
        link=_text(item.get("link")) or "",
        body_html=body_html,
        body_text=body_text,
        author=unescape(author) if author else None,
        author_url=_text(owner.get("link")),
        tags=tuple(tag for tag in (_text(raw) for raw in tags) if tag),
        published_at=_when(item.get("creation_date")),
        updated_at=_when(item.get("last_activity_date")),
        score=_whole(item.get("score")),
        answer_count=_whole(item.get("answer_count")),
    )


def _question_id(value: Any) -> str | None:
    number = _whole(value)
    if number is None:
        return None
    return str(number)


def _whole(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _when(value: Any) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return datetime.fromtimestamp(value, UTC)


def _plain(html: str | None) -> str | None:
    if html is None:
        return None
    text = BeautifulSoup(html, "lxml").get_text(" ", strip=True)
    return text or None


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None
