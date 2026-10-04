"""The local reader lists and searches stored items. It does not write."""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from server.database.base import Base
from server.database.session import create_db_engine, create_session_factory
from server.models import ContentItem, ContentType, Source
from server.reader.app import _card_label, _meta
from server.reader.library import Library, display_text, sqlite_path
from sqlalchemy.orm import Session


def _database(tmp_path: Path) -> str:
    path = tmp_path / "reader.sqlite"
    url = f"sqlite:///{path}"
    engine = create_db_engine(url)
    Base.metadata.create_all(engine)
    with create_session_factory(engine)() as session:
        _seed(session)
        session.commit()
    engine.dispose()
    return url


def _seed(session: Session) -> None:
    session.add_all(
        [
            Source(
                id="redblobgames",
                name="Red Blob Games",
                slug="redblobgames",
                provider_type="rss",
                fetch_interval_seconds=21600,
            ),
            Source(
                id="godot",
                name="Godot Engine",
                slug="godot",
                provider_type="rss",
                fetch_interval_seconds=7200,
            ),
        ]
    )
    session.flush()
    session.add_all(
        [
            ContentItem(
                id="older",
                source_id="redblobgames",
                content_type=ContentType.ARTICLE,
                title="Hexagonal Grids",
                summary="A guide to hex grids.",
                canonical_url="https://example.com/hex",
                url_hash="a" * 64,
                published_at=datetime(2026, 1, 1, tzinfo=UTC),
                fetched_at=datetime(2026, 1, 2, tzinfo=UTC),
                content_html="<p>Full <b>article</b></p>",
            ),
            ContentItem(
                id="newer",
                source_id="godot",
                content_type=ContentType.ARTICLE,
                title="100% snapshot",
                summary="What changed.",
                author="Godot",
                canonical_url="https://example.com/godot",
                url_hash="b" * 64,
                published_at=datetime(2026, 2, 1, tzinfo=UTC),
                fetched_at=datetime(2026, 2, 2, tzinfo=UTC),
            ),
        ]
    )


def test_list_is_newest_first_and_search_is_literal(tmp_path: Path) -> None:
    library = Library.open(_database(tmp_path))
    try:
        listed = library.list_items()
        found = library.list_items(query="hex")
        percent = library.list_items(query="%")
        by_source = library.list_items(source_id="godot")
        article = library.get("older")
        opened = library.get("newer")

        assert [item.id for item in listed] == ["newer", "older"]
        assert [item.title for item in found] == ["Hexagonal Grids"]
        assert [item.id for item in percent] == ["newer"]
        assert [item.id for item in by_source] == ["newer"]
        assert library.sources() == [("godot", "Godot Engine"), ("redblobgames", "Red Blob Games")]
        assert article is not None
        assert display_text(article) == "Full article"
        assert opened is not None
        assert _card_label(listed[0]).startswith("2026-02-01  Godot Engine")
        assert _meta(opened) == "Godot Engine · Godot · 2026-02-01 00:00"
    finally:
        library.close()


def test_reader_does_not_write(tmp_path: Path) -> None:
    library = Library.open(_database(tmp_path))
    try:
        with pytest.raises(sqlite3.OperationalError):
            library._connection.execute("DELETE FROM content_items")
    finally:
        library.close()


def test_missing_database_is_reported(tmp_path: Path) -> None:
    missing = tmp_path / "missing.sqlite"
    with pytest.raises(FileNotFoundError):
        Library.open(f"sqlite:///{missing}")
    with pytest.raises(ValueError, match="sqlite"):
        sqlite_path("postgresql://localhost/app")
