"""Saving a link-only article queues a one-off full-text fetch."""

import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from server.api import create_app
from server.body_fetch import BodyFetchManager, extract_body
from server.database.base import Base
from server.database.session import create_db_engine, create_session_factory
from server.ingestion.fetcher import Fetcher
from server.models import ContentItem, ContentType, ReadingState, Source

_ARTICLE_HTML = (
    "<html><head><script>evil()</script></head><body>"
    "<nav>Navigation noise</nav>"
    "<article><h1>A real article</h1>"
    "<p>This paragraph contains enough real prose for the extractor to accept it as a body.</p>"
    '<img src="/images/diagram.png"><a href="/more">More</a>'
    "</article><footer>Footer noise</footer></body></html>"
)
_URL = "https://example.test/posts/1"


def _transport(html: str = _ARTICLE_HTML, status: int = 200) -> httpx.MockTransport:
    return httpx.MockTransport(lambda request: httpx.Response(status, text=html))


def _fetcher_factory(transport: httpx.MockTransport):
    return lambda **kwargs: Fetcher(transport=transport, **kwargs)


def _seed(path: Path, *, saved: bool = True) -> object:
    engine = create_db_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    now = datetime.now(UTC)
    with factory() as session:
        session.add(
            Source(
                id="body-source",
                name="Body source",
                slug="body-source",
                provider_type="rss",
                fetch_interval_seconds=60,
            )
        )
        session.flush()
        session.add(
            ContentItem(
                id="item-0",
                source_id="body-source",
                content_type=ContentType.ARTICLE,
                title="Item 0",
                canonical_url=_URL,
                url_hash="a" * 64,
                published_at=now,
                fetched_at=now,
            )
        )
        if saved:
            session.add(
                ReadingState(content_id="item-0", saved=True, saved_at=now, updated_at=now)
            )
        session.commit()
    engine.dispose()
    return factory


def test_extract_body_keeps_article_prose_and_absolutizes_links() -> None:
    html, text = extract_body(_ARTICLE_HTML, _URL)
    assert html and text
    assert "This paragraph contains enough real prose" in text
    assert "Navigation noise" not in text
    assert "evil()" not in text
    assert "https://example.test/images/diagram.png" in html
    assert 'href="https://example.test/more"' in html
    assert "<script" not in html


def test_extract_body_falls_back_to_a_role_main_region() -> None:
    markup = (
        "<html><body><nav>Navigation noise</nav>"
        '<div role="main"><h1>A real article</h1>'
        "<p>This paragraph contains enough real prose for the extractor to accept it as a body.</p>"
        "</div><footer>Footer noise</footer></body></html>"
    )
    html, text = extract_body(markup)
    assert html and text
    assert "This paragraph contains enough real prose" in text
    assert "Navigation noise" not in text


def test_extract_body_rejects_pages_without_real_prose() -> None:
    assert extract_body("<html><body><p>Comments</p></body></html>") == (None, None)
    assert extract_body("") == (None, None)


def test_process_stores_a_sanitized_body_for_a_saved_link(tmp_path: Path) -> None:
    factory = _seed(tmp_path / "body.sqlite")
    manager = BodyFetchManager(factory, fetcher_factory=_fetcher_factory(_transport()))
    manager._process("item-0")
    with factory() as session:
        item = session.get(ContentItem, "item-0")
        assert item is not None
        assert item.content_html and "This paragraph" in item.content_html
        assert item.content_text and "enough real prose" in item.content_text
    assert manager.status()["completed"] == 1


def test_process_skips_unsaved_items(tmp_path: Path) -> None:
    factory = _seed(tmp_path / "unsaved.sqlite", saved=False)
    manager = BodyFetchManager(factory, fetcher_factory=_fetcher_factory(_transport()))
    manager._process("item-0")
    with factory() as session:
        item = session.get(ContentItem, "item-0")
        assert item is not None and item.content_html is None
    assert manager.status()["completed"] == 0


def test_a_failed_fetch_is_recorded_but_the_link_survives(tmp_path: Path) -> None:
    factory = _seed(tmp_path / "failed.sqlite")
    manager = BodyFetchManager(
        factory, fetcher_factory=_fetcher_factory(_transport(status=500))
    )
    manager._process("item-0")
    with factory() as session:
        item = session.get(ContentItem, "item-0")
        assert item is not None and item.content_html is None
    status = manager.status()
    assert status["failed"] == 1
    assert status["last_error"]


def test_saving_through_the_api_triggers_a_background_fetch(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'api.sqlite'}"
    _seed(tmp_path / "api.sqlite", saved=False)
    client = TestClient(
        create_app(url, auto_body_fetch=True, fetcher_factory=_fetcher_factory(_transport()))
    )
    with client:
        assert client.get("/body-fetch/status").json()["enabled"] is True
        assert client.put("/content/item-0/state", json={"saved": True}).json()["saved"] is True
        for _ in range(100):
            if client.get("/content/item-0").json()["has_body"]:
                break
            time.sleep(0.05)
        detail = client.get("/content/item-0").json()
        assert detail["has_body"] is True
        assert "This paragraph" in (detail["content_html"] or "")
        assert client.get("/body-fetch/status").json()["completed"] >= 1
        run = client.post("/body-fetch/run").json()
        assert run["ok"] is True
        assert run["queued"] == 0


def test_manual_run_reports_when_the_feature_is_disabled(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'disabled.sqlite'}"
    _seed(tmp_path / "disabled.sqlite", saved=False)
    client = TestClient(create_app(url, auto_body_fetch=False))
    with client:
        assert client.get("/body-fetch/status").json()["enabled"] is False
        run = client.post("/body-fetch/run").json()
        assert run["ok"] is False
        assert "disabled" in (run["error"] or "")
