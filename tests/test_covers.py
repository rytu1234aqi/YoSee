"""Covers for articles stored before the RSS provider began extracting them."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from server.api import create_app
from server.covers import SKIP_WORDS, clean_image_url, first_image_url
from server.database.base import Base
from server.database.session import create_db_engine, create_session_factory
from server.models import ContentItem, ContentType, Source

_BASE = "https://example.test/blog/post"


def test_first_image_wins_and_relative_urls_are_resolved() -> None:
    body = '<p><img class="x" src="/img/first.png"><img src="second.png"></p>'
    assert first_image_url(body, _BASE) == "https://example.test/img/first.png"


def test_lazy_src_attributes_are_supported() -> None:
    assert first_image_url("<img data-src='/a.png'>", _BASE) == "https://example.test/a.png"
    assert first_image_url("<IMG DATA-ORIGINAL='/b.png'>", _BASE) == "https://example.test/b.png"


def test_markup_in_scripts_and_styles_is_ignored() -> None:
    body = (
        "<style>img{}</style>"
        "<script>var x = '<img src=/evil.png>';</script>"
        "<img src='/real.png'>"
    )
    assert first_image_url(body, _BASE) == "https://example.test/real.png"


@pytest.mark.parametrize("word", SKIP_WORDS)
def test_decorative_urls_are_skipped(word: str) -> None:
    body = f"<img src='/{word}.png'><img src='/real.png'>"
    assert first_image_url(body, _BASE) == "https://example.test/real.png"


@pytest.mark.parametrize(
    "raw",
    ["data:image/png;base64,AAAA", "javascript:alert(1)", "ftp://x/y.png", "", "   "],
)
def test_unsafe_or_empty_urls_are_rejected(raw: str) -> None:
    assert clean_image_url(raw, _BASE) is None


def test_entities_are_decoded() -> None:
    expected = "https://example.test/a.png?x=1&y=2"
    assert clean_image_url("/a.png?x=1&amp;y=2", _BASE) == expected
    assert first_image_url("<img src='/a.png?x=1&amp;y=2'>", _BASE) == expected


def test_absolute_url_without_base_and_malformed_inputs_never_raise() -> None:
    assert clean_image_url("https://cdn.test/a.png", None) == "https://cdn.test/a.png"
    assert clean_image_url("/relative.png", None) is None
    assert clean_image_url(None, _BASE) is None
    assert first_image_url(None) is None
    assert first_image_url("") is None
    assert first_image_url("<img") is None
    assert first_image_url("<img src=''") is None


def _article(
    item_id: str,
    url_hash: str,
    *,
    cover_url: str | None,
    content_html: str | None,
    summary: str | None = None,
) -> ContentItem:
    return ContentItem(
        id=item_id,
        source_id="covers",
        content_type=ContentType.ARTICLE,
        title=item_id,
        canonical_url=f"https://example.test/{item_id}",
        url_hash=url_hash,
        cover_url=cover_url,
        content_html=content_html,
        summary=summary,
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
        fetched_at=datetime.now(UTC),
    )


def test_read_endpoints_derive_a_cover_for_old_rows(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'covers.sqlite'}"
    engine = create_db_engine(url)
    Base.metadata.create_all(engine)
    with create_session_factory(engine)() as session:
        session.add(
            Source(
                id="covers",
                name="Covers",
                slug="covers",
                provider_type="rss",
                fetch_interval_seconds=60,
            )
        )
        session.flush()
        session.add(
            _article(
                "from-body",
                "a" * 64,
                cover_url=None,
                content_html='<p><img src="/img/photo.png"></p>',
            )
        )
        session.add(
            _article(
                "from-summary",
                "b" * 64,
                cover_url=None,
                content_html=None,
                summary='<p><img src="/img/summary.png"></p>',
            )
        )
        session.add(
            _article(
                "explicit",
                "c" * 64,
                cover_url="https://cdn.test/explicit.png",
                content_html=None,
            )
        )
        session.add(_article("no-image", "d" * 64, cover_url=None, content_html=None))
        session.commit()
    engine.dispose()

    client = TestClient(create_app(url))
    feed = {item["id"]: item for item in client.get("/feed").json()}
    assert feed["from-body"]["cover_url"] == "https://example.test/img/photo.png"
    assert feed["from-summary"]["cover_url"] == "https://example.test/img/summary.png"
    assert feed["explicit"]["cover_url"] == "https://cdn.test/explicit.png"
    assert feed["no-image"]["cover_url"] is None

    page = {item["id"]: item for item in client.get("/feed/page").json()["items"]}
    assert page["from-body"]["cover_url"] == "https://example.test/img/photo.png"
    assert page["no-image"]["cover_url"] is None

    detail = client.get("/content/from-body").json()
    assert detail["cover_url"] == "https://example.test/img/photo.png"
    assert client.get("/content/no-image").json()["cover_url"] is None
