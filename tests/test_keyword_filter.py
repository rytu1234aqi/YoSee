"""Per-source keyword allowlist filtering, unit and pipeline level."""

import asyncio
from typing import Any

import httpx
import pytest
from server.ingestion.fetcher import Fetcher
from server.ingestion.keyword_filter import (
    KEYWORDS_CONFIG_KEY,
    MIN_BODY_CHARS,
    filter_items,
    item_matches,
    source_keywords,
)
from server.ingestion.normalizer import NormalizedContentItem
from server.ingestion.pipeline import FetchResult, IngestionPipeline
from server.ingestion.providers import DiscoverPage
from server.models import ContentItem, ContentType, FetchRun, Source
from sqlalchemy import select
from sqlalchemy.orm import Session

_FEED_URL = "https://example.com/feed.xml"
_BODY = "A long description paragraph that clearly exceeds the minimum body length."


def _item(
    *,
    title: str = "A plain title",
    summary: str | None = None,
    content_text: str | None = None,
    url: str = "https://example.com/post/1",
) -> NormalizedContentItem:
    return NormalizedContentItem(
        source_id="example",
        external_id=url.rsplit("/", 1)[-1],
        content_type=ContentType.ARTICLE,
        title=title,
        summary=summary,
        content_text=content_text,
        canonical_url=url,
    )


def _source(config: Any) -> Source:
    return Source(
        id="example",
        name="Example",
        slug="example",
        provider_type="rss",
        fetch_interval_seconds=3600,
        config_json=config,
    )


# --- item_matches / filter_items -------------------------------------------------


def test_empty_keywords_disable_filtering() -> None:
    items = [
        _item(url="https://example.com/1"),
        _item(title="Title only stub", url="https://example.com/2"),
    ]
    kept, filtered = filter_items(items, [])

    assert kept == items
    assert filtered == 0
    assert item_matches(items[0], []) is True
    assert item_matches(items[1], []) is True


def test_keyword_found_in_title_summary_or_body() -> None:
    assert item_matches(_item(title="Python Weekly", summary=_BODY), ["python"]) is True
    assert item_matches(_item(summary="python " + _BODY), ["python"]) is True
    assert item_matches(_item(content_text="python " + _BODY), ["python"]) is True


def test_keyword_must_be_present() -> None:
    assert item_matches(_item(summary=_BODY), ["rust"]) is False


def test_matching_is_case_insensitive() -> None:
    assert item_matches(_item(title="Python Weekly", summary=_BODY), ["PYTHON"]) is True
    assert item_matches(_item(title="PYTHON Weekly", summary=_BODY), ["python"]) is True


def test_any_keyword_matches() -> None:
    assert item_matches(_item(title="Rust news", summary=_BODY), ["python", "rust"]) is True
    assert item_matches(_item(title="Go news", summary=_BODY), ["python", "rust"]) is False


def test_title_only_stub_is_rejected() -> None:
    assert (
        item_matches(_item(title="Python release", summary=None, content_text=None), ["python"])
        is False
    )
    assert item_matches(_item(title="Python release", summary="short"), ["python"]) is False
    assert item_matches(_item(title="Python release", content_text="tiny"), ["python"]) is False


def test_body_length_boundary() -> None:
    assert (
        item_matches(
            _item(title="Python", summary="p" * (MIN_BODY_CHARS - 1)), ["python"]
        )
        is False
    )
    assert item_matches(_item(title="Python", summary="p" * MIN_BODY_CHARS), ["python"]) is True


def test_filter_items_returns_kept_and_filtered_count() -> None:
    items = [
        _item(title="Python Weekly", summary=_BODY, url="https://example.com/1"),
        _item(title="Rust Weekly", summary=_BODY, url="https://example.com/2"),
        _item(title="Python stub", url="https://example.com/3"),
    ]
    kept, filtered = filter_items(items, ["python"])

    assert [item.title for item in kept] == ["Python Weekly"]
    assert filtered == 2


# --- source_keywords -------------------------------------------------------------


def test_source_keywords_from_comma_and_space_string() -> None:
    source = _source({KEYWORDS_CONFIG_KEY: "Python, rust  PYTHON ,Go"})
    assert source_keywords(source) == ["python", "rust", "go"]


def test_source_keywords_from_list_dedupes_and_lowercases() -> None:
    source = _source({KEYWORDS_CONFIG_KEY: ["  Rust ", "go", "rust", "", "PY"]})
    assert source_keywords(source) == ["rust", "go", "py"]


def test_source_keywords_caps_at_twenty() -> None:
    words = [f"kw{index}" for index in range(30)]
    assert source_keywords(_source({KEYWORDS_CONFIG_KEY: words})) == words[:20]


@pytest.mark.parametrize("bad", [123, None, True, 3.5, {}, (), ["python", 42]])
def test_source_keywords_tolerates_invalid_values(bad: object) -> None:
    assert source_keywords(_source({KEYWORDS_CONFIG_KEY: bad})) == []


@pytest.mark.parametrize("config", ["not a dict", [1, 2], None, 42])
def test_source_keywords_tolerates_non_dict_config(config: object) -> None:
    assert source_keywords(_source(config)) == []


def test_source_keywords_missing_key_returns_empty() -> None:
    assert source_keywords(_source({"feed_url": _FEED_URL})) == []


# --- pipeline integration --------------------------------------------------------


def _feed() -> bytes:
    xml = (
        "<?xml version='1.0'?><rss version='2.0'><channel>"
        "<title>Example</title>"
        f"<item><title>Python Weekly</title><link>https://example.com/1</link>"
        f"<guid>post-1</guid><description>{_BODY} python</description></item>"
        f"<item><title>Rust Weekly</title><link>https://example.com/2</link>"
        f"<guid>post-2</guid><description>{_BODY}</description></item>"
        "<item><title>Python stub</title><link>https://example.com/3</link>"
        "<guid>post-3</guid></item>"
        "</channel></rss>"
    )
    return xml.encode()


def _run_rss_pipeline(
    session: Session, config: dict[str, Any]
) -> tuple[FetchResult, list[ContentItem]]:
    session.add(_source(config))
    session.flush()

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, content=_feed(), headers={"etag": '"feed-etag"'})

    fetcher = Fetcher(transport=httpx.MockTransport(handler))
    result = asyncio.run(IngestionPipeline(session, fetcher).fetch_source("example"))
    return result, list(session.scalars(select(ContentItem)))


class _StubProvider:
    def __init__(
        self, items: list[NormalizedContentItem], base_skipped: int = 0
    ) -> None:
        self._items = items
        self._base_skipped = base_skipped

    async def collect(
        self, source: Source
    ) -> tuple[list[NormalizedContentItem], int, DiscoverPage]:
        del source
        return self._items, self._base_skipped, DiscoverPage(items=[])


def _run_stub_pipeline(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
    *,
    config: dict[str, Any],
    items: list[NormalizedContentItem],
    base_skipped: int = 0,
) -> tuple[FetchResult, list[ContentItem]]:
    session.add(_source(config))
    session.flush()

    stub = _StubProvider(items, base_skipped)

    def fake_build_provider(
        provider_type: str, fetcher: Fetcher, *, source_id: str
    ) -> _StubProvider:
        del provider_type, fetcher, source_id
        return stub

    monkeypatch.setattr("server.ingestion.pipeline.build_provider", fake_build_provider)

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200)

    fetcher = Fetcher(transport=httpx.MockTransport(handler))
    result = asyncio.run(IngestionPipeline(session, fetcher).fetch_source("example"))
    return result, list(session.scalars(select(ContentItem)))


def test_pipeline_filters_items_and_keeps_them_out_of_the_database(session: Session) -> None:
    result, stored = _run_rss_pipeline(
        session, {"feed_url": _FEED_URL, KEYWORDS_CONFIG_KEY: ["python"]}
    )

    assert result.ok is True
    assert result.discovered == 3
    assert result.skipped == 2
    assert result.inserted == 1
    assert result.updated == 0
    assert {item.title for item in stored} == {"Python Weekly"}
    run = session.scalars(select(FetchRun)).one()
    assert run.items_discovered == 3
    assert run.items_skipped == 2
    assert run.items_created == 1


def test_pipeline_skipped_combines_provider_and_filtered_counts(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    items = [
        _item(title="Python Weekly", summary=_BODY, url="https://example.com/1"),
        _item(title="Rust Weekly", summary=_BODY, url="https://example.com/2"),
        _item(title="Python stub", url="https://example.com/3"),
    ]
    result, stored = _run_stub_pipeline(
        session,
        monkeypatch,
        config={"feed_url": _FEED_URL, KEYWORDS_CONFIG_KEY: ["python"]},
        items=items,
        base_skipped=2,
    )

    assert result.discovered == 3
    assert result.skipped == 4
    assert result.inserted == 1
    assert result.updated == 0
    assert {item.title for item in stored} == {"Python Weekly"}


def test_pipeline_without_keywords_imports_everything(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    items = [
        _item(title="Python Weekly", summary=_BODY, url="https://example.com/1"),
        _item(title="Rust Weekly", summary=_BODY, url="https://example.com/2"),
        _item(title="Python stub", url="https://example.com/3"),
    ]
    result, stored = _run_stub_pipeline(
        session,
        monkeypatch,
        config={"feed_url": _FEED_URL},
        items=items,
        base_skipped=1,
    )

    assert result.discovered == 3
    assert result.skipped == 1
    assert result.inserted == 3
    assert {item.title for item in stored} == {"Python Weekly", "Rust Weekly", "Python stub"}


def test_pipeline_filters_before_applying_the_cap(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    items = [
        _item(title="Python A", summary=_BODY, url="https://example.com/1"),
        _item(title="Rust B", summary=_BODY, url="https://example.com/2"),
        _item(title="Python C", summary=_BODY, url="https://example.com/3"),
        _item(title="Python D", summary=_BODY, url="https://example.com/4"),
    ]
    result, stored = _run_stub_pipeline(
        session,
        monkeypatch,
        config={
            "feed_url": _FEED_URL,
            KEYWORDS_CONFIG_KEY: ["python"],
            "max_items_per_run": 2,
        },
        items=items,
    )

    assert {item.title for item in stored} == {"Python A", "Python C"}
    assert result.inserted == 2
    assert result.skipped == 1
    # Two kept after the cap, plus the one keyword miss: the pre-existing
    # "discovered = capped items" meaning is preserved.
    assert result.discovered == 3


def test_pipeline_discovered_counts_kept_and_filtered_items(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    items = [
        _item(title="Python A", summary=_BODY, url="https://example.com/1"),
        _item(title="Rust B", summary=_BODY, url="https://example.com/2"),
        _item(title="Python C", summary=_BODY, url="https://example.com/3"),
        _item(title="Rust D", summary=_BODY, url="https://example.com/4"),
        _item(title="Python E", summary=_BODY, url="https://example.com/5"),
    ]
    result, stored = _run_stub_pipeline(
        session,
        monkeypatch,
        config={
            "feed_url": _FEED_URL,
            KEYWORDS_CONFIG_KEY: ["python"],
            "max_items_per_run": 2,
        },
        items=items,
    )

    # 2 keyword misses are skipped and 3 matches are capped to 2 imports.
    # discovered keeps its original "capped items" meaning, plus the entries
    # the allowlist examined and rejected.
    assert result.discovered == 4
    assert result.skipped == 2
    assert result.inserted == 2
    assert {item.title for item in stored} == {"Python A", "Python C"}
