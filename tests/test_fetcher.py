"""Fetcher timeout, headers, retries, and HTTP cache validators."""

import asyncio
from collections.abc import Awaitable, Callable

import httpx
import pytest
from server.ingestion import Fetcher, FetchError, RateLimitError
from server.ingestion.fetcher import DEFAULT_USER_AGENT


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.delays: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.delays.append(seconds)
        self.now += seconds


def _run[T](fetcher: Fetcher, coro_factory: Callable[[Fetcher], Awaitable[T]]) -> T:
    async def _inner() -> T:
        async with fetcher:
            return await coro_factory(fetcher)

    return asyncio.run(_inner())


def test_get_sends_user_agent_and_returns_the_body() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            text="feed",
            headers={"etag": '"abc"', "last-modified": "Mon, 01 Jun 2026 00:00:00 GMT"},
        )

    fetcher = Fetcher(transport=httpx.MockTransport(handler), timeout=7)
    response = _run(fetcher, lambda current: current.get("https://example.com/atom.xml"))

    assert response.status_code == 200
    assert response.text == "feed"
    assert response.body == b"feed"
    assert response.etag == '"abc"'
    assert response.last_modified == "Mon, 01 Jun 2026 00:00:00 GMT"
    assert response.not_modified is False
    assert seen[0].headers["user-agent"] == DEFAULT_USER_AGENT
    assert "gzip" in seen[0].headers["accept-encoding"]
    assert fetcher._client is None


def test_conditional_headers_and_not_modified() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["if-none-match"] == '"abc"'
        assert request.headers["if-modified-since"] == "Mon, 01 Jun 2026 00:00:00 GMT"
        assert request.headers["x-source"] == "redblobgames"
        return httpx.Response(304, headers={"etag": '"abc"'})

    fetcher = Fetcher(transport=httpx.MockTransport(handler))
    response = _run(
        fetcher,
        lambda current: current.get(
            "https://example.com/atom.xml",
            {"X-Source": "redblobgames"},
            etag='"abc"',
            last_modified="Mon, 01 Jun 2026 00:00:00 GMT",
        ),
    )

    assert response.not_modified is True
    assert response.status_code == 304
    assert response.body == b""
    assert response.etag == '"abc"'


def test_missing_page_is_not_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(404, text="missing")

    clock = _Clock()
    fetcher = Fetcher(
        transport=httpx.MockTransport(handler),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    with pytest.raises(FetchError) as caught:
        _run(fetcher, lambda current: current.get("https://example.com/missing"))

    assert calls == 1
    assert clock.delays == []
    assert caught.value.status_code == 404
    assert caught.value.url == "https://example.com/missing"


def test_server_error_retries_then_returns_the_document() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(500, text="down")
        return httpx.Response(200, text="back")

    clock = _Clock()
    fetcher = Fetcher(
        transport=httpx.MockTransport(handler),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    response = _run(fetcher, lambda current: current.get("https://example.com/atom.xml"))

    assert calls == 2
    assert clock.delays == [1.0]
    assert response.text == "back"


def test_network_error_stops_after_three_retries() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("connection refused")

    clock = _Clock()
    fetcher = Fetcher(
        transport=httpx.MockTransport(handler),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    with pytest.raises(FetchError) as caught:
        _run(fetcher, lambda current: current.get("https://example.com/atom.xml"))

    assert calls == 4
    assert clock.delays == [1.0, 3.0, 9.0]
    assert caught.value.status_code is None


def test_remote_rate_limit_is_not_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, text="slow down")

    clock = _Clock()
    fetcher = Fetcher(
        transport=httpx.MockTransport(handler),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    with pytest.raises(RateLimitError) as caught:
        _run(fetcher, lambda current: current.get("https://example.com/atom.xml"))

    assert calls == 1
    assert clock.delays == []
    assert caught.value.status_code == 429


def test_request_budget_and_interval_are_enforced() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, text="ok")

    clock = _Clock()
    fetcher = Fetcher(
        transport=httpx.MockTransport(handler),
        min_request_interval=2.5,
        max_requests_per_run=2,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    async def _scenario(current: Fetcher) -> None:
        await current.get("https://example.com/one")
        await current.get("https://example.com/two")
        with pytest.raises(RateLimitError):
            await current.get("https://example.com/three")

    _run(fetcher, _scenario)

    assert calls == 2
    assert clock.delays == [2.5]
