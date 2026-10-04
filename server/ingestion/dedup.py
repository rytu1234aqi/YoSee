"""Decide whether a normalized item is already known.

Two checks, in order: ``(source_id, external_id)`` when the external id is
present, then the SHA-256 of the normalized canonical URL. This module does not
open a session. Callers pass keys they already stored.
"""

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from server.ingestion.normalizer import NormalizedContentItem

# Tracking params only. Other query parameters can change which document is served.
_TRACKING_PARAMS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "ref",
        "fbclid",
        "gclid",
    }
)


class MatchOn(StrEnum):
    """Which dedup rule linked an item to one already known."""

    EXTERNAL_ID = "external_id"
    URL_HASH = "url_hash"


class DedupAction(StrEnum):
    """What the caller should do with an item. Never inserts a second row."""

    INSERT = "insert"
    UPDATE = "update"


@dataclass(frozen=True, slots=True)
class DedupKey:
    """Identity of one item after URL cleanup.

    ``canonical_url`` has tracking parameters removed. ``url_hash`` is the
    SHA-256 hex of that URL and is global, not scoped to a source.
    """

    source_id: str
    external_id: str | None
    canonical_url: str
    url_hash: str


@dataclass(frozen=True, slots=True)
class DedupDecision:
    """One item compared with known keys and earlier items in the same batch."""

    key: DedupKey
    action: DedupAction
    matched_on: MatchOn | None


class DedupService:
    """Fold duplicates inside one batch, given whatever keys are already stored."""

    def prepare(self, item: NormalizedContentItem) -> DedupKey:
        """Normalize the URL and compute its hash. Does not mutate ``item``."""

        canonical_url = normalize_url(item.canonical_url)
        return DedupKey(
            source_id=item.source_id,
            external_id=_external_id(item.external_id),
            canonical_url=canonical_url,
            url_hash=_sha256(canonical_url),
        )

    def decide(
        self,
        item: NormalizedContentItem,
        existing: Sequence[DedupKey] = (),
    ) -> DedupDecision:
        """Compare one item with already stored keys."""

        return self.decide_all([item], existing)[0]

    def decide_all(
        self,
        items: Sequence[NormalizedContentItem],
        existing: Sequence[DedupKey] = (),
    ) -> list[DedupDecision]:
        """Classify items in order.

        The first occurrence is an insert and becomes known to the rest of the
        batch. A later match is an update of that same item.
        """

        known = _Known(existing)
        decisions: list[DedupDecision] = []
        for item in items:
            key = self.prepare(item)
            matched_on = known.match(key)
            if matched_on is None:
                known.add(key)
                decisions.append(DedupDecision(key=key, action=DedupAction.INSERT, matched_on=None))
            else:
                decisions.append(
                    DedupDecision(key=key, action=DedupAction.UPDATE, matched_on=matched_on)
                )
        return decisions


def normalize_url(url: str) -> str:
    """Drop tracking parameters. Keep every other query parameter and the fragment."""

    parts = urlsplit(url.strip())
    kept = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in _TRACKING_PARAMS
    ]
    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            parts.path,
            urlencode(sorted(kept)),
            parts.fragment,
        )
    )


def canonical_url_hash(url: str) -> str:
    """SHA-256 hex of :func:`normalize_url`."""

    return _sha256(normalize_url(url))


class _Known:
    def __init__(self, keys: Sequence[DedupKey]) -> None:
        self._by_external: dict[tuple[str, str], DedupKey] = {}
        self._by_url: dict[str, DedupKey] = {}
        for key in keys:
            self.add(key)

    def add(self, key: DedupKey) -> None:
        if key.external_id is not None:
            self._by_external[(key.source_id, key.external_id)] = key
        self._by_url[key.url_hash] = key

    def match(self, key: DedupKey) -> MatchOn | None:
        if key.external_id is not None and (key.source_id, key.external_id) in self._by_external:
            return MatchOn.EXTERNAL_ID
        if key.url_hash in self._by_url:
            return MatchOn.URL_HASH
        return None


def _external_id(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
