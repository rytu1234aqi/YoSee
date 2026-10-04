"""Single-process, non-overlapping polling of configured sources."""

import asyncio
import logging
import math
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from server.ingestion.fetcher import Fetcher
from server.ingestion.pipeline import FetchResult, IngestionPipeline
from server.models import FetchRun, FetchStatus, Source

logger = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.now(UTC)


def next_fetch_at(source: Source) -> datetime | None:
    """SQLite returns naive UTC dates. Repeated failures back off up to one day."""

    if source.last_fetched_at is None:
        return None
    last = (
        source.last_fetched_at.replace(tzinfo=UTC)
        if (source.last_fetched_at.tzinfo is None)
        else source.last_fetched_at
    )
    interval = max(1, source.fetch_interval_seconds)
    multiplier = 2 ** min(5, max(0, source.consecutive_failures - 1))
    delay = min(interval * multiplier, max(interval, 86400))
    return last + timedelta(seconds=delay)


class FetchScheduler:
    """Own one task per application; use a fresh transaction for each source."""

    def __init__(
        self,
        factory: sessionmaker[Session],
        *,
        poll_seconds: float = 30,
        fetcher_factory: Callable[[], Fetcher] = Fetcher,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        if not math.isfinite(poll_seconds) or poll_seconds <= 0:
            raise ValueError("poll_seconds must be finite and positive")
        self._factory = factory
        self._poll_seconds = poll_seconds
        self._fetcher_factory = fetcher_factory
        self._now = now
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self.current_source: str | None = None
        self.last_checked_at: datetime | None = None
        self.last_error: str | None = None
        self.revision = 0

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="source-fetcher")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception as exc:
                self.last_error = str(exc)
                logger.exception("Source polling failed; retrying on the next check")
            await asyncio.sleep(self._poll_seconds)

    async def run_once(self) -> list[FetchResult]:
        """Fetch only enabled, due sources; failures do not stop the next source."""

        async with self._lock:
            self.last_error = None
            with self._factory() as session:
                ids = list(
                    session.scalars(
                        select(Source.id).where(Source.enabled.is_(True)).order_by(Source.id)
                    )
                )
            results: list[FetchResult] = []
            try:
                for source_id in ids:
                    started = self._now()
                    with self._factory() as session:
                        source = session.get(Source, source_id)
                        if source is None or not source.enabled:
                            continue
                        due = next_fetch_at(source)
                        if due is not None and due > started:
                            continue
                        self.current_source = source.name
                        try:
                            async with self._fetcher_factory() as fetcher:
                                result = await IngestionPipeline(session, fetcher).fetch_source(
                                    source_id
                                )
                            session.commit()
                        except Exception as exc:
                            session.rollback()
                            logger.exception("Unexpected fetch error for %s", source_id)
                            self._record_failure(source_id, started, exc)
                            self.last_error = str(exc)
                            continue
                        results.append(result)
                        if not result.ok:
                            self.last_error = result.error
                        elif result.inserted or result.updated:
                            self.revision += 1
                        logger.info(
                            "Fetched %s: %d new, %d updated, ok=%s",
                            source_id,
                            result.inserted,
                            result.updated,
                            result.ok,
                        )
                self.last_checked_at = self._now()
                return results
            finally:
                self.current_source = None

    def _record_failure(self, source_id: str, started: datetime, exc: Exception) -> None:
        # The failed transaction was rolled back; preserve diagnostics separately.
        with self._factory() as session:
            source = session.get(Source, source_id)
            if source is None:
                return
            source.last_fetched_at = started
            source.consecutive_failures += 1
            source.last_error = str(exc)
            session.add(
                FetchRun(
                    source_id=source_id,
                    started_at=started,
                    finished_at=self._now(),
                    status=FetchStatus.FAILED,
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )
            )
            session.commit()
