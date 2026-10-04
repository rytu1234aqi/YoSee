"""HTML listings mapped through GenericHTMLProvider. No database and no network."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from server.ingestion import ParseError, ProviderError
from server.ingestion.fetcher import Fetcher
from server.ingestion.providers.generic.html import GenericHTMLProvider
from server.models import ContentType, Source

_PAGE = Path(__file__).parent / "fixtures" / "html" / "lostgarden.html"
_URL = "https://lostgarden.com/blog/"
_CONFIG: dict[str, Any] = {
    "list": {"url": _URL},
    "selectors": {"item": "li.wp-block-post"},
    "fields": {
        "title": {"selector": "h3.wp-block-post-title a"},
        "url": {"selector": "h3.wp-block-post-title a", "attr": "href"},
        "summary": {"selector": ".wp-block-post-excerpt__excerpt"},
        "cover": {"selector": "img", "attr": "src"},
        "published_at": {"selector": "time", "attr": "datetime"},
    },
}


def _source(**overrides: Any) -> Source:
    values: dict[str, Any] = {
        "id": "lostgarden",
        "name": "Lost Garden",
        "slug": "lostgarden",
        "provider_type": "html",
        "fetch_interval_seconds": 21600,
        "config_json": _CONFIG,
    }
    values.update(overrides)
    return Source(**values)


def _provider(
    body: bytes,
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
) -> GenericHTMLProvider:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=body, headers=headers)

    return GenericHTMLProvider(Fetcher(transport=httpx.MockTransport(handler)))


def test_listing_maps_rows_and_skips_a_blank_title() -> None:
    provider = _provider(
        _PAGE.read_bytes(),
        headers={"etag": '"list"', "last-modified": "Fri, 02 Jan 2026 00:00:00 GMT"},
    )

    page = asyncio.run(provider.discover(_source(), cursor="ignored"))
    items = asyncio.run(provider.load(_source()))

    assert page.not_modified is False
    assert page.next_cursor is None
    assert page.etag == '"list"'
    assert len(page.items) == 3
    assert [item.title for item in items] == ["Creative Cohorts", "Plain"]
    article = items[0]
    assert article.content_type is ContentType.ARTICLE
    assert article.canonical_url == "https://lostgarden.com/2026/01/01/cohorts/"
    assert article.source_url == _URL
    assert article.summary == "A game design essay."
    assert article.cover_url == "https://lostgarden.com/images/cover.jpg"
    assert article.published_at == datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    assert items[1].canonical_url == "https://lostgarden.com/plain"
    assert items[1].summary is None
    assert items[1].cover_url is None
    assert items[1].published_at is None


def test_not_modified_skips_parsing() -> None:
    page = asyncio.run(
        _provider(b"", status=304, headers={"etag": '"same"'}).discover(_source(etag='"same"'))
    )

    assert page.not_modified is True
    assert page.items == []


def test_missing_list_url_is_a_config_error() -> None:
    with pytest.raises(ProviderError, match="list.url"):
        asyncio.run(_provider(b"<html></html>").discover(_source(config_json={})))


def test_a_page_with_no_items_is_a_parse_error() -> None:
    with pytest.raises(ParseError, match="matched no items"):
        asyncio.run(_provider(b"<html><body></body></html>").discover(_source()))


def test_a_bad_selector_is_a_parse_error() -> None:
    config = {
        "list": {"url": _URL},
        "selectors": {"item": "li["},
        "fields": {"title": {"selector": "a"}, "url": {"selector": "a"}},
    }

    with pytest.raises(ParseError, match="invalid selector"):
        asyncio.run(_provider(b"<html></html>").discover(_source(config_json=config)))
