"""Single-process, non-overlapping polling of configured sources.

Both the background poller and API-triggered fetches go through ``fetch_now_sync``
or ``_fetch_one`` under the same ``asyncio.Lock``. SQLite allows one writer at a
time, so letting API endpoints open their own event loop used to race the poller.
"""

import asyncio
import inspect
import logging
import math
import threading
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


def _failed_result(source_id: str, error: str) -> FetchResult:
    return FetchResult(
        source_id=source_id,
        source_name=source_id,
        discovered=0,
        inserted=0,
        updated=0,
        skipped=0,
        ok=False,
        error=error,
    )


class FetchScheduler:
    """Own one task per application; use a fresh transaction for each source."""

    def __init__(
        self,
        factory: sessionmaker[Session],
        *,
        poll_seconds: float = 30,
        fetcher_factory: Callable[..., Fetcher] = Fetcher,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        if not math.isfinite(poll_seconds) or poll_seconds <= 0:
            raise ValueError("poll_seconds must be finite and positive")
        self._factory = factory
        self._poll_seconds = poll_seconds
        self._fetcher_factory = fetcher_factory
        self._now = now
        self._lock = asyncio.Lock()
        self._sync_lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[None] | None = None
        self.current_source: str | None = None
        self.last_checked_at: datetime | None = None
        self.last_error: str | None = None
        self.revision = 0

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._loop = asyncio.get_running_loop()
            self._task = asyncio.create_task(self._run(), name="source-fetcher")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self._loop = None

    async def _run(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception as exc:
                self.last_error = str(exc)
                logger.exception("Source polling failed; retrying on the next check")
            await asyncio.sleep(self._poll_seconds)

    def fetch_now_sync(self, source_id: str, *, timeout: float = 180.0) -> FetchResult:
        """Run one fetch from a synchronous endpoint without racing the poller.

        When the poller owns a running loop, the coroutine is scheduled on that
        loop so the shared ``asyncio.Lock`` serializes both paths. Otherwise a
        short-lived loop is used, guarded by a thread lock for concurrent API
        callers.
        """

        loop = self._loop
        if loop is not None and loop.is_running():
            future = asyncio.run_coroutine_threadsafe(self._fetch_exclusive(source_id), loop)
            try:
                return future.result(timeout=timeout)
            except Exception as exc:
                logger.exception("Manual fetch for %s failed", source_id)
                return _failed_result(source_id, str(exc))
        with self._sync_lock:
            return asyncio.run(self._fetch_exclusive(source_id))

    async def _fetch_exclusive(self, source_id: str) -> FetchResult:
        async with self._lock:
            return await self._fetch_one(source_id)

    async def run_once(self) -> list[FetchResult]:
        """Fetch only enabled, due sources; failures do not stop the next source."""

        async with self._lock:
            self.last_error = None
            due: list[tuple[str, str]] = []
            with self._factory() as session:
                ids = list(
                    session.scalars(
                        select(Source.id).where(Source.enabled.is_(True)).order_by(Source.id)
                    )
                )
                for source_id in ids:
                    source = session.get(Source, source_id)
                    if source is None or not source.enabled:
                        continue
                    next_at = next_fetch_at(source)
                    if next_at is not None and next_at > self._now():
                        continue
                    due.append((source_id, source.name))
            results: list[FetchResult] = []
            try:
                for source_id, name in due:
                    self.current_source = name
                    results.append(await self._fetch_one(source_id))
                self.last_checked_at = self._now()
                return results
            finally:
                self.current_source = None

    async def _fetch_one(self, source_id: str) -> FetchResult:
        """Fetch and commit one source; never raise into the caller."""

        with self._factory() as session:
            source = session.get(Source, source_id)
            if source is None:
                return _failed_result(source_id, f"source {source_id} was not found")
            started = self._now()
            try:
                async with self._make_fetcher(source) as fetcher:
                    result = await IngestionPipeline(session, fetcher).fetch_source(source_id)
                session.commit()
            except Exception as exc:
                session.rollback()
                logger.exception("Unexpected fetch error for %s", source_id)
                self._record_failure(source_id, started, exc)
                self.last_error = str(exc)
                return _failed_result(source_id, str(exc))
            if not result.ok:
                self.last_error = result.error
            elif result.inserted or result.updated:
                # Manual fetches also need to wake up other open tabs.
                self.revision += 1
            logger.info(
                "Fetched %s: %d new, %d updated, ok=%s",
                source_id,
                result.inserted,
                result.updated,
                result.ok,
            )
            return result

    def _make_fetcher(self, source: Source) -> Fetcher:
        """Wire the per-source request budget that was previously dead config."""

        config = source.config_json if isinstance(source.config_json, dict) else {}
        options: dict[str, object] = {}
        interval = config.get("min_request_interval")
        budget = config.get("max_requests_per_run")
        if isinstance(interval, (int, float)) and not isinstance(interval, bool):
            options["min_request_interval"] = float(interval)
        if isinstance(budget, int) and not isinstance(budget, bool):
            options["max_requests_per_run"] = budget
        if not options:
            return self._fetcher_factory()
        try:
            parameters = inspect.signature(self._fetcher_factory).parameters
        except (TypeError, ValueError):
            return self._fetcher_factory()
        accepted = {key: value for key, value in options.items() if key in parameters}
        return self._fetcher_factory(**accepted) if accepted else self._fetcher_factory()

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
