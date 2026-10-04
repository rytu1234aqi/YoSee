"""RSS and Atom mapping through RSSProvider. No database and no network."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from server.ingestion import FetchError, ParseError, ProviderError
from server.ingestion.fetcher import Fetcher
from server.ingestion.providers.generic.rss import RSSProvider
from server.models import ContentType, Source

_FEEDS = Path(__file__).parent / "fixtures" / "feeds"
_FEED_URL = "https://www.redblobgames.com/feed.xml"


def _feed(name: str) -> bytes:
    return (_FEEDS / name).read_bytes()


def _source(**overrides: Any) -> Source:
    values: dict[str, Any] = {
        "id": "redblobgames",
        "name": "Red Blob Games",
        "slug": "redblobgames",
        "provider_type": "rss",
        "fetch_interval_seconds": 21600,
        "config_json": {"feed_url": _FEED_URL},
    }
    values.update(overrides)
    return Source(**values)


def _provider(
    body: bytes,
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
    seen: list[httpx.Request] | None = None,
) -> RSSProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, content=body, headers=headers)

    return RSSProvider(Fetcher(transport=httpx.MockTransport(handler)))


def test_rss_feed_maps_entries_and_skips_a_blank_title() -> None:
    seen: list[httpx.Request] = []
    provider = _provider(
        _feed("sample.rss"),
        headers={"etag": '"feed"', "last-modified": "Fri, 02 Jan 2026 00:00:00 GMT"},
        seen=seen,
    )
    source = _source()

    page = asyncio.run(provider.discover(source, cursor="ignored"))
    detailed = asyncio.run(provider.fetch_detail(page.items[0]))
    items = asyncio.run(provider.load(source))

    assert page.not_modified is False
    assert page.next_cursor is None
    assert page.etag == '"feed"'
    assert page.last_modified == "Fri, 02 Jan 2026 00:00:00 GMT"
    assert detailed is page.items[0]
    assert len(page.items) == 3
    assert [item.title for item in items] == ["Hexagonal Grids", "Plain only"]

    article = items[0]
    assert article.source_id == "redblobgames"
    assert article.external_id == "hex-grids"
    assert article.content_type is ContentType.ARTICLE
    assert article.summary == "A guide to hex grids."
    assert article.content_html == "<p>Full article</p>"
    assert article.content_text is None
    assert article.canonical_url == "https://www.redblobgames.com/grids/hexagons/"
    assert article.source_url == _FEED_URL
    assert article.author == "Amit Patel"
    assert article.language == "en-us"
    assert article.published_at == datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    assert article.updated_at is None
    assert article.raw_metadata == {
        "feed_url": _FEED_URL,
        "tags": ["programming", "maps"],
    }

    plain = items[1]
    assert plain.external_id is None
    assert plain.canonical_url == "https://www.redblobgames.com/plain"
    assert plain.summary == "Just a description."
    assert plain.content_html is None
    assert plain.published_at is None
    assert plain.raw_metadata["tags"] == []
    assert seen[0].url == _FEED_URL


def test_atom_feed_keeps_published_and_updated_apart() -> None:
    feed_url = "https://example.com/atom.xml"
    provider = _provider(_feed("sample.atom"))
    source = _source(id="gamedev-blog", config_json={"feed_url": feed_url})
    items = asyncio.run(provider.load(source))

    assert len(items) == 1
    article = items[0]
    assert article.source_id == "gamedev-blog"
    assert article.external_id == "tag:example.com,2026:entity-systems"
    assert article.title == "Entity Systems"
    assert article.canonical_url == "https://example.com/posts/entity-systems"
    assert article.author == "Casey"
    assert article.summary == "Notes on entities."
    assert article.content_html == "<p>Body</p>"
    assert article.published_at == datetime(2026, 6, 1, 8, 30, tzinfo=UTC)
    assert article.updated_at == datetime(2026, 6, 2, 1, 0, tzinfo=UTC)


def test_not_modified_skips_parsing() -> None:
    seen: list[httpx.Request] = []
    provider = _provider(
        b"this is not a feed",
        status=304,
        headers={"etag": '"same"', "last-modified": "Thu, 01 Jan 2026 00:00:00 GMT"},
        seen=seen,
    )
    source = _source(etag='"same"', last_modified="Wed, 01 Jan 2025 00:00:00 GMT")

    page = asyncio.run(provider.discover(source))
    items = asyncio.run(provider.load(source))

    assert page.not_modified is True
    assert page.items == []
    assert page.etag == '"same"'
    assert page.last_modified == "Thu, 01 Jan 2026 00:00:00 GMT"
    assert items == []
    assert seen[0].headers["if-none-match"] == '"same"'
    assert seen[0].headers["if-modified-since"] == "Wed, 01 Jan 2025 00:00:00 GMT"
    assert len(seen) == 2


def test_empty_feed_is_not_an_error() -> None:
    body = b'<?xml version="1.0"?><rss version="2.0"><channel><title>Empty</title></channel></rss>'
    provider = _provider(body, headers={"etag": '"empty"'})

    page = asyncio.run(provider.discover(_source()))

    assert page.items == []
    assert page.not_modified is False
    assert page.etag == '"empty"'


def test_unreadable_document_raises_parse_error() -> None:
    provider = _provider(b"this is not a feed")

    with pytest.raises(ParseError) as caught:
        asyncio.run(provider.load(_source()))

    assert caught.value.url == _FEED_URL


def test_missing_feed_url_does_not_fetch() -> None:
    seen: list[httpx.Request] = []
    provider = _provider(b"", seen=seen)

    with pytest.raises(ProviderError) as caught:
        asyncio.run(provider.discover(_source(config_json={"feed_url": "  "})))

    assert seen == []
    assert caught.value.source_id == "redblobgames"


def test_http_failure_is_not_wrapped() -> None:
    provider = _provider(b"missing", status=404)

    with pytest.raises(FetchError) as caught:
        asyncio.run(provider.load(_source()))

    assert caught.value.status_code == 404


def test_normalize_rejects_an_unexpected_raw_item() -> None:
    provider = _provider(b"")

    with pytest.raises(ParseError):
        asyncio.run(provider.normalize({"title": "nope"}))
