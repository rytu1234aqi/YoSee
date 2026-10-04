"""ContentRepository lookups and upsert. Dedup decides; the repository writes."""

from datetime import UTC, datetime
from typing import Any

import pytest
from server.ingestion.dedup import canonical_url_hash
from server.ingestion.normalizer import NormalizedContentItem
from server.models import ContentItem, ContentType
from server.repositories import ContentRepository
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from tests.conftest import make_source

_FETCHED = datetime(2026, 6, 1, tzinfo=UTC)
_PUBLISHED = datetime(2026, 1, 1, tzinfo=UTC)


def _item(**overrides: Any) -> NormalizedContentItem:
    values: dict[str, Any] = {
        "source_id": "redblobgames",
        "external_id": "hex-grids",
        "content_type": ContentType.ARTICLE,
        "title": "Hexagonal Grids",
        "summary": "A guide to hex grids.",
        "canonical_url": "https://www.redblobgames.com/grids/hexagons/?utm_source=rss",
        "author": "Amit Patel",
        "language": "en",
        "published_at": _PUBLISHED,
        "updated_at": datetime(2026, 1, 2, tzinfo=UTC),
        "raw_metadata": {"tags": ["maps"]},
    }
    values.update(overrides)
    return NormalizedContentItem(**values)


def test_upsert_inserts_the_normalized_url_and_can_be_found(session: Session) -> None:
    make_source(session)
    repository = ContentRepository(session)
    item = _item()

    row = repository.upsert(item, fetched_at=_FETCHED)
    item.raw_metadata["tags"] = ["changed"]

    assert row.canonical_url == "https://www.redblobgames.com/grids/hexagons/"
    assert row.url_hash == canonical_url_hash(item.canonical_url)
    assert row.content_updated_at == datetime(2026, 1, 2, tzinfo=UTC)
    assert row.fetched_at == _FETCHED
    assert row.content_hash is None
    assert row.raw_metadata_json == {"tags": ["maps"]}
    assert repository.get_by_external_id("redblobgames", "hex-grids") is row
    assert repository.get_by_external_id("other", "hex-grids") is None
    assert repository.get_by_external_id("redblobgames", "  ") is None
    assert repository.get_by_url_hash(row.url_hash) is row
    assert repository.get_by_url_hash("missing") is None


def test_same_external_id_updates_the_row_instead_of_inserting(session: Session) -> None:
    make_source(session)
    repository = ContentRepository(session)
    first = repository.upsert(_item(title="Old"), fetched_at=_FETCHED)
    seen_again = datetime(2026, 6, 2, tzinfo=UTC)

    second = repository.upsert(
        _item(
            title="New",
            summary="Revised",
            content_html="<p>Revised</p>",
            canonical_url="https://www.redblobgames.com/grids/hexagons-2/",
        ),
        fetched_at=seen_again,
    )

    assert second.id == first.id
    assert second.title == "New"
    assert second.summary == "Revised"
    assert second.content_html == "<p>Revised</p>"
    assert second.canonical_url == "https://www.redblobgames.com/grids/hexagons-2/"
    assert second.fetched_at == seen_again
    assert session.scalars(select(ContentItem)).all() == [second]


def test_same_url_updates_and_fills_a_missing_external_id(session: Session) -> None:
    make_source(session)
    repository = ContentRepository(session)
    first = repository.upsert(
        _item(external_id=None, canonical_url="https://example.com/a"),
        fetched_at=_FETCHED,
    )

    second = repository.upsert(
        _item(
            external_id="hex-grids",
            title="Again",
            canonical_url="https://example.com/a?utm_source=twitter",
        ),
        fetched_at=_FETCHED,
    )

    assert second.id == first.id
    assert second.title == "Again"
    assert second.external_id == "hex-grids"
    assert second.canonical_url == "https://example.com/a"
    assert len(session.scalars(select(ContentItem)).all()) == 1


def test_same_url_from_another_source_does_not_steal_the_row(session: Session) -> None:
    make_source(session, "redblobgames")
    make_source(session, "other")
    repository = ContentRepository(session)
    first = repository.upsert(_item(title="First"), fetched_at=_FETCHED)

    second = repository.upsert(
        _item(
            source_id="other",
            external_id="other-id",
            title="Second",
            canonical_url=first.canonical_url,
        ),
        fetched_at=_FETCHED,
    )

    assert second.id == first.id
    assert second.source_id == "redblobgames"
    assert second.external_id == "hex-grids"
    assert second.title == "Second"
    assert len(session.scalars(select(ContentItem)).all()) == 1


def test_list_latest_puts_undated_rows_last(session: Session) -> None:
    make_source(session)
    repository = ContentRepository(session)
    repository.upsert(
        _item(
            external_id="old",
            title="Old",
            canonical_url="https://example.com/old",
            published_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
        fetched_at=_FETCHED,
    )
    repository.upsert(
        _item(
            external_id="new",
            title="New",
            canonical_url="https://example.com/new",
            published_at=datetime(2026, 2, 1, tzinfo=UTC),
        ),
        fetched_at=_FETCHED,
    )
    repository.upsert(
        _item(
            external_id="undated",
            title="Undated",
            canonical_url="https://example.com/undated",
            published_at=None,
        ),
        fetched_at=_FETCHED,
    )

    assert [row.title for row in repository.list_latest(limit=10)] == ["New", "Old", "Undated"]
    assert [row.title for row in repository.list_latest(limit=1)] == ["New"]


def test_list_latest_rejects_a_non_positive_limit(session: Session) -> None:
    with pytest.raises(ValueError, match="limit"):
        ContentRepository(session).list_latest(limit=0)


def test_upsert_requires_a_known_source(session: Session) -> None:
    with pytest.raises(IntegrityError):
        ContentRepository(session).upsert(_item(), fetched_at=_FETCHED)
