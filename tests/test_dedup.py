"""Dedup rules: source external id, then the normalized canonical URL hash."""

import hashlib
from typing import Any

from server.ingestion.dedup import (
    DedupAction,
    DedupService,
    MatchOn,
    canonical_url_hash,
    normalize_url,
)
from server.ingestion.normalizer import NormalizedContentItem
from server.models import ContentType


def _item(**overrides: Any) -> NormalizedContentItem:
    values: dict[str, Any] = {
        "source_id": "redblobgames",
        "content_type": ContentType.ARTICLE,
        "title": "Hexagonal Grids",
        "canonical_url": "https://www.redblobgames.com/grids/hexagons/",
    }
    values.update(overrides)
    return NormalizedContentItem(**values)


def test_tracking_params_are_removed_and_other_params_stay() -> None:
    dirty = "https://Example.com/a?b=2&utm_source=twitter&id=3&utm_medium=social&ref=home"
    also = "https://example.com/a?fbclid=abc&gclid=xyz&utm_campaign=spring&id=3&b=2#section"

    assert normalize_url(dirty) == "https://example.com/a?b=2&id=3"
    assert normalize_url(also) == "https://example.com/a?b=2&id=3#section"
    assert canonical_url_hash(dirty) == canonical_url_hash(also.split("#", 1)[0])
    assert len(canonical_url_hash(dirty)) == 64


def test_prepare_cleans_the_url_without_changing_the_item() -> None:
    item = _item(
        external_id="  hex-grids  ",
        canonical_url="https://www.redblobgames.com/grids/hexagons/?utm_source=rss",
    )

    key = DedupService().prepare(item)

    assert item.canonical_url.endswith("utm_source=rss")
    assert key.external_id == "hex-grids"
    assert key.canonical_url == "https://www.redblobgames.com/grids/hexagons/"
    assert key.url_hash == hashlib.sha256(key.canonical_url.encode("utf-8")).hexdigest()


def test_blank_external_id_is_ignored() -> None:
    key = DedupService().prepare(_item(external_id="  "))

    assert key.external_id is None


def test_same_external_id_updates_even_when_the_url_differs() -> None:
    service = DedupService()
    stored = service.prepare(_item(external_id="hex-grids"))
    decision = service.decide(
        _item(
            external_id="hex-grids", canonical_url="https://www.redblobgames.com/grids/hexagons-2/"
        ),
        [stored],
    )

    assert decision.action is DedupAction.UPDATE
    assert decision.matched_on is MatchOn.EXTERNAL_ID


def test_external_id_is_only_unique_inside_one_source() -> None:
    service = DedupService()
    stored = service.prepare(_item(source_id="redblobgames", external_id="1"))
    decision = service.decide(
        _item(
            source_id="other",
            external_id="1",
            canonical_url="https://example.com/different",
        ),
        [stored],
    )

    assert decision.action is DedupAction.INSERT
    assert decision.matched_on is None


def test_same_normalized_url_matches_across_sources() -> None:
    service = DedupService()
    stored = service.prepare(_item(source_id="blog", canonical_url="https://example.com/a"))
    decision = service.decide(
        _item(
            source_id="newsletter",
            external_id="issue-4",
            canonical_url="https://example.com/a?utm_campaign=spring",
        ),
        [stored],
    )

    assert decision.action is DedupAction.UPDATE
    assert decision.matched_on is MatchOn.URL_HASH
    assert decision.key.url_hash == stored.url_hash


def test_batch_keeps_the_first_item_and_updates_later_copies() -> None:
    service = DedupService()
    decisions = service.decide_all(
        [
            _item(canonical_url="https://example.com/a?utm_source=x"),
            _item(canonical_url="https://example.com/a?fbclid=1"),
            _item(canonical_url="https://example.com/b?page=2"),
        ]
    )

    assert [decision.action for decision in decisions] == [
        DedupAction.INSERT,
        DedupAction.UPDATE,
        DedupAction.INSERT,
    ]
    assert decisions[1].matched_on is MatchOn.URL_HASH
    assert decisions[0].key.url_hash == decisions[1].key.url_hash
    assert decisions[2].key.canonical_url == "https://example.com/b?page=2"


def test_meaningful_query_values_stay_distinct() -> None:
    service = DedupService()
    decisions = service.decide_all(
        [
            _item(canonical_url="https://example.com/a?page=1"),
            _item(canonical_url="https://example.com/a?page=2"),
        ]
    )

    assert [decision.action for decision in decisions] == [DedupAction.INSERT, DedupAction.INSERT]
    assert decisions[0].key.url_hash != decisions[1].key.url_hash
