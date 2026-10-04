from fastapi.testclient import TestClient
from server.api import create_app
from server.database.base import Base
from server.database.session import create_db_engine


def test_create_custom_rss_source(tmp_path):
    url = f"sqlite:///{tmp_path / 'custom.sqlite'}"
    engine = create_db_engine(url)
    Base.metadata.create_all(engine)
    client = TestClient(create_app(url))
    response = client.post(
        "/sources",
        json={
            "name": "My Blog",
            "feed_url": "https://example.com/feed.xml",
            "fetch_interval_seconds": 600,
        },
    )
    assert response.status_code == 201
    source = response.json()
    assert source["name"] == "My Blog"
    assert client.get("/sources").json()[0]["id"] == source["id"]
    with engine.connect() as connection:
        assert connection.exec_driver_sql("select provider_type from sources").scalar_one() == "rss"
    engine.dispose()


def test_custom_source_rejects_non_http_url(tmp_path):
    url = f"sqlite:///{tmp_path / 'custom-invalid.sqlite'}"
    engine = create_db_engine(url)
    Base.metadata.create_all(engine)
    client = TestClient(create_app(url))
    assert (
        client.post(
            "/sources", json={"name": "Bad", "feed_url": "file:///tmp/feed"}
        ).status_code
        == 422
    )
    engine.dispose()


def test_discover_feed_from_website(monkeypatch, tmp_path):
    from server.ingestion.fetcher import FetchResponse

    class FakeFetcher:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url):
            return FetchResponse(
                url=url,
                status_code=200,
                headers={},
                body=b'<link rel="alternate" type="application/rss+xml" href="/feed.xml">',
                text="",
                etag=None,
                last_modified=None,
                not_modified=False,
            )

    url = f"sqlite:///{tmp_path / 'discover.sqlite'}"
    engine = create_db_engine(url)
    Base.metadata.create_all(engine)
    client = TestClient(create_app(url, fetcher_factory=FakeFetcher))
    response = client.post("/sources/discover", json={"url": "https://example.com/blog"})
    assert response.status_code == 200
    assert response.json()["feed_url"] == "https://example.com/feed.xml"
    engine.dispose()


def test_create_html_source_and_report_selector_failure(tmp_path):
    from server.ingestion.fetcher import FetchResponse

    class FakeFetcher:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, **kwargs):
            return FetchResponse(
                url=url,
                status_code=200,
                headers={},
                body=b"<html><article><h2><a href='/one'>First article</a></h2></article></html>",
                text="",
                etag=None,
                last_modified=None,
                not_modified=False,
            )

    url = f"sqlite:///{tmp_path / 'custom-html.sqlite'}"
    engine = create_db_engine(url)
    Base.metadata.create_all(engine)
    client = TestClient(create_app(url, fetcher_factory=FakeFetcher))
    response = client.post(
        "/sources/html",
        json={
            "name": "Custom HTML",
            "url": "https://example.com/blog",
            "item_selector": "article",
            "title_selector": "h2 a",
            "link_selector": "h2 a",
            "fetch_interval_seconds": 3600,
        },
    )
    assert response.status_code == 201
    result = response.json()
    assert result["source"]["name"] == "Custom HTML"
    assert result["ok"] is True
    assert result["inserted"] == 1
    article = client.get("/feed", params={"source_id": result["id"]}).json()[0]
    assert article["title"] == "First article"
    engine.dispose()


def test_source_fetch_history_endpoint(tmp_path):
    url = f"sqlite:///{tmp_path / 'runs.sqlite'}"
    engine = create_db_engine(url)
    Base.metadata.create_all(engine)
    from datetime import UTC, datetime

    from server.database.session import create_session_factory
    from server.models import FetchRun, FetchStatus, Source

    factory = create_session_factory(engine)
    with factory() as session:
        session.add(
            Source(
                id="custom-run",
                name="Run source",
                slug="custom-run",
                provider_type="rss",
                fetch_interval_seconds=600,
                config_json={"feed_url": "https://example.com/feed.xml"},
            )
        )
        session.add(
            FetchRun(
                source_id="custom-run",
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
                status=FetchStatus.SUCCESS,
                items_discovered=3,
                items_created=2,
                items_updated=1,
            )
        )
        session.commit()
    client = TestClient(create_app(url))
    response = client.get("/sources/custom-run/runs")
    assert response.status_code == 200
    assert response.json()[0]["items_created"] == 2
    engine.dispose()
