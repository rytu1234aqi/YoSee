"""Regression tests for the P0/P1 hardening pass."""

import re
import socket
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from server import web as web_module
from server.api import _source_id, create_app
from server.database.base import Base
from server.database.session import create_db_engine, create_session_factory
from server.ingestion.errors import FetchError
from server.ingestion.fetcher import Fetcher
from server.models import ContentItem, ContentType, FetchRun, FetchStatus, ReadingState, Source
from server.security import UnsafeUrlError, safe_http_url, sanitize_html, validate_public_url
from server.web import create_web_app
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

FEED = (Path(__file__).parent / "fixtures/feeds/sample.rss").read_bytes()


def _client(
    tmp_path: Path, *, auth_token: str | None = None
) -> tuple[TestClient, sessionmaker[Session]]:
    url = f"sqlite:///{tmp_path / 'hardening.sqlite'}"
    engine = create_db_engine(url)
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    client = TestClient(create_app(url, auth_token=auth_token))
    return client, factory


def _source(source_id: str = "custom-target", **overrides: object) -> Source:
    values: dict[str, object] = dict(
        id=source_id,
        name="Target",
        slug=source_id,
        provider_type="rss",
        config_json={"feed_url": "https://example.test/feed"},
        fetch_interval_seconds=60,
    )
    values.update(overrides)
    return Source(**values)


def _backup(article_id: str = "a1") -> dict[str, object]:
    return {
        "sources": [
            {
                "id": "saved-source",
                "name": "Saved",
                "slug": "saved-source",
                "provider_type": "import",
                "enabled": True,
                "config_json": {},
                "fetch_interval_seconds": 86400,
            }
        ],
        "articles": [
            {
                "id": article_id,
                "source_id": "saved-source",
                "title": "First",
                "canonical_url": "https://example.test/first",
                "content_html": (
                    '<p onclick="steal()">Hi<script>alert(1)</script>'
                    '<a href="javascript:alert(1)">bad</a></p>'
                ),
                "content_type": "ARTICLE",
                "fetched_at": "2026-01-01T00:00:00Z",
            }
        ],
        "reading_states": [{"content_id": article_id, "is_read": True, "saved": True}],
    }


def test_import_deduplicates_by_url_and_remaps_reading_state(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    first = client.post("/import/content", json=_backup())
    assert first.status_code == 200
    assert first.json()["imported"] == 1

    article = client.get("/content/a1").json()
    assert article["is_read"] is True and article["saved"] is True
    assert "<script" not in (article["content_html"] or "")
    assert "onclick" not in (article["content_html"] or "")
    assert "javascript:" not in (article["content_html"] or "")
    # A restored backup must never restart automatic fetching.
    assert client.get("/sources").json()[0]["enabled"] is False

    second = client.post("/import/content", json=_backup("a2"))
    assert second.json()["imported"] == 0
    assert second.json()["skipped"] == 1
    assert client.get("/content/a1").json()["saved"] is True
    assert client.get("/content/a2").status_code == 404


def test_delete_source_removes_articles_runs_and_states(tmp_path: Path) -> None:
    client, factory = _client(tmp_path)
    with factory() as session:
        session.add(_source())
        session.flush()
        session.add(
            ContentItem(
                id="child",
                source_id="custom-target",
                content_type=ContentType.ARTICLE,
                title="Child",
                canonical_url="https://example.test/child",
                url_hash="b" * 64,
                fetched_at=datetime.now(UTC),
            )
        )
        session.flush()  # make the parent row visible before adding dependent states
        session.add(
            FetchRun(
                source_id="custom-target",
                started_at=datetime.now(UTC),
                status=FetchStatus.SUCCESS,
            )
        )
        session.add(ReadingState(content_id="child", is_read=True))
        session.add(_source("built-in", slug="built-in"))
        session.commit()
    assert client.delete("/sources/custom-target").status_code == 204
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Source)) == 1
        assert session.scalar(select(func.count()).select_from(ContentItem)) == 0
        assert session.scalar(select(func.count()).select_from(FetchRun)) == 0
        assert session.scalar(select(func.count()).select_from(ReadingState)) == 0
    assert client.delete("/sources/built-in").status_code == 409


@pytest.mark.parametrize(
    "bad",
    [
        "http://127.0.0.1:8765/backup",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.1/feed",
        "http://localhost/feed",
        "http://[::1]/feed",
        "https://user:pass@example.com/feed",
        "file:///tmp/feed",
        "ftp://example.com/feed",
    ],
)
def test_source_urls_reject_private_or_non_http_targets(tmp_path: Path, bad: str) -> None:
    client, _ = _client(tmp_path)
    response = client.post("/sources", json={"name": "Bad", "feed_url": bad})
    assert response.status_code == 422
    with pytest.raises(UnsafeUrlError):
        validate_public_url(bad)


def test_public_urls_still_accepted(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    response = client.post(
        "/sources", json={"name": "Good", "feed_url": "https://example.com/feed.xml"}
    )
    assert response.status_code == 201
    assert validate_public_url("https://example.com/feed.xml").startswith("https://")


def test_local_token_and_host_guard(tmp_path: Path) -> None:
    client, _ = _client(tmp_path, auth_token="secret-token")
    local = {"Host": "127.0.0.1:8765"}
    assert client.get("/sources", headers=local).status_code == 401
    assert client.get("/sources").status_code == 400  # rebound/testserver host
    assert client.get("/health", headers=local).status_code == 200
    assert (
        client.get(
            "/sources", headers={**local, "X-Local-Token": "secret-token"}
        ).status_code
        == 200
    )


def test_web_shell_injects_the_token(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'shell.sqlite'}"
    engine = create_db_engine(url)
    Base.metadata.create_all(engine)
    engine.dispose()
    client = TestClient(create_web_app(url, auth_token="fixed-token"))
    local = {"Host": "127.0.0.1:8765"}
    page = client.get("/", headers=local)
    assert page.status_code == 200
    assert 'name="local-token" content="fixed-token"' in page.text
    assert client.get("/sources", headers=local).status_code == 401
    assert (
        client.get("/sources", headers={**local, "X-Local-Token": "fixed-token"}).status_code
        == 200
    )


def test_web_shell_disables_caching(tmp_path: Path) -> None:
    """Embedded WebViews must not keep a stale CSS/JS after a backend change."""

    url = f"sqlite:///{tmp_path / 'no-store.sqlite'}"
    engine = create_db_engine(url)
    Base.metadata.create_all(engine)
    engine.dispose()
    client = TestClient(create_web_app(url, auth_token="fixed-token"))
    local = {"Host": "127.0.0.1:8765"}
    assert client.get("/", headers=local).headers["cache-control"] == "no-store"
    assert client.get("/static/app.css", headers=local).headers["cache-control"] == "no-store"
    assert client.get("/static/app.js", headers=local).headers["cache-control"] == "no-store"


def test_index_versions_static_assets(tmp_path: Path) -> None:
    """The shell must reference fresh URLs, not the ones a WebView cached earlier."""

    url = f"sqlite:///{tmp_path / 'asset-version.sqlite'}"
    engine = create_db_engine(url)
    Base.metadata.create_all(engine)
    engine.dispose()
    client = TestClient(create_web_app(url, auth_token="fixed-token"))
    local = {"Host": "127.0.0.1:8765"}
    html = client.get("/", headers=local).text
    match = re.search(r"/static/app\.css\?v=([0-9a-f]{12})", html)
    assert match, html[:400]
    version = match.group(1)
    assert f'src="/static/app.js?v={version}"' in html
    assert "__ASSET_VERSION__" not in html
    assert client.get(f"/static/app.css?v={version}", headers=local).status_code == 200


def test_asset_version_changes_with_file_contents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(web_module, "WEB_ROOT", tmp_path)
    (tmp_path / "app.css").write_text("body { color: red }", encoding="utf-8")
    before = web_module.asset_version()
    (tmp_path / "app.css").write_text("body { color: blue }", encoding="utf-8")
    assert web_module.asset_version() != before


def test_sanitizer_drops_whole_dangerous_subtrees() -> None:
    result = sanitize_html(
        '<div>keep<script>alert(1)</script><svg><script>evil()</script></svg>'
        '<img src="javascript:alert(1)" onerror="evil()"><a href="https://example.com/x">ok</a></div>'
    )
    assert result is not None
    assert "keep" in result
    assert "alert(1)" not in result
    assert "onerror" not in result
    assert 'href="https://example.com/x"' in result
    assert safe_http_url("javascript:alert(1)") == ""
    assert safe_http_url("data:text/html,x") == ""
    assert safe_http_url("https://example.com/x").startswith("https://")
    assert safe_http_url("//evil.test/x") == ""
    assert safe_http_url("/guide") == "/guide"
    assert '<a href="/guide">local</a>' in (
        sanitize_html('<p><a href="/guide">local</a></p>') or ""
    )


def test_source_ids_are_stable_across_processes() -> None:
    first = _source_id("My Blog", "https://example.com/feed.xml")
    assert first == _source_id("My Blog", "https://example.com/feed.xml")
    assert first.startswith("custom-my-blog-")
    assert _source_id("My Blog", "https://example.com/other") != first


def test_import_rejects_invalid_structure(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    payload = {
        "sources": [{"id": "s", "fetch_interval_seconds": "not-a-number"}],
        "articles": [{"id": "a", "title": "T", "canonical_url": "https://example.test/a"}],
    }
    assert client.post("/import/content", json=payload).status_code == 422


def test_proxy_environment_does_not_reject_public_hosts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local DNS answers behind a proxy are routing artifacts, not targets."""

    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:3128")
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.1.11", 443))
        ],
    )
    fetcher = Fetcher()
    assert fetcher._resolve_dns is False
    fetcher._assert_public("https://www.redblobgames.com/feed.xml")
    for blocked in (
        "http://127.0.0.1:8765/backup",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.1.2.3/feed",
    ):
        with pytest.raises(FetchError):
            fetcher._assert_public(blocked)


def test_direct_connections_still_reject_rebound_hosts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
        ],
    )
    fetcher = Fetcher()
    assert fetcher._resolve_dns is True
    with pytest.raises(FetchError, match="non-public"):
        fetcher._assert_public("https://rebind.example/feed")


def test_manual_fetch_runs_on_the_scheduler_loop(tmp_path: Path) -> None:
    """The API fetch must share the poller's lock, not open a second loop."""

    url = f"sqlite:///{tmp_path / 'concurrent.sqlite'}"
    engine = create_db_engine(url)
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    with factory() as session:
        session.add(_source(last_fetched_at=datetime.now(UTC)))
        session.commit()
    engine.dispose()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=FEED)

    app = create_app(
        url,
        auto_fetch=True,
        poll_seconds=3600,
        fetcher_factory=lambda: Fetcher(transport=httpx.MockTransport(handler)),
    )
    with TestClient(app) as client:
        response = client.post("/sources/custom-target/fetch")
        assert response.status_code == 200
        assert response.json()["ok"] is True
        status = client.get("/sync/status").json()
        assert status["enabled"] is True
        assert status["revision"] >= 1  # manual fetches also wake up other tabs
