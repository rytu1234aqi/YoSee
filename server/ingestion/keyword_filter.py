"""Optional per-source keyword allowlist ("只要什么") for discovered items.

A source opts in by storing ``config_json["keywords"]``. When that list is
empty, filtering is a complete no-op so existing sources keep every item.
"""

import re

from server.ingestion.normalizer import NormalizedContentItem
from server.models.source import Source

MIN_BODY_CHARS = 40
KEYWORDS_CONFIG_KEY = "keywords"

_MAX_KEYWORDS = 20
_SPLIT_KEYWORDS = re.compile(r"[,，、;\s]+")


def source_keywords(source: Source) -> list[str]:
    """Return a source's normalized keyword allowlist, or ``[]`` when unusable.

    ``config_json`` may not be a mapping and ``keywords`` may be a single string
    (comma/space separated) or a list of strings. Entries are stripped, dropped
    when empty, de-duplicated in order, lowercased and capped at 20. An invalid
    value is tolerated and yields an empty allowlist, which disables filtering.
    """

    config = source.config_json
    if not isinstance(config, dict):
        return []
    raw = config.get(KEYWORDS_CONFIG_KEY)
    if isinstance(raw, str):
        candidates: list[str] = _SPLIT_KEYWORDS.split(raw)
    elif isinstance(raw, list):
        if not all(isinstance(value, str) for value in raw):
            return []
        candidates = [value for value in raw if isinstance(value, str)]
    else:
        return []

    keywords: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        keyword = candidate.strip().lower()
        if not keyword or keyword in seen:
            continue
        seen.add(keyword)
        keywords.append(keyword)
        if len(keywords) >= _MAX_KEYWORDS:
            break
    return keywords


def item_matches(item: NormalizedContentItem, keywords: list[str]) -> bool:
    """Return whether an item passes the keyword allowlist.

    An empty allowlist accepts everything. Otherwise a keyword must appear in
    the lowercased ``title + summary + content_text`` text, and the item must
    have a real body: ``summary`` or ``content_text`` must be at least
    ``MIN_BODY_CHARS`` characters long after stripping, so title-only stubs are
    rejected even when their title matches.
    """

    if not keywords:
        return True
    summary = item.summary or ""
    body = item.content_text or ""
    if len(summary.strip()) < MIN_BODY_CHARS and len(body.strip()) < MIN_BODY_CHARS:
        return False
    text = f"{item.title} {summary} {body}".lower()
    return any(keyword.lower() in text for keyword in keywords)


def filter_items(
    items: list[NormalizedContentItem], keywords: list[str]
) -> tuple[list[NormalizedContentItem], int]:
    """Return the items to keep and how many the allowlist filtered out."""

    if not keywords:
        return list(items), 0
    kept = [item for item in items if item_matches(item, keywords)]
    return kept, len(items) - len(kept)
