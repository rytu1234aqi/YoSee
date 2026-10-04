"""Configured sources through load, fetch, and the database."""

import asyncio
from pathlib import Path

import httpx
import pytest
from server.database.base import Base
from server.database.session import create_db_engine, create_session_factory
from server.ingestion.cli import main
from server.ingestion.fetcher import Fetcher
from server.ingestion.pipeline import IngestionPipeline
from server.ingestion.providers.platforms.stackexchange import questions_url
from server.ingestion.sources import load_source_configs, upsert_sources
from server.models import ContentItem, FetchRun, FetchStatus, Source
from sqlalchemy import select
from sqlalchemy.orm import Session

_ROOT = Path(__file__).parent
_SOURCES = _ROOT.parent / "configs" / "sources"
_FEEDS = _ROOT / "fixtures" / "feeds"
_API = questions_url(site="gamedev", sort="activity", pagesize=50, page=1)
_LIST = "https://lostgarden.com/blog/"
_BODIES = {
    "https://www.redblobgames.com/blog/posts.xml": (_FEEDS / "sample.rss").read_bytes(),
    "https://godotengine.org/rss.xml": (_FEEDS / "godot.rss").read_bytes(),
    "https://www.gamedeveloper.com/rss.xml": (_FEEDS / "gamedeveloper.rss").read_bytes(),
    _API: (_ROOT / "fixtures" / "stackexchange" / "questions.json").read_bytes(),
    _LIST: (_ROOT / "fixtures" / "html" / "lostgarden.html").read_bytes(),
}


def test_checked_in_sources_cover_rss_and_the_api() -> None:
    configs = load_source_configs(_SOURCES)
    rss = [config for config in configs if config.provider_type == "rss"]
    api = [config for config in configs if config.provider_type == "stackexchange"]

    assert [config.id for config in configs] == [
        "gamedev-stackexchange",
        "gamedeveloper",
        "godot",
        "lostgarden",
        "redblobgames",
    ]
    html = [config for config in configs if config.provider_type == "html"]
    assert {config.config["feed_url"] for config in rss} == set(_BODIES) - {_API, _LIST}
    assert [config.config["list"]["url"] for config in html] == [_LIST]
    assert [(config.id, config.config["site"]) for config in api] == [
        ("gamedev-stackexchange", "gamedev"),
    ]


def _fetcher(
    *,
    fail: str | None = None,
    requested: list[str] | None = None,
) -> Fetcher:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if requested is not None:
            requested.append(url)
        if url == fail:
            return httpx.Response(404, content=b"missing")
        body = _BODIES.get(url)
        if body is None:
            return httpx.Response(404, content=b"unknown")
        return httpx.Response(200, content=body, headers={"etag": f'"{url}"'})

    return Fetcher(transport=httpx.MockTransport(handler))


def _load(session: Session) -> None:
    upsert_sources(session, load_source_configs(_SOURCES))


def test_fetch_enabled_persists_items_from_every_source(session: Session) -> None:
    _load(session)

    results = asyncio.run(IngestionPipeline(session, _fetcher()).fetch_enabled())
    stored = session.scalars(select(ContentItem)).all()
    runs = session.scalars(select(FetchRun)).all()

    assert [result.source_id for result in results] == [
        "gamedev-stackexchange",
        "gamedeveloper",
        "godot",
        "lostgarden",
        "redblobgames",
    ]
    assert all(result.ok for result in results)
    assert sum(result.discovered for result in results) == 7
    assert sum(result.inserted for result in results) == 7
    assert sum(result.skipped for result in results) == 4
    assert {item.source_id for item in stored} == {
        "gamedeveloper",
        "gamedev-stackexchange",
        "godot",
        "lostgarden",
        "redblobgames",
    }
    assert len(stored) == 7
    assert {run.status for run in runs} == {FetchStatus.SUCCESS}
    redblob = session.get(Source, "redblobgames")
    assert redblob is not None
    assert redblob.etag == '"https://www.redblobgames.com/blog/posts.xml"'


def test_one_failed_source_does_not_stop_the_others(session: Session) -> None:
    _load(session)

    fetcher = _fetcher(fail="https://godotengine.org/rss.xml")
    results = asyncio.run(IngestionPipeline(session, fetcher).fetch_enabled())
    by_id = {result.source_id: result for result in results}
    godot = session.get(Source, "godot")

    assert by_id["godot"].ok is False
    assert by_id["redblobgames"].inserted == 2
    assert by_id["gamedeveloper"].inserted == 1
    assert godot is not None
    assert godot.consecutive_failures == 1
    assert by_id["gamedev-stackexchange"].inserted == 1
    assert by_id["lostgarden"].inserted == 2
    assert {item.source_id for item in session.scalars(select(ContentItem))} == {
        "gamedeveloper",
        "gamedev-stackexchange",
        "lostgarden",
        "redblobgames",
    }


def test_disabled_source_is_not_fetched(session: Session) -> None:
    _load(session)
    source = session.get(Source, "godot")
    assert source is not None
    source.enabled = False
    requested: list[str] = []

    results = asyncio.run(IngestionPipeline(session, _fetcher(requested=requested)).fetch_enabled())

    assert [result.source_id for result in results] == [
        "gamedev-stackexchange",
        "gamedeveloper",
        "lostgarden",
        "redblobgames",
    ]
    assert "https://godotengine.org/rss.xml" not in requested


def test_cli_fetch_all_commits_every_configured_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "gamedev.sqlite"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database}")
    engine = create_db_engine(f"sqlite:///{database}")
    Base.metadata.create_all(engine)
    engine.dispose()

    assert main(["load-sources", "--dir", str(_SOURCES)]) == 0
    capsys.readouterr()
    exit_code = main(["fetch-all"], fetcher=_fetcher())
    printed = capsys.readouterr().out

    assert exit_code == 0
    assert "Fetching Game Developer..." in printed
    assert "Fetching Game Development Stack Exchange..." in printed
    assert "Fetching Godot Engine..." in printed
    assert "Fetching Lost Garden..." in printed
    assert "Fetching Red Blob Games..." in printed
    assert "5 sources fetched" in printed
    assert "7 items discovered" in printed
    assert "7 items inserted" in printed
    assert "0 fatal errors" in printed

    check = create_db_engine(f"sqlite:///{database}")
    try:
        with create_session_factory(check)() as session:
            assert len(session.scalars(select(ContentItem)).all()) == 7
    finally:
        check.dispose()
