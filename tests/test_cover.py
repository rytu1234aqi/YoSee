"""The cover URL is exposed by every article-list and detail endpoint."""

from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient
from server.api import create_app
from server.database.base import Base
from server.database.session import create_db_engine, create_session_factory
from server.models import ContentItem, ContentType, Source

_COVER = "https://example.test/cover.png"


def _article(item_id: str, url_hash: str, cover_url: str | None) -> ContentItem:
    return ContentItem(
        id=item_id,
        source_id="covered",
        content_type=ContentType.ARTICLE,
        title=item_id,
        canonical_url=f"https://example.test/{item_id}",
        url_hash=url_hash,
        cover_url=cover_url,
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
        fetched_at=datetime.now(UTC),
    )


def test_cover_url_is_returned_by_feed_page_and_content(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'cover.sqlite'}"
    engine = create_db_engine(url)
    Base.metadata.create_all(engine)
    with create_session_factory(engine)() as session:
        session.add(
            Source(
                id="covered",
                name="Covered",
                slug="covered",
                provider_type="rss",
                fetch_interval_seconds=60,
            )
        )
        session.flush()
        session.add(_article("item-with-cover", "a" * 64, _COVER))
        session.add(_article("item-no-cover", "b" * 64, None))
        session.commit()
    engine.dispose()

    client = TestClient(create_app(url))
    feed = {item["id"]: item for item in client.get("/feed").json()}
    assert feed["item-with-cover"]["cover_url"] == _COVER
    assert feed["item-no-cover"]["cover_url"] is None

    page = client.get("/feed/page").json()
    paged = {item["id"]: item for item in page["items"]}
    assert paged["item-with-cover"]["cover_url"] == _COVER
    assert paged["item-no-cover"]["cover_url"] is None

    assert client.get("/content/item-with-cover").json()["cover_url"] == _COVER
    assert client.get("/content/item-no-cover").json()["cover_url"] is None
