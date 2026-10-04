"""Schema behavior for Source, ContentItem, and FetchRun."""

from datetime import UTC, datetime

import pytest
from server.models import ContentItem, ContentType, FetchRun, FetchStatus, Source
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from tests.conftest import make_item, make_source


def test_sqlite_foreign_keys_are_enabled(session: Session) -> None:
    enabled = session.execute(text("PRAGMA foreign_keys")).scalar_one()
    assert enabled == 1


def test_content_item_round_trip_keeps_metadata_only_body_empty(session: Session) -> None:
    source = make_source(session)
    published_at = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    item = ContentItem(
        source_id=source.id,
        external_id="hex-grids",
        content_type=ContentType.ARTICLE,
        title="Hexagonal Grids",
        summary="Geometry of hex grids.",
        canonical_url="https://www.redblobgames.com/grids/hexagons/",
        author="Amit Patel",
        language="en",
        published_at=published_at,
        content_updated_at=published_at,
        fetched_at=datetime(2026, 1, 3, tzinfo=UTC),
        reading_time_minutes=12,
        url_hash="b" * 64,
        content_hash="c" * 64,
        raw_metadata_json={"feed": "atom"},
        parser_version=None,
    )
    session.add(item)
    session.commit()

    stored = session.scalars(select(ContentItem)).one()
    assert stored.id
    assert stored.content_html is None
    assert stored.content_text is None
    assert stored.parser_version is None
    assert stored.title == "Hexagonal Grids"
    assert stored.content_type is ContentType.ARTICLE
    assert stored.raw_metadata_json == {"feed": "atom"}
    assert stored.published_at == published_at
    assert stored.content_updated_at == published_at
    assert stored.created_at is not None
    assert stored.updated_at is not None
    assert stored.source.slug == "redblobgames"


def test_source_id_is_the_config_key_not_a_generated_uuid(session: Session) -> None:
    source = make_source(session, "redblobgames")
    session.commit()

    stored = session.get(Source, "redblobgames")
    assert stored is not None
    assert stored.id == source.id
    assert stored.enabled is True
    assert stored.consecutive_failures == 0
    assert stored.config_json == {"feed_url": "https://www.redblobgames.com/atom.xml"}
    assert stored.etag is None


def test_same_external_id_on_two_sources_is_allowed(session: Session) -> None:
    first = make_source(session, "redblobgames")
    second = Source(
        id="gaffer",
        name="Gaffer On Games",
        slug="gaffer",
        provider_type="rss",
        fetch_interval_seconds=21600,
    )
    session.add(second)
    session.flush()

    make_item(session, first, external_id="shared", url_hash="1" * 64)
    make_item(
        session,
        second,
        external_id="shared",
        canonical_url="https://gafferongames.com/post/",
        url_hash="2" * 64,
    )
    session.commit()

    assert len(session.scalars(select(ContentItem)).all()) == 2


def test_duplicate_external_id_within_a_source_is_rejected(session: Session) -> None:
    source = make_source(session)
    make_item(session, source, external_id="shared", url_hash="1" * 64)

    with pytest.raises(IntegrityError):
        make_item(
            session,
            source,
            external_id="shared",
            canonical_url="https://example.com/other",
            url_hash="2" * 64,
        )


def test_missing_external_id_does_not_collapse_items(session: Session) -> None:
    source = make_source(session)
    make_item(session, source, external_id=None, url_hash="1" * 64)
    make_item(
        session,
        source,
        external_id=None,
        title="Another",
        canonical_url="https://example.com/another",
        url_hash="2" * 64,
    )
    session.commit()

    assert len(session.scalars(select(ContentItem)).all()) == 2


def test_duplicate_canonical_url_hash_is_rejected_across_sources(session: Session) -> None:
    first = make_source(session, "redblobgames")
    second = Source(
        id="mirror",
        name="Mirror",
        slug="mirror",
        provider_type="rss",
        fetch_interval_seconds=7200,
    )
    session.add(second)
    session.flush()
    make_item(session, first, url_hash="f" * 64)

    with pytest.raises(IntegrityError):
        make_item(
            session,
            second,
            external_id="other",
            canonical_url="https://www.redblobgames.com/grids/hexagons/?utm_source=x",
            url_hash="f" * 64,
        )


def test_content_item_requires_a_real_source(session: Session) -> None:
    with pytest.raises(IntegrityError):
        make_item(
            session,
            Source(
                id="missing",
                name="Missing",
                slug="missing",
                provider_type="rss",
                fetch_interval_seconds=60,
            ),
        )


def test_fetch_run_defaults_and_belongs_to_source(session: Session) -> None:
    source = make_source(session)
    started_at = datetime(2026, 3, 1, tzinfo=UTC)
    run = FetchRun(source_id=source.id, started_at=started_at)
    session.add(run)
    session.commit()

    stored = session.scalars(select(FetchRun)).one()
    assert stored.status is FetchStatus.RUNNING
    assert stored.items_discovered == 0
    assert stored.items_created == 0
    assert stored.items_updated == 0
    assert stored.items_skipped == 0
    assert stored.finished_at is None
    assert stored.error_message is None
    assert stored.source.id == "redblobgames"
    assert source.fetch_runs == [stored]
