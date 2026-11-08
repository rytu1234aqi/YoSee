"""Daily lightweight backup for the local reader.

The database keeps full article bodies, fetch runs and cache validators. A
backup does not need any of that: it carries only the link stream, full text
for saved items, and the subscription list. Plain JSON keeps the file small,
human-readable and repairable by hand.

One file per day, kept next to the database. A run never raises into the
request path: a failed backup is recorded and retried on the next tick.
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine.url import make_url
from sqlalchemy.orm import Session, sessionmaker

from server.models import ContentItem, ReadingState, Source

logger = logging.getLogger(__name__)

FORMAT = "yosee-lite"
VERSION = 1
DAILY_KEEP = 7
WEEKLY_KEEP = 4
DEFAULT_INTERVAL_SECONDS = 24 * 60 * 60
_MEMORY_URLS = {"sqlite://", "sqlite:///:memory:"}


def utc_now() -> datetime:
    return datetime.now(UTC)


def default_backup_dir(database_url: str) -> Path | None:
    """Place backups beside the database instead of hard-coding ``data/``.

    Desktop and Android builds may point ``DATABASE_URL`` somewhere else, and
    in-memory databases have nothing worth backing up.
    """

    if database_url in _MEMORY_URLS:
        return None
    database = make_url(database_url).database
    if not database or database == ":memory:":
        return None
    return Path(database).expanduser().parent / "backups"


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


class BackupManager:
    """Own one daemon thread per application and one JSON file per day."""

    def __init__(
        self,
        factory: sessionmaker[Session],
        backup_dir: Path,
        *,
        enabled: bool = True,
        now: Callable[[], datetime] = utc_now,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        daily_keep: int = DAILY_KEEP,
        weekly_keep: int = WEEKLY_KEEP,
    ) -> None:
        if not math.isfinite(interval_seconds) or interval_seconds <= 0:
            raise ValueError("interval_seconds must be finite and positive")
        self._factory = factory
        self._dir = backup_dir
        self._enabled = enabled
        self._now = now
        self._interval = interval_seconds
        self._daily_keep = daily_keep
        self._weekly_keep = weekly_keep
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_backup_at: datetime | None = None
        self.last_error: str | None = None
        self.last_path: Path | None = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    def build_payload(self) -> dict[str, Any]:
        """Read the three things worth keeping, in one consistent snapshot."""

        with self._factory() as session:
            source_rows = session.scalars(select(Source).order_by(Source.id)).all()
            item_rows = session.execute(
                select(ContentItem, ReadingState)
                .outerjoin(ReadingState, ReadingState.content_id == ContentItem.id)
                .order_by(
                    ContentItem.published_at.desc().nulls_last(),
                    ContentItem.id.desc(),
                )
            ).all()

            sources: list[dict[str, Any]] = [
                {
                    "id": row.id,
                    "slug": row.slug,
                    "name": row.name,
                    "provider_type": row.provider_type,
                    "homepage_url": row.homepage_url,
                    "enabled": row.enabled,
                    "config_json": row.config_json,
                    "fetch_interval_seconds": row.fetch_interval_seconds,
                }
                for row in source_rows
            ]

            links: list[dict[str, Any]] = []
            saved_articles: list[dict[str, Any]] = []
            for item, state in item_rows:
                seen = bool(state.is_read) if state is not None else False
                saved = bool(state.saved) if state is not None else False
                links.append(
                    {
                        "url": item.canonical_url,
                        "title": item.title,
                        "source_id": item.source_id,
                        "published_at": _iso(item.published_at),
                        "seen": seen,
                        "saved": saved,
                    }
                )
                if saved:
                    saved_articles.append(
                        {
                            "url": item.canonical_url,
                            "title": item.title,
                            "summary": item.summary,
                            "content_html": item.content_html,
                            "content_text": item.content_text,
                            "source_id": item.source_id,
                            "author": item.author,
                            "published_at": _iso(item.published_at),
                        }
                    )

        return {
            "format": FORMAT,
            "version": VERSION,
            "generated_at": _iso(self._now()),
            "counts": {
                "sources": len(sources),
                "links": len(links),
                "saved": len(saved_articles),
            },
            "sources": sources,
            "links": links,
            "saved_articles": saved_articles,
        }

    @staticmethod
    def _daily_name(moment: datetime) -> str:
        return f"daily-{moment.date().isoformat()}.json"

    @staticmethod
    def _weekly_name(moment: datetime) -> str:
        iso = moment.isocalendar()
        return f"weekly-{iso.year}-W{iso.week:02d}.json"

    def _atomic_write(self, target: Path, payload: dict[str, Any]) -> None:
        """Write a temp file, verify it parses, then rename into place."""

        self._dir.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.tmp")
        try:
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            parsed = json.loads(tmp.read_text(encoding="utf-8"))
            if parsed.get("format") != FORMAT or "links" not in parsed:
                raise RuntimeError("backup verification failed")
            os.replace(tmp, target)
        finally:
            tmp.unlink(missing_ok=True)

    def prune(self) -> None:
        """Keep only the newest files we created; never touch anything else."""

        if not self._dir.exists():
            return
        for prefix, keep in (
            ("daily-", self._daily_keep),
            ("weekly-", self._weekly_keep),
        ):
            files = sorted(
                self._dir.glob(f"{prefix}*.json"),
                key=lambda path: path.name,
                reverse=True,
            )
            for path in files[keep:]:
                path.unlink(missing_ok=True)

    def write_backup(self, moment: datetime | None = None) -> Path:
        """Write today's snapshot (plus a weekly copy on Sundays)."""

        when = moment or self._now()
        payload = self.build_payload()
        daily = self._dir / self._daily_name(when)
        self._atomic_write(daily, payload)
        if when.isoweekday() == 7:
            self._atomic_write(self._dir / self._weekly_name(when), payload)
        self.prune()
        self.last_backup_at = when
        self.last_path = daily
        self.last_error = None
        logger.info(
            "Daily backup written: %s (sources=%d links=%d saved=%d)",
            daily,
            payload["counts"]["sources"],
            payload["counts"]["links"],
            payload["counts"]["saved"],
        )
        return daily

    def run_once(self) -> bool:
        """Never let a backup failure break reading or fetching."""

        if not self._enabled:
            return False
        with self._lock:
            try:
                self.write_backup()
                return True
            except Exception as exc:  # deliberately isolated from the request path
                self.last_error = str(exc)
                logger.exception("Daily backup failed; retrying on the next run")
                return False

    def start(self) -> None:
        if not self._enabled or self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="daily-backup", daemon=True
        )
        self._thread.start()

    def _loop(self) -> None:
        # Catch up after downtime: if today's file is missing, write it now.
        if not (self._dir / self._daily_name(self._now())).exists():
            self.run_once()
        while not self._stop.wait(self._interval):
            self.run_once()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def status(self) -> dict[str, Any]:
        files = (
            sorted(self._dir.glob("daily-*.json")) if self._dir.exists() else []
        )
        return {
            "enabled": self._enabled,
            "format": FORMAT,
            "directory": str(self._dir),
            "last_backup_at": _iso(self.last_backup_at),
            "latest_file": files[-1].name if files else None,
            "files": len(files),
            "last_error": self.last_error,
        }
