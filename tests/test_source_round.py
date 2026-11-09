"""The stream can be limited to one round of chosen sources."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from server.api import _parse_source_ids, create_app
from server.database.base import Base
from server.database.session import create_db_engine, create_session_factory
from server.models import ContentItem, ContentType, Source

_SOURCES = ("alpha", "beta", "gamma")
_PER_SOURCE = 3
_BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _seed(path: Path) -> str:
    url = f"sqlite:///{path}"
    engine = create_db_engine(url)
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    with factory() as session:
        for source_id in _SOURCES:
            session.add(
                Source(
                    id=source_id,
                    name=source_id.title(),
                    slug=source_id,
                    provider_type="rss",
                    fetch_interval_seconds=3600,
                )
            )
        session.flush()
        for s_index, source_id in enumerate(_SOURCES):
            for i in range(_PER_SOURCE):
                n = s_index * _PER_SOURCE + i
                session.add(
                    ContentItem(
                        id=f"item-{n}",
                        source_id=source_id,
                        content_type=ContentType.ARTICLE,
                        title=f"{source_id} {i}",
                        canonical_url=f"https://example.test/{n}",
                        url_hash=f"{n:064x}",
                        published_at=_BASE + timedelta(minutes=n),
                        fetched_at=_BASE,
                    )
                )
        session.commit()
    engine.dispose()
    return url


def test_parse_source_ids_cleans_and_bounds_the_list() -> None:
    assert _parse_source_ids(None) is None
    assert _parse_source_ids("") is None
    assert _parse_source_ids(" , , ") is None
    assert _parse_source_ids("alpha, beta ,,gamma") == ["alpha", "beta", "gamma"]
    assert _parse_source_ids("x" * 100) == ["x" * 64]
    assert len(_parse_source_ids(",".join(f"s{i}" for i in range(80)))) == 50


def test_feed_without_source_ids_keeps_returning_every_source(tmp_path: Path) -> None:
    client = TestClient(create_app(_seed(tmp_path / "all.sqlite")))
    assert len(client.get("/feed").json()) == 9
    assert len(client.get("/feed/page?limit=20").json()["items"]) == 9


def test_source_ids_filter_both_feed_endpoints(tmp_path: Path) -> None:
    client = TestClient(create_app(_seed(tmp_path / "round.sqlite")))
    for path in ("/feed?source_ids=alpha,gamma", "/feed/page?source_ids=alpha,gamma&limit=20"):
        items = client.get(path).json()
        if isinstance(items, dict):
            items = items["items"]
        assert len(items) == 6
        assert {item["source_id"] for item in items} == {"alpha", "gamma"}


def test_unknown_source_ids_produce_an_empty_page(tmp_path: Path) -> None:
    client = TestClient(create_app(_seed(tmp_path / "empty.sqlite")))
    assert client.get("/feed?source_ids=nope").json() == []
    page = client.get("/feed/page?source_ids=nope").json()
    assert page["items"] == []
    assert page["next_cursor"] is None


def test_source_ids_survive_cursor_paging(tmp_path: Path) -> None:
    client = TestClient(create_app(_seed(tmp_path / "paging.sqlite")))
    first = client.get("/feed/page?source_ids=beta&limit=2").json()
    assert len(first["items"]) == 2
    assert all(item["source_id"] == "beta" for item in first["items"])
    assert first["next_cursor"]
    second = client.get(
        f"/feed/page?source_ids=beta&limit=2&cursor={first['next_cursor']}"
    ).json()
    assert len(second["items"]) == 1
    assert second["items"][0]["source_id"] == "beta"
    assert second["next_cursor"] is None


def test_source_ids_combine_with_status_and_query(tmp_path: Path) -> None:
    client = TestClient(create_app(_seed(tmp_path / "combined.sqlite")))
    assert client.get("/feed?source_ids=alpha&status=unread").json()
    assert client.get("/feed?source_ids=alpha&q=alpha%202").json()[0]["id"] == "item-2"
    assert client.get("/feed?source_ids=beta&q=alpha").json() == []
