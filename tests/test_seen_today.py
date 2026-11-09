"""The "seen today" view reads straight from reading_states.read_at."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from server.api import create_app
from server.database.base import Base
from server.database.session import create_db_engine, create_session_factory
from server.models import ContentItem, ContentType, ReadingState, Source


def _seed(path: Path, reads: list[datetime | None]) -> None:
    engine = create_db_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    now = datetime.now(UTC)
    with create_session_factory(engine)() as session:
        session.add(
            Source(
                id="today",
                name="Today",
                slug="today",
                provider_type="rss",
                fetch_interval_seconds=60,
            )
        )
        session.flush()
        for index, read_at in enumerate(reads):
            session.add(
                ContentItem(
                    id=f"item-{index}",
                    source_id="today",
                    content_type=ContentType.ARTICLE,
                    title=f"Item {index}",
                    canonical_url=f"https://example.test/{index}",
                    url_hash=f"{index}" * 64,
                    published_at=now,
                    fetched_at=now,
                )
            )
            if read_at is not None:
                session.add(
                    ReadingState(
                        content_id=f"item-{index}",
                        is_read=True,
                        read_at=read_at,
                        updated_at=now,
                    )
                )
        session.commit()
    engine.dispose()


def test_seen_today_returns_only_todays_reads_most_recent_first(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    _seed(
        tmp_path / "today.sqlite",
        [now - timedelta(hours=2), now - timedelta(minutes=1), now - timedelta(days=3), None],
    )
    items = TestClient(create_app(f"sqlite:///{tmp_path / 'today.sqlite'}")).get(
        "/seen-today"
    ).json()
    assert [item["id"] for item in items] == ["item-1", "item-0"]
    assert all(item["is_read"] is True for item in items)
    assert all(item["has_body"] is False for item in items)
    assert all("cover_url" in item for item in items)


def test_marking_read_and_unread_moves_an_item_on_and_off_the_list(tmp_path: Path) -> None:
    path = tmp_path / "toggle.sqlite"
    _seed(path, [None])
    client = TestClient(create_app(f"sqlite:///{path}"))
    assert client.get("/seen-today").json() == []
    client.put("/content/item-0/state", json={"is_read": True})
    assert [item["id"] for item in client.get("/seen-today").json()] == ["item-0"]
    client.put("/content/item-0/state", json={"is_read": False})
    assert client.get("/seen-today").json() == []
