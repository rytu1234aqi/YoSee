"""Link-only cards are flagged by the API so the UI can open the original."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from server.api import _has_body, create_app
from server.database.base import Base
from server.database.session import create_db_engine, create_session_factory
from server.models import ContentItem, ContentType, Source

_REAL_BODY = "<p>A real paragraph of article text that is worth reading.</p>"


def _item(
    item_id: str = "x",
    url_hash: str = "a" * 64,
    *,
    html: str | None = None,
    text: str | None = None,
) -> ContentItem:
    return ContentItem(
        id=item_id,
        source_id="body-source",
        content_type=ContentType.ARTICLE,
        title=item_id,
        canonical_url=f"https://example.test/{item_id}",
        url_hash=url_hash,
        content_html=html,
        content_text=text,
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
        fetched_at=datetime.now(UTC),
    )


@pytest.mark.parametrize(
    ("html", "text", "expected"),
    [
        (None, None, False),
        ("", "   ", False),
        ("<p></p>", None, False),
        ("<p>Comments</p>", None, False),
        ("&lt;p&gt;Comments&lt;/p&gt;", None, False),
        ("<script>var a = 'lots of javascript here';</script>", None, False),
        (_REAL_BODY, None, True),
        (None, "A real paragraph of article text that is worth reading.", True),
        ("<p>这是一段真正存下来的中文正文，长度足够用来在软件里阅读。</p>", None, True),
    ],
)
def test_has_body_ignores_markup_only_content(
    html: str | None, text: str | None, expected: bool
) -> None:
    assert _has_body(_item(html=html, text=text)) is expected


def test_feed_page_and_detail_all_report_has_body(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'body.sqlite'}"
    engine = create_db_engine(url)
    Base.metadata.create_all(engine)
    with create_session_factory(engine)() as session:
        session.add(
            Source(
                id="body-source",
                name="Body source",
                slug="body-source",
                provider_type="rss",
                fetch_interval_seconds=60,
            )
        )
        session.flush()
        session.add(_item("with-body", "a" * 64, html=_REAL_BODY))
        session.add(_item("link-only", "b" * 64))
        session.commit()
    engine.dispose()

    client = TestClient(create_app(url))
    feed = {item["id"]: item for item in client.get("/feed").json()}
    assert feed["with-body"]["has_body"] is True
    assert feed["link-only"]["has_body"] is False

    page = {item["id"]: item for item in client.get("/feed/page").json()["items"]}
    assert page["with-body"]["has_body"] is True
    assert page["link-only"]["has_body"] is False

    assert client.get("/content/with-body").json()["has_body"] is True
    assert client.get("/content/link-only").json()["has_body"] is False
