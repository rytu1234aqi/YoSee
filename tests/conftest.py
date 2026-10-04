"""In-memory SQLite session with the Milestone 0 schema."""

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from server.database.base import Base
from server.database.session import create_db_engine, create_session_factory
from server.models import ContentItem, ContentType, Source
from sqlalchemy.orm import Session


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    with factory() as db_session:
        try:
            yield db_session
        finally:
            db_session.rollback()
    engine.dispose()


def make_source(session: Session, source_id: str = "redblobgames") -> Source:
    source = Source(
        id=source_id,
        name="Red Blob Games",
        slug=source_id,
        provider_type="rss",
        homepage_url="https://www.redblobgames.com/",
        fetch_interval_seconds=21600,
        config_json={"feed_url": "https://www.redblobgames.com/atom.xml"},
    )
    session.add(source)
    session.flush()
    return source


def make_item(
    session: Session,
    source: Source,
    *,
    title: str = "Hex grids",
    canonical_url: str = "https://www.redblobgames.com/grids/hexagons/",
    url_hash: str = "a" * 64,
    external_id: str | None = "hex-grids",
) -> ContentItem:
    item = ContentItem(
        source_id=source.id,
        external_id=external_id,
        content_type=ContentType.ARTICLE,
        title=title,
        canonical_url=canonical_url,
        url_hash=url_hash,
        fetched_at=datetime.now(UTC),
    )
    session.add(item)
    session.flush()
    return item
