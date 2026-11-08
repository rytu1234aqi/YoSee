"""The "scroll like a short-video feed" features: quiet sources, keyword
filters, saved-article tags, link-only export and old-link cleanup status.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from server.api import (
    SourceCreate,
    _clean_keywords,
    create_app,
)
from server.database.base import Base
from server.database.session import create_db_engine, create_session_factory
from server.models import ContentItem, ContentType, ReadingState, Source
from sqlalchemy.orm import Session, sessionmaker

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)


def _seed(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        quiet = Source(
            id="quiet",
            name="Quiet blog",
            slug="quiet",
            provider_type="rss",
            fetch_interval_seconds=3600,
            config_json={
                "feed_url": "https://example.test/quiet.xml",
                "keywords": ["Shader", "shader"],
            },
            last_success_at=NOW - timedelta(days=60),
        )
        monthly = Source(
            id="monthly",
            name="Monthly digest",
            slug="monthly",
            provider_type="rss",
            fetch_interval_seconds=30 * 86400,
            config_json={},
            last_success_at=NOW - timedelta(days=10),
        )
        session.add_all([quiet, monthly])
        session.flush()
        for index, (item_id, title) in enumerate(
            [("a", "Shader tricks"), ("b", "A short post"), ("c", "Rendering notes")]
        ):
            session.add(
                ContentItem(
                    id=item_id,
                    source_id="quiet",
                    external_id=item_id,
                    content_type=ContentType.ARTICLE,
                    title=title,
                    summary="A summary",
                    canonical_url=f"https://example.test/{item_id}",
                    url_hash=f"{item_id}" * 64,
                    published_at=NOW - timedelta(days=index),
                    fetched_at=NOW,
                    content_text="A saved body long enough to read offline.",
                )
            )
        session.flush()
        session.add(
            ReadingState(
                content_id="a", is_read=True, saved=True, tag="like", updated_at=NOW
            )
        )
        session.add(ReadingState(content_id="c", is_read=False, saved=False, updated_at=NOW))
        session.commit()


@pytest.fixture
def env(tmp_path: Path) -> Iterator[tuple[TestClient, sessionmaker[Session], str]]:
    url = f"sqlite:///{tmp_path / 'stream.sqlite'}"
    engine = create_db_engine(url)
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    _seed(factory)
    with TestClient(create_app(url, auto_cleanup=False)) as client:
        yield client, factory, url
    engine.dispose()


def test_clean_keywords_dedupes_lowercases_and_caps() -> None:
    assert _clean_keywords([" Shader ", "shader", "Rendering", ""]) == [
        "shader",
        "rendering",
    ]
    assert _clean_keywords(["x"] * 30) == ["x"]


def test_source_create_normalizes_keywords() -> None:
    payload = SourceCreate(
        name="A blog",
        feed_url="https://example.test/feed.xml",
        keywords=[" Shader ", "shader", "Rendering"],
    )
    assert payload.keywords == ["shader", "rendering"]


def test_sources_report_keywords_and_quiet_flag(env) -> None:
    client, _, _ = env
    rows = {row["id"]: row for row in client.get("/sources").json()}
    assert rows["quiet"]["keywords"] == ["shader"]
    assert rows["quiet"]["stale"] is True
    assert rows["quiet"]["days_since_update"] >= 55
    # A monthly digest is not stale after ten days: its own interval is longer.
    assert rows["monthly"]["stale"] is False
    assert rows["monthly"]["keywords"] == []


def test_patch_source_keywords_are_persisted_and_normalized(env) -> None:
    client, factory, _ = env
    result = client.patch(
        "/sources/quiet", json={"keywords": [" Rendering ", "rendering", "Vulkan"]}
    )
    assert result.status_code == 200
    assert result.json()["keywords"] == ["rendering", "vulkan"]
    with factory() as session:
        row = session.get(Source, "quiet")
        assert row is not None
        assert row.config_json["keywords"] == ["rendering", "vulkan"]
        assert row.config_json["feed_url"] == "https://example.test/quiet.xml"


def test_saved_tag_round_trip_through_state_and_article(env) -> None:
    client, _, _ = env
    assert client.get("/content/a").json()["tag"] == "like"
    assert client.put("/content/b/state", json={"tag": "later"}).json() == {
        "is_read": False,
        "saved": True,
    }
    article = client.get("/content/b").json()
    assert article["tag"] == "later"
    assert article["saved"] is True

    client.put("/content/b/state", json={"tag": "none"})
    assert client.get("/content/b").json()["tag"] is None
    assert client.get("/content/b").json()["saved"] is True

    client.put("/content/b/state", json={"tag": "like"})
    client.put("/content/b/state", json={"saved": False})
    cleared = client.get("/content/b").json()
    assert cleared["saved"] is False
    assert cleared["tag"] is None


def test_invalid_tag_is_rejected(env) -> None:
    client, _, _ = env
    assert client.put("/content/c/state", json={"tag": "whatever"}).status_code == 422


def test_feed_and_feed_page_expose_the_tag(env) -> None:
    client, _, _ = env
    first = client.get("/feed", params={"limit": 10}).json()
    assert next(item for item in first if item["id"] == "a")["tag"] == "like"
    page = client.get("/feed/page", params={"limit": 10}).json()
    assert next(item for item in page["items"] if item["id"] == "a")["tag"] == "like"


def test_saved_link_export_has_no_article_bodies(env) -> None:
    client, _, _ = env
    links = client.get("/export/saved-links").json()
    assert len(links) == 1
    assert links[0]["url"] == "https://example.test/a"
    assert links[0]["tag"] == "like"
    assert links[0]["source_name"] == "Quiet blog"
    assert "content_html" not in links[0]
    assert "content_text" not in links[0]


def test_full_backup_round_trips_the_tag(tmp_path: Path) -> None:
    source_url = f"sqlite:///{tmp_path / 'source.sqlite'}"
    engine = create_db_engine(source_url)
    Base.metadata.create_all(engine)
    _seed(create_session_factory(engine))
    engine.dispose()
    with TestClient(create_app(source_url)) as origin:
        backup = origin.get("/backup").json()
    assert any(state["tag"] == "like" for state in backup["reading_states"])

    target_url = f"sqlite:///{tmp_path / 'target.sqlite'}"
    target = create_db_engine(target_url)
    Base.metadata.create_all(target)
    target.dispose()
    with TestClient(create_app(target_url)) as restored:
        assert restored.post("/import/content", json=backup).json()["imported"] == 3
        assert restored.get("/content/a").json()["tag"] == "like"


def test_cleanup_status_and_manual_run_are_available(env) -> None:
    client, _, _ = env
    status = client.get("/cleanup/status").json()
    assert status["enabled"] is False
    assert status["keep_recent"] == 200
    assert client.post("/cleanup/run").json()["ok"] is False


def test_cleanup_enabled_app_can_report_and_run(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'cleanup.sqlite'}"
    engine = create_db_engine(url)
    Base.metadata.create_all(engine)
    _seed(create_session_factory(engine))
    engine.dispose()
    with TestClient(create_app(url, auto_cleanup=True, cleanup_keep_recent=50)) as client:
        assert client.get("/cleanup/status").json()["enabled"] is True
        assert client.get("/cleanup/status").json()["keep_recent"] == 50
        result = client.post("/cleanup/run").json()
        assert result["ok"] is True
        # Seeded items are too recent and mostly unread, so nothing is removed.
        assert result["deleted"] == 0
        assert client.get("/feed").json()
