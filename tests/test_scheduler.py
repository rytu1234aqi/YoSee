"""Exercise due times, isolation and shutdown without external requests or sleeps."""

import asyncio
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from server.api import create_app
from server.database.base import Base
from server.database.session import create_db_engine, create_session_factory
from server.ingestion.fetcher import Fetcher
from server.ingestion.scheduler import FetchScheduler, next_fetch_at
from server.models import ContentItem, FetchRun, FetchStatus, Source
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

FEED = (Path(__file__).parent / "fixtures/feeds/sample.rss").read_bytes()


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'scheduler.sqlite'}")
    Base.metadata.create_all(engine)
    yield create_session_factory(engine)
    engine.dispose()


def source(source_id: str, **overrides: object) -> Source:
    values: dict[str, object] = dict(
        id=source_id,
        name=source_id,
        slug=source_id,
        provider_type="rss",
        config_json={"feed_url": f"https://example.test/{source_id}"},
        fetch_interval_seconds=60,
        consecutive_failures=0,
    )
    values.update(overrides)
    return Source(**values)


def test_only_due_enabled_sources_are_fetched(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        session.add_all(
            [
                source("never"),
                source("due", last_fetched_at=datetime.now(UTC) - timedelta(minutes=2)),
                source("recent", last_fetched_at=datetime.now(UTC)),
                source("disabled", enabled=False),
            ]
        )
        session.commit()
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        return httpx.Response(200, content=FEED)

    scheduler = FetchScheduler(
        factory, fetcher_factory=lambda: Fetcher(transport=httpx.MockTransport(handler))
    )

    async def exercise() -> None:
        first, second = await asyncio.gather(scheduler.run_once(), scheduler.run_once())
        assert len(first) == 2
        assert second == []

    asyncio.run(exercise())
    assert requested == ["/due", "/never"]
    with factory() as session:
        runs = session.scalars(select(FetchRun)).all()
        assert len(runs) == 2
        assert all(run.status == FetchStatus.SUCCESS for run in runs)
        assert all(run.finished_at >= run.started_at for run in runs)


@pytest.mark.parametrize("unexpected", [False, True])
def test_failure_is_persisted_and_next_source_continues(
    factory: sessionmaker[Session],
    unexpected: bool,
) -> None:
    with factory() as session:
        session.add_all([source("a-failed"), source("b-good")])
        session.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/a-failed":
            if unexpected:
                raise RuntimeError("bad parser")
            return httpx.Response(404)
        return httpx.Response(200, content=FEED)

    scheduler = FetchScheduler(
        factory, fetcher_factory=lambda: Fetcher(transport=httpx.MockTransport(handler))
    )
    asyncio.run(scheduler.run_once())
    with factory() as session:
        failed = session.get(Source, "a-failed")
        assert failed is not None
        assert failed.consecutive_failures == 1
        assert failed.last_error
        assert next_fetch_at(failed) > datetime.now(UTC)
        runs = session.scalars(select(FetchRun).order_by(FetchRun.source_id)).all()
        assert [run.status for run in runs] == [FetchStatus.FAILED, FetchStatus.SUCCESS]
        assert session.scalars(select(ContentItem)).all()


def test_naive_utc_and_failure_backoff() -> None:
    start = datetime(2026, 10, 2)
    row = source("backoff", last_fetched_at=start, consecutive_failures=3)
    assert next_fetch_at(row) == start.replace(tzinfo=UTC) + timedelta(seconds=240)
    row.fetch_interval_seconds = 21600
    row.consecutive_failures = 100
    assert next_fetch_at(row) == start.replace(tzinfo=UTC) + timedelta(days=1)


def test_fetch_does_not_block_writes_and_shutdown_cancels_request(
    factory: sessionmaker[Session],
) -> None:
    with factory() as session:
        session.add(source("slow"))
        session.commit()

    async def exercise() -> None:
        requested = asyncio.Event()
        cancelled = asyncio.Event()

        async def handler(request: httpx.Request) -> httpx.Response:
            requested.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
            return httpx.Response(200, content=FEED)

        scheduler = FetchScheduler(
            factory, fetcher_factory=lambda: Fetcher(transport=httpx.MockTransport(handler))
        )
        scheduler.start()
        try:
            await asyncio.wait_for(requested.wait(), timeout=2)
            with factory() as session:
                session.add(source("other", enabled=False))
                session.commit()  # Must succeed while the HTTP request is pending.
        finally:
            await asyncio.wait_for(scheduler.stop(), timeout=2)
        assert cancelled.is_set()
        assert scheduler.current_source is None

    asyncio.run(exercise())
    with factory() as session:
        assert session.get(Source, "other") is not None
        assert session.scalars(select(FetchRun)).all() == []


@pytest.mark.parametrize("interval", [0, -1, float("nan"), float("inf")])
def test_invalid_poll_interval_is_rejected(
    factory: sessionmaker[Session],
    interval: float,
) -> None:
    with pytest.raises(ValueError, match="positive"):
        FetchScheduler(factory, poll_seconds=interval)


def test_application_owns_scheduler_lifetime(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'lifetime.sqlite'}"
    engine = create_db_engine(url)
    Base.metadata.create_all(engine)
    with create_session_factory(engine)() as session:
        session.add(source("waiting"))
        session.commit()
    engine.dispose()
    requested = threading.Event()
    cancelled = threading.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        return httpx.Response(304)

    app = create_app(
        url,
        auto_fetch=True,
        fetcher_factory=lambda: Fetcher(transport=httpx.MockTransport(handler)),
    )
    with TestClient(app) as client:
        assert requested.wait(timeout=2)
        status = client.get("/sync/status").json()
        assert status["enabled"] is True
        assert status["running"] is True
        assert status["current_source"] == "waiting"
        assert client.get("/health").status_code == 200
    assert cancelled.wait(timeout=2)
