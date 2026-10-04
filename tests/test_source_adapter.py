"""SourceAdapter contract and the shared normalized item."""

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError
from server.ingestion import DiscoverPage, NormalizedContentItem, SourceAdapter
from server.models import ContentType, Source


class _ListingAdapter(SourceAdapter):
    """Test double: the listing already contains the whole item."""

    async def discover(self, source: Source, cursor: str | None = None) -> DiscoverPage:
        assert cursor is None
        return DiscoverPage(items=[{"source_id": source.id, "title": "Hexagonal Grids"}])

    async def fetch_detail(self, raw_item: Any) -> Any:
        return raw_item

    async def normalize(self, raw_item: Any) -> NormalizedContentItem:
        return NormalizedContentItem(
            source_id=raw_item["source_id"],
            external_id="hex-grids",
            content_type=ContentType.ARTICLE,
            title=raw_item["title"],
            canonical_url="https://www.redblobgames.com/grids/hexagons/",
            published_at=datetime(2026, 1, 2, tzinfo=UTC),
        )


class _DownAdapter(SourceAdapter):
    async def discover(self, source: Source, cursor: str | None = None) -> DiscoverPage:
        return DiscoverPage(items=[])

    async def fetch_detail(self, raw_item: Any) -> Any:
        return raw_item

    async def normalize(self, raw_item: Any) -> NormalizedContentItem:
        raise AssertionError(raw_item)

    async def health_check(self) -> bool:
        return False


def _source() -> Source:
    return Source(
        id="redblobgames",
        name="Red Blob Games",
        slug="redblobgames",
        provider_type="rss",
        fetch_interval_seconds=21600,
    )


def test_source_adapter_cannot_be_constructed() -> None:
    with pytest.raises(TypeError):
        SourceAdapter()  # type: ignore[abstract]


def test_adapter_discovers_and_normalizes_without_a_database() -> None:
    adapter = _ListingAdapter()
    source = _source()

    page = asyncio.run(adapter.discover(source))
    detailed = asyncio.run(adapter.fetch_detail(page.items[0]))
    item = asyncio.run(adapter.normalize(detailed))

    assert page.next_cursor is None
    assert item.source_id == "redblobgames"
    assert item.content_type is ContentType.ARTICLE
    assert item.title == "Hexagonal Grids"
    assert item.content_html is None
    assert asyncio.run(adapter.health_check()) is True


def test_health_check_can_report_an_unreachable_source() -> None:
    assert asyncio.run(_DownAdapter().health_check()) is False


def test_normalized_item_rejects_blank_required_text() -> None:
    with pytest.raises(ValidationError):
        NormalizedContentItem(
            source_id="redblobgames",
            content_type="ARTICLE",
            title="  ",
            canonical_url="https://www.redblobgames.com/grids/hexagons/",
        )


def test_normalized_items_do_not_share_metadata() -> None:
    first = NormalizedContentItem(
        source_id="redblobgames",
        content_type=ContentType.POST,
        title="One",
        canonical_url="https://example.com/one",
    )
    second = NormalizedContentItem(
        source_id="redblobgames",
        content_type=ContentType.POST,
        title="Two",
        canonical_url="https://example.com/two",
    )

    first.raw_metadata["fetched"] = True

    assert second.raw_metadata == {}
    assert first.updated_at is None
