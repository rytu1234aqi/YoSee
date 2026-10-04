"""Run one source through its provider and into the content repository.

The provider does not write. DedupService decides inside the repository. This
module records the FetchRun and the source status, then the caller commits.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from server.ingestion.dedup import canonical_url_hash
from server.ingestion.errors import IngestionError
from server.ingestion.fetcher import Fetcher
from server.ingestion.normalizer import NormalizedContentItem
from server.ingestion.providers import build_provider
from server.models.fetch_run import FetchRun, FetchStatus
from server.models.source import Source
from server.repositories.content_repository import ContentRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class FetchResult:
    """Outcome of one source. ``ok`` is false when that source did not complete."""

    source_id: str
    source_name: str
    discovered: int
    inserted: int
    updated: int
    skipped: int
    ok: bool
    error: str | None = None


class IngestionPipeline:
    """Fetch one configured source and persist its items."""

    def __init__(self, session: Session, fetcher: Fetcher) -> None:
        self._session = session
        self._fetcher = fetcher
        self._content = ContentRepository(session)

    async def fetch_source(self, source_id: str) -> FetchResult:
        """Fetch ``source_id``. A missing or failed source is a result, not a crash.

        An explicit id is fetched even when the source is disabled.
        """

        source = self._session.get(Source, source_id)
        if source is None:
            return self._missing(source_id)
        return await self._fetch(source)

    async def fetch_enabled(self) -> list[FetchResult]:
        """Fetch every enabled source. One failure does not stop the rest."""

        statement = select(Source).where(Source.enabled.is_(True)).order_by(Source.id)
        sources = list(self._session.scalars(statement))
        return [await self._fetch(source) for source in sources]

    def _missing(self, source_id: str) -> FetchResult:
        return FetchResult(
            source_id=source_id,
            source_name=source_id,
            discovered=0,
            inserted=0,
            updated=0,
            skipped=0,
            ok=False,
            error=f"source {source_id} was not found",
        )

    async def _fetch(self, source: Source) -> FetchResult:
        started = datetime.now(UTC)
        run = FetchRun(source_id=source.id, started_at=started, status=FetchStatus.RUNNING)
        self._session.add(run)
        # Do not acquire SQLite's writer lock while waiting for external HTTP.
        # Providers have no database access; flush only after collection finishes.
        source.last_fetched_at = started

        try:
            provider = build_provider(source.provider_type, self._fetcher, source_id=source.id)
            items, skipped, page = await provider.collect(source)
        except IngestionError as exc:
            logger.warning("fetch %s failed: %s", source.id, exc)
            return self._fail(source, run, datetime.now(UTC), exc)

        inserted = 0
        updated = 0
        for item in items:
            if self._already_stored(item):
                updated += 1
            else:
                inserted += 1
            self._content.upsert(item, fetched_at=started)
        _store_validators(source, page.etag, page.last_modified)
        return self._succeed(source, run, datetime.now(UTC), len(items), inserted, updated, skipped)

    def _already_stored(self, item: NormalizedContentItem) -> bool:
        external_id = item.external_id
        if external_id and self._content.get_by_external_id(item.source_id, external_id):
            return True
        return self._content.get_by_url_hash(canonical_url_hash(item.canonical_url)) is not None

    def _succeed(
        self,
        source: Source,
        run: FetchRun,
        finished: datetime,
        discovered: int,
        inserted: int,
        updated: int,
        skipped: int,
    ) -> FetchResult:
        run.finished_at = finished
        run.status = FetchStatus.SUCCESS
        run.items_discovered = discovered
        run.items_created = inserted
        run.items_updated = updated
        run.items_skipped = skipped
        source.last_success_at = finished
        source.consecutive_failures = 0
        source.last_error = None
        self._session.flush()
        return FetchResult(
            source_id=source.id,
            source_name=source.name,
            discovered=discovered,
            inserted=inserted,
            updated=updated,
            skipped=skipped,
            ok=True,
        )

    def _fail(
        self,
        source: Source,
        run: FetchRun,
        finished: datetime,
        exc: IngestionError,
    ) -> FetchResult:
        message = str(exc)
        run.finished_at = finished
        run.status = FetchStatus.FAILED
        run.error_type = type(exc).__name__
        run.error_message = message
        source.consecutive_failures = (source.consecutive_failures or 0) + 1
        source.last_error = message
        self._session.flush()
        return FetchResult(
            source_id=source.id,
            source_name=source.name,
            discovered=0,
            inserted=0,
            updated=0,
            skipped=0,
            ok=False,
            error=message,
        )


def _store_validators(source: Source, etag: str | None, last_modified: str | None) -> None:
    if etag is not None:
        source.etag = etag
    if last_modified is not None:
        source.last_modified = last_modified
