"""One source fetched through the pipeline and the fetch-source command."""

import asyncio
from pathlib import Path

import httpx
import pytest
from server.database.base import Base
from server.database.session import create_db_engine, create_session_factory
from server.ingestion.cli import main
from server.ingestion.fetcher import Fetcher
from server.ingestion.pipeline import IngestionPipeline
from server.models import ContentItem, FetchRun, FetchStatus, Source
from sqlalchemy import select
from sqlalchemy.orm import Session

_FEED = Path(__file__).parent / "fixtures" / "feeds" / "sample.rss"
_FEED_URL = "https://www.redblobgames.com/feed.xml"


def _source(**overrides: object) -> Source:
    values: dict[str, object] = {
        "id": "redblobgames",
        "name": "Red Blob Games",
        "slug": "redblobgames",
        "provider_type": "rss",
        "fetch_interval_seconds": 21600,
        "config_json": {"feed_url": _FEED_URL},
    }
    values.update(overrides)
    return Source(**values)


def _fetcher(body: bytes, *, status: int = 200, headers: dict[str, str] | None = None) -> Fetcher:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=body, headers=headers)

    return Fetcher(transport=httpx.MockTransport(handler))


def _run(session: Session, fetcher: Fetcher, source_id: str = "redblobgames") -> object:
    pipeline = IngestionPipeline(session, fetcher)
    return asyncio.run(pipeline.fetch_source(source_id))


def test_fetch_source_inserts_items_and_skips_a_blank_title(session: Session) -> None:
    session.add(_source())
    session.flush()
    fetcher = _fetcher(
        _FEED.read_bytes(),
        headers={"etag": '"feed"', "last-modified": "Fri, 02 Jan 2026 00:00:00 GMT"},
    )

    result = _run(session, fetcher)
    stored = session.scalars(select(ContentItem).order_by(ContentItem.title)).all()
    run = session.scalars(select(FetchRun)).one()
    source = session.get(Source, "redblobgames")

    assert result.ok is True
    assert result.discovered == 2
    assert result.inserted == 2
    assert result.updated == 0
    assert result.skipped == 1
    assert [item.title for item in stored] == ["Hexagonal Grids", "Plain only"]
    assert run.status is FetchStatus.SUCCESS
    assert run.items_discovered == 2
    assert run.items_created == 2
    assert run.items_skipped == 1
    assert source is not None
    assert source.etag == '"feed"'
    assert source.last_modified == "Fri, 02 Jan 2026 00:00:00 GMT"
    assert source.consecutive_failures == 0
    assert source.last_success_at is not None


def test_fetching_again_updates_the_same_rows(session: Session) -> None:
    session.add(_source())
    session.flush()
    body = _FEED.read_bytes()
    first = _run(session, _fetcher(body))
    second = _run(session, _fetcher(body))

    assert first.inserted == 2
    assert second.discovered == 2
    assert second.inserted == 0
    assert second.updated == 2
    assert len(session.scalars(select(ContentItem)).all()) == 2
    assert len(session.scalars(select(FetchRun)).all()) == 2


def test_not_modified_keeps_the_stored_items(session: Session) -> None:
    session.add(_source())
    session.flush()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, content=_FEED.read_bytes(), headers={"etag": '"same"'})
        assert request.headers["if-none-match"] == '"same"'
        return httpx.Response(304, headers={"etag": '"same"'})

    fetcher = Fetcher(transport=httpx.MockTransport(handler))
    _run(session, fetcher)
    result = _run(session, fetcher)

    assert result.ok is True
    assert result.discovered == 0
    assert result.inserted == 0
    assert result.updated == 0
    assert len(session.scalars(select(ContentItem)).all()) == 2


def test_http_failure_is_recorded_and_does_not_insert(session: Session) -> None:
    session.add(_source())
    session.flush()

    result = _run(session, _fetcher(b"missing", status=404))
    source = session.get(Source, "redblobgames")
    run = session.scalars(select(FetchRun)).one()

    assert result.ok is False
    assert result.error is not None
    assert "404" in result.error
    assert session.scalars(select(ContentItem)).all() == []
    assert run.status is FetchStatus.FAILED
    assert run.error_type == "FetchError"
    assert source is not None
    assert source.consecutive_failures == 1
    assert source.last_error == result.error
    assert source.last_success_at is None


def test_unknown_source_and_unsupported_provider_fail_cleanly(session: Session) -> None:
    missing = _run(session, _fetcher(b""), "missing")
    session.add(_source(provider_type="sitemap"))
    session.flush()
    unsupported = _run(session, _fetcher(b"should-not-be-requested"))

    assert missing.ok is False
    assert missing.error == "source missing was not found"
    assert unsupported.ok is False
    assert unsupported.error == "unsupported provider sitemap"
    assert session.scalars(select(FetchRun)).one().error_type == "ProviderError"
    assert session.scalars(select(ContentItem)).all() == []


def test_cli_fetch_source_commits_and_prints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "gamedev.sqlite"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database}")
    engine = create_db_engine(f"sqlite:///{database}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    with factory() as session:
        session.add(_source())
        session.commit()
    engine.dispose()

    exit_code = main(
        ["fetch-source", "redblobgames"],
        fetcher=_fetcher(_FEED.read_bytes()),
    )
    printed = capsys.readouterr().out

    assert exit_code == 0
    assert "Fetching Red Blob Games..." in printed
    assert "2 discovered" in printed
    assert "2 new" in printed
    assert "0 duplicated" in printed
    assert "1 skipped" in printed

    check = create_db_engine(f"sqlite:///{database}")
    try:
        with create_session_factory(check)() as session:
            assert len(session.scalars(select(ContentItem)).all()) == 2
    finally:
        check.dispose()
