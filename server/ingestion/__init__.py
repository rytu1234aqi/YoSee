"""Ingestion pipeline package.

Providers discover and normalize external content. They must not write to the database.
"""

from server.ingestion.dedup import DedupAction, DedupDecision, DedupKey, DedupService, MatchOn
from server.ingestion.errors import FetchError, ParseError, ProviderError, RateLimitError
from server.ingestion.fetcher import Fetcher, FetchResponse
from server.ingestion.normalizer import NormalizedContentItem
from server.ingestion.pipeline import FetchResult, IngestionPipeline
from server.ingestion.providers.base import DiscoverPage, SourceAdapter
from server.ingestion.providers.generic.html import GenericHTMLProvider
from server.ingestion.providers.generic.rss import RSSProvider
from server.ingestion.providers.platforms.stackexchange import StackExchangeProvider
from server.ingestion.sources import SourceConfig, SourceConfigError

__all__ = [
    "DedupAction",
    "DedupDecision",
    "DedupKey",
    "DedupService",
    "DiscoverPage",
    "FetchError",
    "FetchResponse",
    "FetchResult",
    "Fetcher",
    "GenericHTMLProvider",
    "IngestionPipeline",
    "MatchOn",
    "NormalizedContentItem",
    "ParseError",
    "ProviderError",
    "RSSProvider",
    "RateLimitError",
    "StackExchangeProvider",
    "SourceAdapter",
    "SourceConfig",
    "SourceConfigError",
]
