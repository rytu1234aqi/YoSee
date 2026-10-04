"""Generic document providers. Site-specific adapters are not here."""

from server.ingestion.providers.generic.rss import RSSProvider

__all__ = ["RSSProvider"]
