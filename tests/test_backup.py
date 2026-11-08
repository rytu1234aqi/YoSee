"""The daily backup is a thin, human-readable snapshot, not a database copy."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from server.api import create_app
from server.backup import FORMAT, BackupManager, default_backup_dir
from server.database.base import Base
from server.database.session import create_db_engine, create_session_factory
from server.models import ContentItem, ContentType, ReadingState, Source
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

FIXED_NOW = datetime(2026, 11, 8, 3, 0, tzinfo=UTC)  # a Sunday


@pytest.fixture
def factory() -> Iterator[sessionmaker[Session]]:
    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)
    try:
        yield create_session_factory(engine)
    finally:
        engine.dispose()


def seed(factory: sessionmaker[Session], *, saved: bool = True) -> None:
    with factory() as session:
        source = Source(
            id="site",
            name="Example",
            slug="site",
            provider_type="rss",
            homepage_url="https://example.com/",
            fetch_interval_seconds=3600,
            config_json={"feed_url": "https://example.com/feed.xml"},
            etag="stale-validator",
            consecutive_failures=3,
            last_error="upstream down",
        )
        session.add(source)
        session.flush()
        item = ContentItem(
            source_id=source.id,
            external_id="a1",
            content_type=ContentType.ARTICLE,
            title="A post",
            canonical_url="https://example.com/a",
            url_hash="a" * 64,
            fetched_at=FIXED_NOW,
            content_html="<p>full body</p>",
            content_text="full body",
        )
        session.add(item)
        session.flush()
        session.add(
            ReadingState(
                content_id=item.id,
                is_read=True,
                saved=saved,
                updated_at=FIXED_NOW,
            )
        )
        session.commit()


def make_manager(
    factory: sessionmaker[Session], path: Path, **kwargs: object
) -> BackupManager:
    return BackupManager(factory, path, now=lambda: FIXED_NOW, **kwargs)  # type: ignore[arg-type]


def test_backup_dir_sits_next_to_the_database(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'reader.sqlite'}"
    assert default_backup_dir(url) == tmp_path / "backups"
    assert default_backup_dir("sqlite://") is None
    assert default_backup_dir("sqlite:///:memory:") is None


def test_payload_keeps_only_three_sections(factory: sessionmaker[Session], tmp_path: Path) -> None:
    seed(factory)
    payload = make_manager(factory, tmp_path).build_payload()

    assert set(payload) == {
        "format", "version", "generated_at", "counts", "sources", "links", "saved_articles",
    }
    assert payload["format"] == FORMAT
    assert payload["counts"] == {"sources": 1, "links": 1, "saved": 1}

    source = payload["sources"][0]
    assert "etag" not in source
    assert "consecutive_failures" not in source
    assert "last_error" not in source
    assert "last_fetched_at" not in source
    assert source["config_json"] == {"feed_url": "https://example.com/feed.xml"}

    link = payload["links"][0]
    assert set(link) == {"url", "title", "source_id", "published_at", "seen", "saved"}
    assert link["seen"] is True and link["saved"] is True
    assert "content_html" not in link
    assert payload["saved_articles"][0]["content_html"] == "<p>full body</p>"


def test_unsaved_article_has_no_body_in_backup(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    seed(factory, saved=False)
    payload = make_manager(factory, tmp_path).build_payload()
    assert payload["counts"] == {"sources": 1, "links": 1, "saved": 0}
    assert payload["saved_articles"] == []
    assert payload["links"][0]["saved"] is False


def test_write_creates_daily_file_and_leaves_no_temp(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    seed(factory)
    target = make_manager(factory, tmp_path).write_backup()

    assert target.name == "daily-2026-11-08.json"
    parsed = json.loads(target.read_text(encoding="utf-8"))
    assert parsed["format"] == FORMAT
    assert parsed["counts"]["links"] == 1
    assert list(tmp_path.glob("*.tmp")) == []
    assert list(tmp_path.glob(".*.tmp")) == []


def test_sunday_also_writes_a_weekly_copy(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    make_manager(factory, tmp_path).write_backup()
    iso = FIXED_NOW.isocalendar()
    assert (tmp_path / "daily-2026-11-08.json").exists()
    assert (tmp_path / f"weekly-{iso.year}-W{iso.week:02d}.json").exists()


def test_prune_keeps_seven_daily_and_four_weekly_only(tmp_path: Path) -> None:
    for day in range(1, 11):
        (tmp_path / f"daily-2026-11-{day:02d}.json").write_text("{}", encoding="utf-8")
    for week in range(40, 47):
        (tmp_path / f"weekly-2026-W{week:02d}.json").write_text("{}", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("keep me", encoding="utf-8")

    manager = BackupManager(create_session_factory(create_db_engine("sqlite://")), tmp_path)
    manager.prune()

    assert len(list(tmp_path.glob("daily-*.json"))) == 7
    assert len(list(tmp_path.glob("weekly-*.json"))) == 4
    assert (tmp_path / "notes.txt").exists()
    assert not (tmp_path / "daily-2026-11-01.json").exists()
    assert (tmp_path / "daily-2026-11-10.json").exists()


def test_failure_is_recorded_and_never_raises(
    factory: sessionmaker[Session], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = make_manager(factory, tmp_path)

    def broken() -> dict[str, object]:
        raise OSError("disk full")

    monkeypatch.setattr(manager, "build_payload", broken)
    assert manager.run_once() is False
    assert manager.last_error == "disk full"
    assert manager.status()["last_error"] == "disk full"


def test_disabled_manager_does_not_write(factory: sessionmaker[Session], tmp_path: Path) -> None:
    manager = make_manager(factory, tmp_path, enabled=False)
    assert manager.run_once() is False
    assert list(tmp_path.glob("*.json")) == []
    assert manager.status()["enabled"] is False


def test_status_reports_latest_file(factory: sessionmaker[Session], tmp_path: Path) -> None:
    manager = make_manager(factory, tmp_path)
    manager.write_backup()
    status = manager.status()
    assert status["latest_file"] == "daily-2026-11-08.json"
    assert status["files"] == 1
    assert status["last_backup_at"] is not None


def test_endpoints_report_status_and_run_on_demand(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    url = f"sqlite:///{tmp_path / 'app.sqlite'}"
    seed_engine = create_engine(url)
    Base.metadata.create_all(seed_engine)
    seed_engine.dispose()
    seed(create_session_factory(create_engine(url)))

    with TestClient(
        create_app(url, auto_backup=True, backup_dir=tmp_path / "backups")
    ) as client:
        status = client.get("/backup/status").json()
        assert status["enabled"] is True
        assert status["format"] == FORMAT
        result = client.post("/backup/run").json()
        assert result["ok"] is True
        assert Path(result["path"]).exists()


def test_in_memory_database_disables_backup() -> None:
    with TestClient(create_app("sqlite://", auto_backup=True)) as client:
        assert client.get("/backup/status").json()["enabled"] is False
