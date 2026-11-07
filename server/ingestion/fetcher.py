"""HTTP client for ingestion.

Providers parse bytes. This module is the only place that sends requests, retries,
and attaches cache validators. It does not write the database.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Self
from urllib.parse import urljoin

import httpx

from server.ingestion.errors import FetchError, IngestionError, RateLimitError
from server.security import UnsafeUrlError, validate_public_url

DEFAULT_USER_AGENT = "GameDevFeed/0.0 (personal content reader)"
DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_MAX_RETRIES = 3
MAX_REDIRECTS = 5
MAX_RESPONSE_BYTES = 8 * 1024 * 1024

# 404, parser errors, and validation errors are not retried. 429 is handled
# separately so a refusal does not become three extra hits.
_RETRYABLE_STATUS = frozenset({408, 425, 500, 502, 503, 504})
_REDIRECT_STATUS = frozenset({301, 302, 303, 307, 308})


@dataclass(frozen=True, slots=True)
class FetchResponse:
    """One completed HTTP response, including a 304.

    ``not_modified`` means the caller should skip parsing and keep the stored copy.
    ``etag`` and ``last_modified`` are the validators to save for the next request.
    """

    url: str
    status_code: int
    headers: Mapping[str, str]
    body: bytes
    text: str
    etag: str | None
    last_modified: str | None
    not_modified: bool


class Fetcher:
    """GET a URL with timeout, retries, cache headers, and a per-run request budget.

    ``max_requests_per_run`` counts ``get`` calls, not retry attempts. The wait
    between calls is ``min_request_interval`` seconds. Retries use their own
    backoff of 1s, 3s, then 9s.
    """

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        user_agent: str = DEFAULT_USER_AGENT,
        min_request_interval: float = 0.0,
        max_requests_per_run: int | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if max_requests_per_run is not None and max_requests_per_run < 0:
            raise ValueError("max_requests_per_run must be zero or positive")
        self._client = client
        self._owns_client = client is None
        self._transport = transport
        self._timeout = timeout
        self._max_retries = max(0, max_retries)
        self._user_agent = user_agent
        self._min_request_interval = max(0.0, min_request_interval)
        self._max_requests_per_run = max_requests_per_run
        self._sleep = sleep or _default_sleep
        self._monotonic = monotonic or _default_monotonic
        # A caller-supplied transport/client is used by tests and by offline
        # replay; public-host checks only apply to real network requests.
        self._check_public_host = transport is None and client is None
        self._sent = 0
        self._last_finished_at: float | None = None

    async def __aenter__(self) -> Self:
        if self._client is None:
            self._client = self._build_client()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def get(
        self,
        url: str,
        headers: Mapping[str, str] | None = None,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> FetchResponse:
        """GET ``url``. Pass the source cache validators to send conditional headers."""

        await self._reserve_request(url)
        try:
            if self._client is not None:
                return await self._get_with_redirects(
                    self._client, url, headers, etag, last_modified
                )
            async with self._build_client() as client:
                return await self._get_with_redirects(client, url, headers, etag, last_modified)
        finally:
            self._last_finished_at = self._monotonic()

    async def _get_with_redirects(
        self,
        client: httpx.AsyncClient,
        url: str,
        headers: Mapping[str, str] | None,
        etag: str | None,
        last_modified: str | None,
    ) -> FetchResponse:
        """Follow redirects manually so every hop is validated before the hop.

        ``follow_redirects=True`` would let a public URL bounce into the local
        network or a cloud metadata endpoint without another check.
        """

        current = url
        for hop in range(MAX_REDIRECTS + 1):
            self._assert_public(current)
            response = await self._send(client, current, headers, etag, last_modified)
            location = response.headers.get("location")
            if response.status_code not in _REDIRECT_STATUS or not location:
                return response
            if hop >= MAX_REDIRECTS:
                raise FetchError(f"GET {url} exceeded {MAX_REDIRECTS} redirects", url=url)
            current = urljoin(current, location)
        raise FetchError(f"GET {url} exceeded {MAX_REDIRECTS} redirects", url=url)

    def _assert_public(self, url: str) -> None:
        if not self._check_public_host:
            return
        try:
            validate_public_url(url, resolve_dns=True)
        except UnsafeUrlError as exc:
            raise FetchError(f"blocked URL target: {exc}", url=url) from exc

    def _build_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=self._transport,
            timeout=httpx.Timeout(self._timeout),
            follow_redirects=False,
        )

    async def _reserve_request(self, url: str) -> None:
        if self._max_requests_per_run is not None and self._sent >= self._max_requests_per_run:
            raise RateLimitError(
                f"request limit of {self._max_requests_per_run} reached for {url}",
                url=url,
            )
        await self._wait_for_interval()
        self._sent += 1

    async def _wait_for_interval(self) -> None:
        if self._min_request_interval <= 0 or self._last_finished_at is None:
            return
        remaining = self._min_request_interval - (self._monotonic() - self._last_finished_at)
        if remaining > 0:
            await self._sleep(remaining)

    async def _send(
        self,
        client: httpx.AsyncClient,
        url: str,
        headers: Mapping[str, str] | None,
        etag: str | None,
        last_modified: str | None,
    ) -> FetchResponse:
        request_headers = self._request_headers(headers, etag, last_modified)
        for attempt in range(self._max_retries + 1):
            try:
                response = await client.get(url, headers=request_headers)
            except httpx.UnsupportedProtocol as exc:
                raise FetchError(f"GET {url} failed: {exc}", url=url) from exc
            except httpx.TransportError as exc:
                if attempt >= self._max_retries:
                    raise FetchError(f"GET {url} failed: {exc}", url=url) from exc
                await self._sleep(_backoff(attempt))
                continue

            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES:
                raise FetchError(
                    f"GET {url} response exceeded {MAX_RESPONSE_BYTES} bytes", url=url
                )
            if response.status_code == 304 or response.status_code < 400:
                return _to_response(response)
            if response.status_code in _RETRYABLE_STATUS and attempt < self._max_retries:
                await self._sleep(_backoff(attempt))
                continue
            raise _http_error(url, response.status_code)
        raise FetchError(f"GET {url} failed", url=url)

    def _request_headers(
        self,
        headers: Mapping[str, str] | None,
        etag: str | None,
        last_modified: str | None,
    ) -> dict[str, str]:
        merged = {"User-Agent": self._user_agent, "Accept": "*/*"}
        if etag:
            merged["If-None-Match"] = etag
        if last_modified:
            merged["If-Modified-Since"] = last_modified
        if headers:
            merged.update(headers)
        return merged


def _backoff(attempt: int) -> float:
    """Return the delay before the next try: 1s, 3s, 9s."""

    return float(3**attempt)


def _to_response(response: httpx.Response) -> FetchResponse:
    headers = {key.lower(): value for key, value in response.headers.items()}
    body = response.content
    if len(body) > MAX_RESPONSE_BYTES:
        raise FetchError(
            f"GET {response.url} response exceeded {MAX_RESPONSE_BYTES} bytes",
            url=str(response.url),
        )
    return FetchResponse(
        url=str(response.url),
        status_code=response.status_code,
        headers=headers,
        body=body,
        text=response.text,
        etag=headers.get("etag"),
        last_modified=headers.get("last-modified"),
        not_modified=response.status_code == 304,
    )


def _http_error(url: str, status_code: int) -> IngestionError:
    if status_code == 429:
        return RateLimitError(
            f"GET {url} was rate limited",
            url=url,
            status_code=status_code,
        )
    return FetchError(
        f"GET {url} failed with HTTP {status_code}",
        url=url,
        status_code=status_code,
    )


async def _default_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


def _default_monotonic() -> float:
    return time.monotonic()
