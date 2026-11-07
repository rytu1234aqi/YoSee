"""Per-source import caps for archive-style feeds.

Some publishers put their entire back catalogue in one feed document. The cap
keeps the leading (newest) entries so a first fetch cannot flood the reader.
"""

import asyncio
from typing import Any

import httpx
import pytest
from pydantic import ValidationError
from server.ingestion.fetcher import Fetcher
from server.ingestion.pipeline import FetchResult, IngestionPipeline
from server.ingestion.sources import SourceConfig, source_from_config
from server.models import ContentItem, Source
from sqlalchemy import select
from sqlalchemy.orm import Session

_FEED_URL = "https://example.com/feed.xml"


def _rss(count: int) -> bytes:
    items = "".join(
        f"<item><title>Post {index}</title>"
        f"<link>https://example.com/post/{index}</link>"
        f"<guid>post-{index}</guid><description>Summary {index}</description></item>"
        for index in range(count)
    )
    xml = (
        "<?xml version='1.0'?><rss version='2.0'><channel>"
        f"<title>Example</title>{items}</channel></rss>"
    )
    return xml.encode()


def _run(
    session: Session, cap: object | None, *, count: int = 5
) -> tuple[FetchResult, list[ContentItem]]:
    config: dict[str, Any] = {"feed_url": _FEED_URL}
    if cap is not None:
        config["max_items_per_run"] = cap
    source = Source(
        id="example",
        name="Example",
        slug="example",
        provider_type="rss",
        homepage_url="https://example.com/",
        fetch_interval_seconds=3600,
        config_json=config,
        enabled=True,
    )
    session.add(source)
    session.flush()

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, content=_rss(count), headers={"etag": '"feed-etag"'})

    fetcher = Fetcher(transport=httpx.MockTransport(handler))
    result = asyncio.run(IngestionPipeline(session, fetcher).fetch_source("example"))
    stored = list(session.scalars(select(ContentItem)))
    return result, stored


def _config(cap: object) -> dict[str, Any]:
    return {
        "id": "example",
        "name": "Example",
        "provider": {"type": "rss"},
        "config": {"feed_url": _FEED_URL},
        "fetch": {"interval_seconds": 3600, "max_items_per_run": cap},
    }


def test_source_config_carries_item_cap() -> None:
    config = SourceConfig.model_validate(_config(50))
    assert source_from_config(config).config_json["max_items_per_run"] == 50


def test_source_config_rejects_non_positive_cap() -> None:
    with pytest.raises(ValidationError):
        SourceConfig.model_validate(_config(0))


def test_item_cap_keeps_only_the_leading_entries(session: Session) -> None:
    result, stored = _run(session, 2)

    assert result.ok
    assert result.discovered == 2
    assert result.inserted == 2
    assert len(stored) == 2
    assert {item.title for item in stored} == {"Post 0", "Post 1"}


def test_without_a_cap_every_entry_is_imported(session: Session) -> None:
    result, stored = _run(session, None)

    assert result.discovered == 5
    assert result.inserted == 5
    assert len(stored) == 5


@pytest.mark.parametrize("cap", [0, -1, True, "2", 1.5])
def test_invalid_caps_are_ignored(session: Session, cap: object) -> None:
    result, _stored = _run(session, cap)
    assert result.discovered == 5
