"""Stack Exchange questions mapped through StackExchangeProvider. No database."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from server.ingestion import ParseError, ProviderError, RateLimitError
from server.ingestion.fetcher import Fetcher
from server.ingestion.providers.platforms.stackexchange import (
    StackExchangeProvider,
    questions_url,
)
from server.models import ContentType, Source

_FIXTURE = Path(__file__).parent / "fixtures" / "stackexchange" / "questions.json"
_URL = questions_url(site="gamedev", sort="activity", pagesize=50, page=1)


def _source(**overrides: Any) -> Source:
    values: dict[str, Any] = {
        "id": "gamedev-stackexchange",
        "name": "Game Development Stack Exchange",
        "slug": "gamedev-stackexchange",
        "provider_type": "stackexchange",
        "fetch_interval_seconds": 1800,
        "config_json": {"site": "gamedev"},
    }
    values.update(overrides)
    return Source(**values)


def _provider(
    body: bytes,
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
    seen: list[httpx.Request] | None = None,
) -> StackExchangeProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, content=body, headers=headers)

    return StackExchangeProvider(Fetcher(transport=httpx.MockTransport(handler)))


def test_questions_map_onto_normalized_items_and_skip_a_blank_title() -> None:
    seen: list[httpx.Request] = []
    provider = _provider(
        _FIXTURE.read_bytes(),
        headers={"etag": '"page"', "last-modified": "Fri, 02 Jan 2026 00:00:00 GMT"},
        seen=seen,
    )
    source = _source()

    page = asyncio.run(provider.discover(source))
    items = asyncio.run(provider.load(source))

    assert str(seen[0].url) == _URL
    assert page.next_cursor == "2"
    assert page.etag == '"page"'
    assert page.last_modified == "Fri, 02 Jan 2026 00:00:00 GMT"
    assert len(items) == 1
    question = items[0]
    assert question.source_id == "gamedev-stackexchange"
    assert question.external_id == "42"
    assert question.content_type is ContentType.QUESTION
    assert question.title == 'How do I "rotate" a vector?'
    assert question.summary == "I have a vector."
    assert question.content_html == "<p>I have a vector.</p>"
    assert question.content_text == "I have a vector."
    assert (
        question.canonical_url == "https://gamedev.stackexchange.com/questions/42/rotate-a-vector"
    )
    assert question.source_url == _URL
    assert question.author == "Amit & Co"
    assert question.author_url == "https://gamedev.stackexchange.com/users/7/amit"
    assert question.published_at == datetime.fromtimestamp(1767268800, UTC)
    assert question.updated_at == datetime.fromtimestamp(1767355200, UTC)
    assert question.raw_metadata["tags"] == ["math", "vectors"]
    assert question.raw_metadata["score"] == 8
    assert question.raw_metadata["answer_count"] == 1


def test_not_modified_skips_parsing() -> None:
    provider = _provider(b"", status=304, headers={"etag": '"same"'})

    page = asyncio.run(provider.discover(_source(etag='"same"')))

    assert page.not_modified is True
    assert page.items == []
    assert page.etag == '"same"'


def test_missing_site_and_bad_pagesize_are_config_errors() -> None:
    provider = _provider(b"{}")

    with pytest.raises(ProviderError, match="config_json.site"):
        asyncio.run(provider.discover(_source(config_json={})))
    with pytest.raises(ProviderError, match="pagesize"):
        asyncio.run(provider.discover(_source(config_json={"site": "gamedev", "pagesize": 0})))


def test_throttle_and_unreadable_json_fail_the_page() -> None:
    throttled = _provider(b'{"error_id": 502, "error_message": "too many requests"}')
    broken = _provider(b"not-json")

    with pytest.raises(RateLimitError, match="too many requests"):
        asyncio.run(throttled.discover(_source()))
    with pytest.raises(ParseError, match="could not parse"):
        asyncio.run(broken.discover(_source()))
