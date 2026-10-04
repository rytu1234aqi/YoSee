"""Adapter contract for every external source.

A provider discovers raw items and maps them to ``NormalizedContentItem``. It must
not open a session, commit a transaction, or decide how the feed is sorted.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from server.ingestion.normalizer import NormalizedContentItem
from server.models.source import Source

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DiscoverPage:
    """One page of provider-specific raw items.

    ``items`` stay raw until ``normalize``. ``next_cursor`` is ``None`` when the
    source has no further page. HTTP providers set ``not_modified`` when the cache
    validators still match; ``items`` is then empty and the stored copy should stay.
    ``etag`` and ``last_modified`` are the validators from this response, if any.
    """

    items: list[Any]
    next_cursor: str | None = None
    not_modified: bool = False
    etag: str | None = None
    last_modified: str | None = None


class SourceAdapter(ABC):
    """Turn one configured source into normalized content."""

    @abstractmethod
    async def discover(self, source: Source, cursor: str | None = None) -> DiscoverPage:
        """List raw items. Pass ``cursor`` to continue a previous page."""

    @abstractmethod
    async def fetch_detail(self, raw_item: Any) -> Any:
        """Load anything ``discover`` did not already include.

        Sources that are complete at discovery, such as a full RSS entry, return
        ``raw_item`` unchanged.
        """

    @abstractmethod
    async def normalize(self, raw_item: Any) -> NormalizedContentItem:
        """Map one raw item onto the shared content model."""

    async def health_check(self) -> bool:
        """Return whether this source looks reachable. The default is healthy."""

        return True

    async def load(self, source: Source) -> list[NormalizedContentItem]:
        """Return every item that normalizes. See :meth:`collect`."""

        items, _skipped, _page = await self.collect(source)
        return items

    async def collect(
        self,
        source: Source,
    ) -> tuple[list[NormalizedContentItem], int, DiscoverPage]:
        """Return normalized items, the skip count, and the discovery page.

        One broken item is logged and counted. A failed request, or a document
        that cannot be parsed, still raises. A 304 returns no items and skips none.
        """

        page = await self.discover(source)
        items: list[NormalizedContentItem] = []
        skipped = 0
        for raw in page.items:
            try:
                items.append(await self.normalize(await self.fetch_detail(raw)))
            except ValidationError as exc:
                skipped += 1
                label = getattr(raw, "title", None) or getattr(raw, "link", None) or "<untitled>"
                logger.warning("skipped %s entry %r: %s", source.id, label, exc)
        return items, skipped, page
