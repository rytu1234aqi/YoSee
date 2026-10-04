"""Failures of one ingestion step.

Callers record these and continue with the next source. A single fetch or parse
failure must not stop the run.
"""


class IngestionError(Exception):
    """Base error for the ingestion pipeline."""


class FetchError(IngestionError):
    """A request failed and should not be treated as content."""

    def __init__(self, message: str, *, url: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.url = url
        self.status_code = status_code


class RateLimitError(IngestionError):
    """The local request budget was spent, or the server returned HTTP 429."""

    def __init__(self, message: str, *, url: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.url = url
        self.status_code = status_code


class ParseError(IngestionError):
    """A document was fetched but is not a readable feed."""

    def __init__(self, message: str, *, url: str) -> None:
        super().__init__(message)
        self.url = url


class ProviderError(IngestionError):
    """A source is misconfigured, so it cannot be loaded or fetched."""

    def __init__(self, message: str, *, source_id: str) -> None:
        super().__init__(message)
        self.source_id = source_id
