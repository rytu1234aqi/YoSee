"""Alembic can build the Milestone 0 schema from an empty database."""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from server.database.session import create_db_engine
from sqlalchemy import inspect


def test_upgrade_creates_core_tables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database_path = tmp_path / "gamedev.sqlite"
    url = f"sqlite:///{database_path}"
    monkeypatch.setenv("DATABASE_URL", url)

    command.upgrade(Config("alembic.ini"), "head")

    engine = create_db_engine(url)
    try:
        inspector = inspect(engine)
        names = set(inspector.get_table_names())
        reading_state_columns = {
            column["name"] for column in inspector.get_columns("reading_states")
        }
    finally:
        engine.dispose()
    assert names == {
        "alembic_version",
        "content_items",
        "fetch_runs",
        "reading_states",
        "sources",
    }
    # The newest migration adds the collection sub-label without recreating
    # the table, so existing rows and older backups stay valid.
    assert "tag" in reading_state_columns
