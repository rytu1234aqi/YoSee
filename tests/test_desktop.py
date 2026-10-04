"""Regression coverage for migrating live SQLite data into the desktop app."""

import sqlite3
from pathlib import Path

import pytest
from server.desktop import prepare_database


def test_migration_includes_wal_and_never_overwrites_existing_data(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy.sqlite"
    with sqlite3.connect(legacy) as source:
        source.execute("PRAGMA journal_mode=WAL")
        source.execute("CREATE TABLE articles (id INTEGER, title TEXT, saved INTEGER)")
        source.execute("INSERT INTO articles VALUES (1, '留存文章', 1)")
        source.commit()
        assert Path(str(legacy) + "-wal").exists()
        target = prepare_database(tmp_path / "应用 数据", legacy)
        with sqlite3.connect(target) as copy:
            assert copy.execute("SELECT * FROM articles").fetchall() == [(1, "留存文章", 1)]
            copy.execute("INSERT INTO articles VALUES (2, '客户端新增', 0)")
        prepare_database(target.parent, legacy)
        with sqlite3.connect(target) as copy:
            assert copy.execute("SELECT count(*) FROM articles").fetchone() == (2,)
        assert source.execute("SELECT count(*) FROM articles").fetchone() == (1,)


def test_failed_migration_does_not_leave_empty_database(tmp_path: Path) -> None:
    data_dir = tmp_path / "desktop"
    with pytest.raises(sqlite3.OperationalError):
        prepare_database(data_dir, tmp_path / "missing.sqlite")
    assert not (data_dir / "youdekan.sqlite").exists()
    assert list(data_dir.iterdir()) == []


def test_new_install_creates_data_directory(tmp_path: Path) -> None:
    target = prepare_database(tmp_path / "new")
    assert target.parent.is_dir()
    assert not target.exists()  # Alembic creates the database, not a bundled personal database.
