"""Source adapters. Concrete providers live in subpackages such as ``generic``."""

from server.ingestion.errors import ProviderError
from server.ingestion.fetcher import Fetcher
from server.ingestion.providers.base import DiscoverPage, SourceAdapter
from server.ingestion.providers.generic.html import GenericHTMLProvider
from server.ingestion.providers.generic.rss import RSSProvider
from server.ingestion.providers.platforms.stackexchange import StackExchangeProvider

__all__ = ["DiscoverPage", "SourceAdapter", "build_provider"]


def build_provider(provider_type: str, fetcher: Fetcher, *, source_id: str) -> SourceAdapter:
    """Return the adapter for one source. Unknown types raise ``ProviderError``."""

    if provider_type == "rss":
        return RSSProvider(fetcher)
    if provider_type == "html":
        return GenericHTMLProvider(fetcher)
    if provider_type == "stackexchange":
        return StackExchangeProvider(fetcher)
    raise ProviderError(f"unsupported provider {provider_type}", source_id=source_id)
