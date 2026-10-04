from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from server.api import create_app
from server.database.base import Base
from server.database.session import create_db_engine, create_session_factory
from server.models import ContentItem, ContentType, Source
from sqlalchemy.orm import Session, sessionmaker


def test_feed_and_sources(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'api.sqlite'}"
    engine = create_db_engine(url)
    Base.metadata.create_all(engine)
    with create_session_factory(engine)() as session:
        session.add(
            Source(
                id="godot",
                name="Godot",
                slug="godot",
                provider_type="rss",
                fetch_interval_seconds=60,
            )
        )
        session.flush()
        session.add(
            ContentItem(
                id="item-1",
                source_id="godot",
                content_type=ContentType.ARTICLE,
                title="Godot rendering",
                summary="Rendering updates",
                canonical_url="https://example.test/1",
                url_hash="a" * 64,
                published_at=datetime(2026, 1, 1, tzinfo=UTC),
                fetched_at=datetime.now(UTC),
            )
        )
        session.commit()
    engine.dispose()

    client = TestClient(create_app(url))
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/sources").json()[0]["id"] == "godot"
    assert client.get("/feed", params={"q": "rendering"}).json()[0]["id"] == "item-1"
    assert client.get("/content/item-1").json()["title"] == "Godot rendering"
    assert client.put("/content/item-1/state", json={"saved": True}).json() == {
        "is_read": False,
        "saved": True,
    }
    article = client.get("/content/item-1").json()
    assert article["saved"] is True
    assert client.get("/feed", params={"status": "saved"}).json()[0]["id"] == "item-1"
    assert client.get("/feed", params={"status": "unread"}).json()[0]["id"] == "item-1"
    assert client.put("/content/item-1/state", json={"is_read": True}).json() == {
        "is_read": True,
        "saved": True,
    }
    assert client.get("/feed", params={"status": "unread"}).json() == []
    assert client.get("/feed", params={"offset": 1}).json() == []
    export = client.get("/export/content").json()
    assert export[0]["title"] == "Godot rendering"
    assert export[0]["is_read"] is True


def test_full_backup_can_be_imported(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'backup-import.sqlite'}"
    engine = create_db_engine(url)
    Base.metadata.create_all(engine)
    client = TestClient(create_app(url))
    backup = {
        "sources": [{
            "id": "saved-source", "name": "Saved Source", "slug": "saved-source",
            "provider_type": "import", "enabled": False, "config_json": {},
            "fetch_interval_seconds": 86400,
        }],
        "articles": [{
            "id": "saved-item", "source_id": "saved-source", "title": "Saved article",
            "canonical_url": "https://example.test/saved", "content_text": "Body",
            "content_type": "ARTICLE", "fetched_at": "2026-01-01T00:00:00Z",
        }],
        "reading_states": [{"content_id": "saved-item", "is_read": True, "saved": True}],
    }
    result = client.post("/import/content", json=backup)
    assert result.status_code == 200
    assert result.json()["imported"] == 1
    article = client.get("/content/saved-item").json()
    assert article["is_read"] is True
    assert article["saved"] is True
    engine.dispose()


@pytest.fixture
def paged_api(tmp_path: Path) -> Iterator[tuple[TestClient, sessionmaker[Session]]]:
    url = f"sqlite:///{tmp_path / 'pages.sqlite'}"
    engine = create_db_engine(url)
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    with factory() as session:
        session.add(
            Source(
                id="paging",
                name="Paging",
                slug="paging",
                provider_type="rss",
                fetch_interval_seconds=60,
            )
        )
        session.flush()
        for index in range(55):
            session.add(_page_item(index))
        session.commit()
    with TestClient(create_app(url)) as client:
        yield client, factory
    engine.dispose()


def _page_item(index: int) -> ContentItem:
    return ContentItem(
        id=f"item-{index:03}",
        source_id="paging",
        content_type=ContentType.ARTICLE,
        title=f"Article {index}" if index else "100% literal_under_score",
        canonical_url=f"https://example.test/{index}",
        url_hash=f"{index:064x}",
        published_at=datetime(2026, 1, 1) + timedelta(minutes=index // 5) if index >= 5 else None,
        fetched_at=datetime.now(UTC),
    )


def test_cursor_pagination_survives_new_arrivals_and_includes_undated(
    paged_api: tuple[TestClient, sessionmaker[Session]],
) -> None:
    client, factory = paged_api
    first = client.get("/feed/page").json()
    assert [item["id"] for item in first["items"]] == [f"item-{i:03}" for i in range(54, 4, -1)]
    with factory() as session:
        session.add(_page_item(999))
        session.commit()
    # Removing an item from a filtered list must not shift the following page either.
    client.put("/content/item-054/state", json={"is_read": True})
    second = client.get("/feed/page", params={"cursor": first["next_cursor"]}).json()
    assert [item["id"] for item in second["items"]] == [f"item-{i:03}" for i in range(4, -1, -1)]
    assert second["next_cursor"] is None
    assert client.get("/feed/page").json()["items"][0]["id"] == "item-999"


def test_cursor_walk_handles_tied_dates_and_null_date_cursor(
    paged_api: tuple[TestClient, sessionmaker[Session]],
) -> None:
    client, _ = paged_api
    ids: list[str] = []
    params: dict[str, object] = {"limit": 3}
    for _ in range(20):
        response = client.get("/feed/page", params=params)
        assert response.status_code == 200
        page = response.json()
        ids.extend(item["id"] for item in page["items"])
        if page["next_cursor"] is None:
            break
        params["cursor"] = page["next_cursor"]
    assert ids == [f"item-{i:03}" for i in range(54, -1, -1)]


def test_filters_and_literal_search_work_on_paginated_feed(
    paged_api: tuple[TestClient, sessionmaker[Session]],
) -> None:
    client, _ = paged_api
    client.put("/content/item-000/state", json={"saved": True})
    page = client.get(
        "/feed/page",
        params={
            "source_id": "paging",
            "status": "saved",
            "q": "%",
        },
    ).json()
    assert [item["id"] for item in page["items"]] == ["item-000"]
    assert page["next_cursor"] is None
    assert client.get("/feed/page", params={"source_id": "missing"}).json()["items"] == []
    assert len(client.get("/feed", params={"q": "_"}).json()) == 1


@pytest.mark.parametrize(
    "params",
    [
        {"cursor": "!not-base64!"},
        {"cursor": "e30="},
        {"limit": 0},
        {"limit": 201},
        {"status": "unknown"},
    ],
)
def test_invalid_page_parameters_are_rejected(
    paged_api: tuple[TestClient, sessionmaker[Session]],
    params: dict[str, object],
) -> None:
    client, _ = paged_api
    assert client.get("/feed/page", params=params).status_code == 422
