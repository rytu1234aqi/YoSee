"""Read-time cover discovery for articles stored before covers were kept.

Older rows have ``cover_url = NULL`` even when their saved HTML contains a
usable image. Rather than rewriting the database, the read endpoints derive a
cover from the first plausible ``<img>`` in the stored body or summary.
"""

from __future__ import annotations

import html
import re
from urllib.parse import urljoin, urlparse

SKIP_WORDS = (
    "icon",
    "avatar",
    "emoji",
    "pixel",
    "tracking",
    "badge",
    "sponsor",
    "logo",
    "1x1",
    "gravatar",
)

_STRIP_BLOCKS = re.compile(
    r"<\s*(?:script|style)\b[^>]*>[\s\S]*?<\s*/\s*(?:script|style)\s*>",
    re.IGNORECASE,
)
# A triple-quoted raw string keeps both quote characters literal.
_IMG_SRC = re.compile(
    r"""<img\b[^>]*?\s(?:src|data-src|data-original)\s*=\s*(["'])(.*?)\1""",
    re.IGNORECASE | re.DOTALL,
)


def clean_image_url(raw: str | None, base_url: str | None) -> str | None:
    """Return an absolute http(s) image URL, or None for junk / unsafe input."""
    if not isinstance(raw, str):
        return None
    try:
        text = html.unescape(raw.strip())
        if not text:
            return None
        resolved = urljoin(base_url, text) if base_url else text
        parsed = urlparse(resolved)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return None
        lowered = resolved.lower()
        if any(word in lowered for word in SKIP_WORDS):
            return None
        return resolved
    except (TypeError, ValueError):
        return None


def first_image_url(html_text: str | None, base_url: str | None = None) -> str | None:
    """Pick the first usable image in a stored HTML body or summary."""
    if not isinstance(html_text, str) or not html_text:
        return None
    try:
        cleaned = _STRIP_BLOCKS.sub(" ", html_text)
        for match in _IMG_SRC.finditer(cleaned):
            url = clean_image_url(match.group(2), base_url)
            if url is not None:
                return url
    except (TypeError, ValueError, re.error):
        return None
    return None
