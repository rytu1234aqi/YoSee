"""SQLite engine and session factory.

Milestone 0 uses one local SQLite database. Every connection enables foreign keys,
WAL mode, and a busy timeout. Callers pass the URL; importing this module creates nothing.
"""

import os
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine.url import make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import Pool, StaticPool

DEFAULT_DATABASE_URL = "sqlite:///data/gamedev.sqlite"
_MEMORY_URLS = {"sqlite://", "sqlite:///:memory:"}


def get_database_url() -> str:
    """Return ``DATABASE_URL`` or the local Milestone 0 SQLite file."""

    return os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)


def _enable_sqlite_pragmas(dbapi_connection: Any, _connection_record: Any) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


def _ensure_sqlite_directory(url: str) -> None:
    if url in _MEMORY_URLS:
        return
    database = make_url(url).database
    if database is None or database == ":memory:":
        return
    Path(database).expanduser().parent.mkdir(parents=True, exist_ok=True)


def create_db_engine(url: str, *, poolclass: type[Pool] | None = None) -> Engine:
    """Create an engine. In-memory URLs share one connection so tests can see their writes."""

    kwargs: dict[str, Any] = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        _ensure_sqlite_directory(url)
    if poolclass is not None:
        kwargs["poolclass"] = poolclass
    elif url in _MEMORY_URLS:
        kwargs["poolclass"] = StaticPool
    engine = create_engine(url, **kwargs)
    if engine.dialect.name == "sqlite":
        event.listen(engine, "connect", _enable_sqlite_pragmas)
    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)
