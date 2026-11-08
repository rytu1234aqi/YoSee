"""Cleanup deletes only old, read, unsaved links; saved/unread are never touched."""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from server.cleanup import (
    DEFAULT_INTERVAL_SECONDS,
    KEEP_RECENT,
    MIN_AGE_DAYS,
    CleanupManager,
    utc_now,
)
from server.database.base import Base
from server.database.session import create_db_engine, create_session_factory
from server.models import ContentItem, ContentType, ReadingState, Source
from sqlalchemy.orm import Session, sessionmaker

FIXED_NOW = datetime(2026, 11, 8, 3, 0, tzinfo=UTC)
# FIXED_NOW - 30 days == 2026-10-09; anything older than that is a candidate.
OLD = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
NEWER_OLD = datetime(2026, 6, 2, 0, 0, tzinfo=UTC)
OLDER_OLD = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)
RECENT = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
NAIVE_OLD = datetime(2026, 1, 1, 0, 0)  # no tzinfo, on purpose


@dataclass(frozen=True)
class ItemSpec:
    key: str
    published_at: datetime | None
    is_read: bool = False
    saved: bool = False
    has_state: bool = True


@pytest.fixture
def factory() -> Iterator[sessionmaker[Session]]:
    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)
    try:
        yield create_session_factory(engine)
    finally:
        engine.dispose()


def seed(factory: sessionmaker[Session], specs: list[ItemSpec]) -> dict[str, str]:
    """Build a Source, its ContentItems and (optionally) their ReadingStates."""

    ids: dict[str, str] = {}
    with factory() as session:
        session.add(
            Source(
                id="site",
                name="Site",
                slug="site",
                provider_type="rss",
                homepage_url="https://example.com/",
                fetch_interval_seconds=3600,
                config_json={"feed_url": "https://example.com/feed.xml"},
            )
        )
        session.flush()
        for spec in specs:
            item = ContentItem(
                source_id="site",
                external_id=spec.key,
                content_type=ContentType.ARTICLE,
                title=spec.key,
                canonical_url=f"https://example.com/{spec.key}",
                url_hash=hashlib.sha256(spec.key.encode()).hexdigest(),
                fetched_at=FIXED_NOW,
                published_at=spec.published_at,
            )
            session.add(item)
            session.flush()
            if spec.has_state:
                session.add(
                    ReadingState(
                        content_id=item.id,
                        is_read=spec.is_read,
                        saved=spec.saved,
                        updated_at=FIXED_NOW,
                    )
                )
            ids[spec.key] = item.id
        session.commit()
    return ids


def item_exists(factory: sessionmaker[Session], item_id: str) -> bool:
    with factory() as session:
        return session.get(ContentItem, item_id) is not None


def state_exists(factory: sessionmaker[Session], content_id: str) -> bool:
    with factory() as session:
        return session.get(ReadingState, content_id) is not None


def make_manager(factory: sessionmaker[Session], **kwargs: object) -> CleanupManager:
    return CleanupManager(factory, now=lambda: FIXED_NOW, **kwargs)


def test_constants_and_utc_now() -> None:
    assert KEEP_RECENT == 200
    assert MIN_AGE_DAYS == 30
    assert DEFAULT_INTERVAL_SECONDS == 24 * 60 * 60
    assert utc_now().tzinfo is not None


def test_protected_zone_is_never_deleted(factory: sessionmaker[Session]) -> None:
    ids = seed(
        factory,
        [
            ItemSpec("newest", NEWER_OLD, is_read=True, saved=False),
            ItemSpec("oldest", OLDER_OLD, is_read=True, saved=False),
        ],
    )
    manager = make_manager(factory, keep_recent=1)

    # Both are read/unsaved/old, but the top-1 by published_at is protected.
    assert manager.select_ids() == [ids["oldest"]]
    assert manager.run_once() is True
    assert item_exists(factory, ids["newest"]) is True
    assert item_exists(factory, ids["oldest"]) is False


def test_saved_is_never_deleted(factory: sessionmaker[Session]) -> None:
    ids = seed(factory, [ItemSpec("saved", OLD, is_read=True, saved=True)])
    manager = make_manager(factory, keep_recent=0)

    assert manager.select_ids() == []
    assert manager.run_once() is True
    assert manager.status()["last_deleted"] == 0
    assert item_exists(factory, ids["saved"]) is True


def test_unread_and_missing_state_are_kept(factory: sessionmaker[Session]) -> None:
    ids = seed(
        factory,
        [
            ItemSpec("unread", OLD, is_read=False, saved=False),
            ItemSpec("no_state", OLD, has_state=False),
        ],
    )
    manager = make_manager(factory, keep_recent=0)

    assert manager.select_ids() == []
    assert manager.run_once() is True
    assert item_exists(factory, ids["unread"]) is True
    assert item_exists(factory, ids["no_state"]) is True


def test_null_published_at_is_kept(factory: sessionmaker[Session]) -> None:
    ids = seed(factory, [ItemSpec("null_pub", None, is_read=True, saved=False)])
    manager = make_manager(factory, keep_recent=0)

    assert manager.select_ids() == []
    assert manager.run_once() is True
    assert item_exists(factory, ids["null_pub"]) is True


def test_recent_published_at_is_kept(factory: sessionmaker[Session]) -> None:
    ids = seed(factory, [ItemSpec("recent", RECENT, is_read=True, saved=False)])
    manager = make_manager(factory, keep_recent=0)

    assert manager.select_ids() == []
    assert manager.run_once() is True
    assert item_exists(factory, ids["recent"]) is True


def test_naive_published_at_is_treated_as_utc(factory: sessionmaker[Session]) -> None:
    ids = seed(factory, [ItemSpec("naive", NAIVE_OLD, is_read=True, saved=False)])
    manager = make_manager(factory, keep_recent=0)

    # Must not raise TypeError when comparing naive DB values with the cutoff.
    assert manager.select_ids() == [ids["naive"]]
    assert manager.run_once() is True
    assert item_exists(factory, ids["naive"]) is False


def test_oldest_read_link_is_deleted_and_state_cascades(
    factory: sessionmaker[Session],
) -> None:
    ids = seed(factory, [ItemSpec("doomed", OLD, is_read=True, saved=False)])
    manager = make_manager(factory, keep_recent=0)

    assert state_exists(factory, ids["doomed"]) is True
    assert manager.select_ids() == [ids["doomed"]]
    assert manager.run_once() is True
    assert manager.status()["last_deleted"] == 1
    assert item_exists(factory, ids["doomed"]) is False
    assert state_exists(factory, ids["doomed"]) is False


def test_status_fields(factory: sessionmaker[Session]) -> None:
    manager = make_manager(factory, keep_recent=7, min_age_days=14)
    status = manager.status()

    assert {
        "enabled",
        "keep_recent",
        "min_age_days",
        "last_run_at",
        "last_deleted",
        "last_error",
    } <= set(status)
    assert status["enabled"] is True
    assert status["keep_recent"] == 7
    assert status["min_age_days"] == 14
    assert status["last_run_at"] is None
    assert status["last_deleted"] == 0
    assert status["last_error"] is None

    # A successful run with zero deletions still completes and stamps the run.
    assert manager.run_once() is True
    after = manager.status()
    assert after["last_run_at"] is not None
    assert isinstance(after["last_run_at"], str)
    assert after["last_deleted"] == 0
    assert after["last_error"] is None


def test_failure_is_recorded_and_never_raises(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = make_manager(factory)

    def broken() -> list[str]:
        raise OSError("disk full")

    monkeypatch.setattr(manager, "select_ids", broken)
    assert manager.run_once() is False
    assert manager.last_error == "disk full"
    assert manager.status()["last_error"] == "disk full"
    assert manager.status()["last_run_at"] is None


def test_successful_run_clears_last_error(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = make_manager(factory)

    def broken() -> list[str]:
        raise OSError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr(manager, "select_ids", broken)
        assert manager.run_once() is False

    assert manager.run_once() is True
    assert manager.status()["last_error"] is None


def test_disabled_manager_does_not_delete(factory: sessionmaker[Session]) -> None:
    ids = seed(factory, [ItemSpec("doomed", OLD, is_read=True, saved=False)])
    manager = make_manager(factory, enabled=False, keep_recent=0)

    assert manager.run_once() is False
    assert manager.status()["enabled"] is False
    assert item_exists(factory, ids["doomed"]) is True

    # start() must not spawn a thread when disabled.
    manager.start()
    assert manager._thread is None
    manager.stop()


def test_start_waits_before_first_run(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = make_manager(factory, interval_seconds=0.2)
    runs: list[bool] = []

    def spy() -> bool:
        runs.append(True)
        return True

    monkeypatch.setattr(manager, "run_once", spy)
    manager.start()
    try:
        status = manager.status()
        assert status["last_run_at"] is None
        assert status["last_deleted"] == 0
        assert runs == []  # no immediate cleanup on start
    finally:
        manager.stop()
    assert runs == []


def test_thread_runs_after_interval(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = make_manager(factory, interval_seconds=0.05)
    fired = threading.Event()

    def spy() -> bool:
        fired.set()
        return True

    monkeypatch.setattr(manager, "run_once", spy)
    manager.start()
    try:
        assert fired.wait(timeout=2.0) is True
    finally:
        manager.stop()
