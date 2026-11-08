"""Periodic cleanup of stale links for the local reader.

只删「已读且未收藏」的旧链接，收藏和未读永不删：只有带 ReadingState 且
``is_read=True`` / ``saved=False``、发布时间早于宽限期、又不在最近
``keep_recent`` 条保护区内的文章才会被清除。缺失或为 NULL 的阅读状态一律
按未读/未收藏处理，因此永远不会删除。删除 ContentItem 会由数据库级联删除
对应的 ReadingState（``PRAGMA foreign_keys=ON`` + ``ON DELETE CASCADE``）。

一次运行失败只记录 ``last_error``，绝不向调用方抛出；守护线程启动时先等待
一个完整周期再执行第一次清理，避免重启时误删刚抓取的数据。
"""

from __future__ import annotations

import logging
import math
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from server.models import ContentItem, ReadingState

logger = logging.getLogger(__name__)

KEEP_RECENT = 200
MIN_AGE_DAYS = 30
DEFAULT_INTERVAL_SECONDS = 24 * 60 * 60


def utc_now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    """Normalise a datetime to aware UTC (SQLite returns naive datetimes)."""

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


class CleanupManager:
    """Own one daemon thread that periodically removes stale read links."""

    def __init__(
        self,
        factory: sessionmaker[Session],
        *,
        enabled: bool = True,
        now: Callable[[], datetime] = utc_now,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        keep_recent: int = KEEP_RECENT,
        min_age_days: int = MIN_AGE_DAYS,
    ) -> None:
        if not math.isfinite(interval_seconds) or interval_seconds <= 0:
            raise ValueError("interval_seconds must be finite and positive")
        self._factory = factory
        self._enabled = enabled
        self._now = now
        self._interval = interval_seconds
        self._keep_recent = keep_recent
        self._min_age_days = min_age_days
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_run_at: datetime | None = None
        self.last_deleted: int = 0
        self.last_error: str | None = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    def select_ids(self) -> list[str]:
        """Return the ids the next run would delete; read-only, no writes."""

        cutoff = _as_utc(self._now()) - timedelta(days=self._min_age_days)
        result: list[str] = []
        with self._factory() as session:
            protected = set(
                session.scalars(
                    select(ContentItem.id)
                    .order_by(ContentItem.published_at.desc().nulls_last(), ContentItem.id.desc())
                    .limit(self._keep_recent)
                ).all()
            )
            candidates = session.scalars(
                select(ContentItem)
                .join(ReadingState, ReadingState.content_id == ContentItem.id)
                .where(
                    ReadingState.is_read.is_(True),
                    ReadingState.saved.is_(False),
                )
                .order_by(ContentItem.published_at.desc().nulls_last(), ContentItem.id.desc())
            ).all()
            for item in candidates:
                if item.id in protected:
                    continue
                if item.published_at is None:
                    continue
                if _as_utc(item.published_at) > cutoff:
                    continue
                result.append(item.id)
        return result

    def run_once(self) -> bool:
        """Delete one batch; never let a failure escape into the request path."""

        if not self._enabled:
            return False
        with self._lock:
            try:
                ids = self.select_ids()
                with self._factory() as session:
                    session.execute(delete(ContentItem).where(ContentItem.id.in_(ids)))
                    session.commit()
                self.last_deleted = len(ids)
                self.last_run_at = self._now()
                self.last_error = None
                logger.info("Content cleanup removed %d stale item(s)", len(ids))
                return True
            except Exception as exc:  # deliberately isolated from the request path
                self.last_error = str(exc)
                logger.exception("Content cleanup failed; retrying on the next run")
                return False

    def start(self) -> None:
        if not self._enabled or self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="content-cleanup", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        # Wait one full interval before the first run so a restart never wipes
        # data that was just fetched.
        while not self._stop.wait(self._interval):
            self.run_once()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self._enabled,
            "keep_recent": self._keep_recent,
            "min_age_days": self._min_age_days,
            "last_run_at": _iso(self.last_run_at),
            "last_deleted": self.last_deleted,
            "last_error": self.last_error,
        }
