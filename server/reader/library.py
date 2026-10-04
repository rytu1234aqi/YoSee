"""Read stored content for the local window.

This is a reader, not another ingestion path. It opens the existing SQLite file
and does not create tables or change rows.
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from bs4 import BeautifulSoup
from sqlalchemy.engine.url import make_url

from server.database.session import get_database_url


@dataclass(frozen=True, slots=True)
class FeedCard:
    """One row in the content list."""

    id: str
    title: str
    summary: str | None
    source_name: str
    author: str | None
    published_at: datetime | None
    canonical_url: str


@dataclass(frozen=True, slots=True)
class Article:
    """One opened item. Body HTML is empty when the source only stored metadata."""

    id: str
    title: str
    summary: str | None
    source_name: str
    author: str | None
    published_at: datetime | None
    canonical_url: str
    content_html: str | None
    content_text: str | None


class Library:
    """SQLite access for the reader. Callers close it."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._connection.row_factory = sqlite3.Row

    @classmethod
    def open(cls, url: str | None = None) -> "Library":
        """Open the configured database. An in-memory URL is rejected."""

        database_url = get_database_url() if url is None else url
        path = sqlite_path(database_url)
        if not path.is_file():
            raise FileNotFoundError(f"database {path} does not exist")
        return cls(sqlite3.connect(f"file:{path}?mode=ro", uri=True))

    def close(self) -> None:
        self._connection.close()

    def sources(self) -> list[tuple[str, str]]:
        """Return ``(id, name)`` for sources that currently have items."""

        rows = self._connection.execute(
            """
            SELECT sources.id, sources.name
            FROM sources
            WHERE EXISTS (
                SELECT 1 FROM content_items WHERE content_items.source_id = sources.id
            )
            ORDER BY sources.name
            """
        )
        return [(str(row["id"]), str(row["name"])) for row in rows]

    def list_items(
        self,
        *,
        source_id: str | None = None,
        query: str | None = None,
        limit: int = 200,
    ) -> list[FeedCard]:
        """Return newest items. ``query`` matches title, summary, or author."""

        if limit < 1:
            raise ValueError("limit must be positive")
        statement = """
            SELECT
                content_items.id,
                content_items.title,
                content_items.summary,
                sources.name AS source_name,
                content_items.author,
                content_items.published_at,
                content_items.canonical_url
            FROM content_items
            JOIN sources ON sources.id = content_items.source_id
            WHERE (? IS NULL OR content_items.source_id = ?)
              AND (
                    ? IS NULL
                    OR content_items.title LIKE ? ESCAPE '\\'
                    OR content_items.summary LIKE ? ESCAPE '\\'
                    OR content_items.author LIKE ? ESCAPE '\\'
                  )
            ORDER BY content_items.published_at DESC NULLS LAST, content_items.id DESC
            LIMIT ?
        """
        pattern = _like(query)
        rows = self._connection.execute(
            statement,
            (source_id, source_id, pattern, pattern, pattern, pattern, limit),
        )
        return [_card(row) for row in rows]

    def get(self, item_id: str) -> Article | None:
        """Return one item, or ``None`` when it is gone."""

        row = self._connection.execute(
            """
            SELECT
                content_items.id,
                content_items.title,
                content_items.summary,
                sources.name AS source_name,
                content_items.author,
                content_items.published_at,
                content_items.canonical_url,
                content_items.content_html,
                content_items.content_text
            FROM content_items
            JOIN sources ON sources.id = content_items.source_id
            WHERE content_items.id = ?
            """,
            (item_id,),
        ).fetchone()
        if row is None:
            return None
        return Article(
            id=str(row["id"]),
            title=str(row["title"]),
            summary=_optional(row["summary"]),
            source_name=str(row["source_name"]),
            author=_optional(row["author"]),
            published_at=_when(row["published_at"]),
            canonical_url=str(row["canonical_url"]),
            content_html=_optional(row["content_html"]),
            content_text=_optional(row["content_text"]),
        )


def display_text(article: Article) -> str:
    """Return the saved text, falling back from HTML to the summary."""

    if article.content_text:
        return article.content_text
    if article.content_html:
        text = BeautifulSoup(article.content_html, "lxml").get_text(" ", strip=True)
        if text:
            return text
    if article.summary:
        return article.summary
    return "No saved text. Open the original page."


def sqlite_path(url: str) -> Path:
    """Return the file behind a SQLite URL."""

    if not url.startswith("sqlite:"):
        raise ValueError(f"{url} is not a sqlite database")
    database = make_url(url).database
    if database is None or database == ":memory:":
        raise ValueError("the reader needs a sqlite file")
    return Path(database).expanduser()


def _like(query: str | None) -> str | None:
    if query is None:
        return None
    stripped = query.strip()
    if not stripped:
        return None
    escaped = stripped.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _card(row: sqlite3.Row) -> FeedCard:
    return FeedCard(
        id=str(row["id"]),
        title=str(row["title"]),
        summary=_optional(row["summary"]),
        source_name=str(row["source_name"]),
        author=_optional(row["author"]),
        published_at=_when(row["published_at"]),
        canonical_url=str(row["canonical_url"]),
    )


def _optional(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _when(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None
