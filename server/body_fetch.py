"""Fetch and store the full text of articles the user saves.

Most link-aggregator entries only carry a URL. When one is saved, a background
worker visits the original page once and keeps a sanitized copy of the body, so
the collection can still be read offline. A failure never touches the link.
"""

from __future__ import annotations

import asyncio
import queue
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from server.ingestion.errors import FetchError
from server.ingestion.fetcher import Fetcher
from server.models import ContentItem, ReadingState
from server.security import (
    MAX_HTML_LENGTH,
    UnsafeUrlError,
    sanitize_html,
    validate_public_url,
)

MIN_BODY_CHARS = 80
QUEUE_POLL_SECONDS = 0.5
STOP_JOIN_SECONDS = 2.0
_DROPPED_TAGS = (
    "script",
    "style",
    "noscript",
    "nav",
    "header",
    "footer",
    "aside",
    "form",
    "svg",
    "iframe",
    "button",
)


def extract_body(markup: str, base_url: str = "") -> tuple[str | None, str | None]:
    """Return (sanitized HTML, plain text) for the main article region.

    This is deliberately conservative: a page without a recognizable article
    body is left alone rather than filling the reader with navigation chrome.
    """
    soup = BeautifulSoup(markup, "html.parser")
    for tag in soup(_DROPPED_TAGS):
        tag.decompose()
    root = (
        soup.find("article")
        or soup.find("main")
        or soup.select_one('[role="main"]')
        or soup.body
    )
    if root is None:
        return None, None
    for node in root.find_all(["img", "a"]):
        attr = "src" if node.name == "img" else "href"
        value = node.get(attr)
        if isinstance(value, str) and base_url and not value.startswith(("#", "mailto:")):
            try:
                node[attr] = urljoin(base_url, value)
            except ValueError:
                pass
    text = root.get_text(" ", strip=True)
    if len(text) < MIN_BODY_CHARS:
        return None, None
    html = sanitize_html(str(root)[:MAX_HTML_LENGTH])
    if not html:
        return None, None
    return html, text[:MAX_HTML_LENGTH]


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


class BodyFetchManager:
    """A single daemon thread that fills in the body of saved link-only items."""

    def __init__(
        self,
        factory: sessionmaker[Session],
        *,
        enabled: bool = True,
        fetcher_factory: Callable[..., Fetcher] = Fetcher,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._factory = factory
        self._enabled = enabled
        self._fetcher_factory = fetcher_factory
        self._now = now
        self._queue: queue.Queue[str] = queue.Queue()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._pending: set[str] = set()
        self._attempted: set[str] = set()
        self._completed = 0
        self._failed = 0
        self._last_error: str | None = None
        self._last_run_at: datetime | None = None

    def enqueue(self, content_id: str, *, force: bool = False) -> bool:
        """Queue one item for a body fetch. Never blocks the request path."""
        if not self._enabled:
            return False
        with self._lock:
            if force:
                self._attempted.discard(content_id)
            if content_id in self._pending or content_id in self._attempted:
                return False
            self._pending.add(content_id)
        self._queue.put(content_id)
        return True

    def enqueue_all_pending(self, *, force: bool = False) -> int:
        """Requeue every saved item that still has no body."""
        with self._factory() as session:
            ids = session.scalars(
                select(ContentItem.id)
                .join(ReadingState, ReadingState.content_id == ContentItem.id)
                .where(
                    ReadingState.saved.is_(True),
                    or_(ContentItem.content_html.is_(None), ContentItem.content_html == ""),
                    or_(ContentItem.content_text.is_(None), ContentItem.content_text == ""),
                )
            ).all()
        queued = 0
        for content_id in ids:
            queued += int(self.enqueue(content_id, force=force))
        return queued

    def start(self) -> None:
        if not self._enabled or self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="yosee-body-fetch", daemon=True
        )
        self._thread.start()
        self.enqueue_all_pending()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=STOP_JOIN_SECONDS)
        self._thread = None

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": self._enabled,
                "pending": len(self._pending),
                "attempted": len(self._attempted),
                "completed": self._completed,
                "failed": self._failed,
                "last_run_at": _iso(self._last_run_at),
                "last_error": self._last_error,
            }

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                content_id = self._queue.get(timeout=QUEUE_POLL_SECONDS)
            except queue.Empty:
                continue
            try:
                self._process(content_id)
            except Exception as exc:  # a daemon worker must never die silently
                self._record_failure(f"{type(exc).__name__}: {exc}")
            finally:
                with self._lock:
                    self._pending.discard(content_id)
                self._queue.task_done()

    def _record_failure(self, message: str) -> None:
        with self._lock:
            self._failed += 1
            self._last_error = message[:300]
            self._last_run_at = self._now()

    def _record_success(self) -> None:
        with self._lock:
            self._completed += 1
            self._last_error = None
            self._last_run_at = self._now()

    def _download(self, url: str) -> str:
        async def run() -> str:
            async with self._fetcher_factory(timeout=8.0, max_retries=1) as fetcher:
                response = await fetcher.get(url)
                return response.text

        return asyncio.run(run())

    def _process(self, content_id: str) -> None:
        with self._lock:
            self._attempted.add(content_id)
        with self._factory() as session:
            item = session.get(ContentItem, content_id)
            state = session.get(ReadingState, content_id)
            if item is None or state is None or not state.saved:
                return
            if (item.content_html or "").strip() or (item.content_text or "").strip():
                return
            url = item.canonical_url
        try:
            # Literal private addresses are rejected here; Fetcher repeats the
            # check with proxy-aware DNS resolution for every redirect hop.
            validated = validate_public_url(url)
            markup, text = extract_body(self._download(validated), validated)
        except (UnsafeUrlError, FetchError, ValueError, OSError) as exc:
            self._record_failure(str(exc))
            return
        if markup is None or text is None:
            self._record_failure("no readable article body found")
            return
        with self._factory() as session:
            item = session.get(ContentItem, content_id)
            state = session.get(ReadingState, content_id)
            if item is None or state is None or not state.saved:
                return
            if (item.content_html or "").strip() or (item.content_text or "").strip():
                return
            item.content_html = markup
            item.content_text = text
            session.commit()
        self._record_success()
