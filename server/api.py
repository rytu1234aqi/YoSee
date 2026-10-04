"""Local reader API, with optional application-owned source polling."""

import asyncio
import base64
import binascii
import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from server.database.session import create_db_engine, create_session_factory, get_database_url
from server.ingestion.fetcher import Fetcher
from server.ingestion.pipeline import FetchResult, IngestionPipeline
from server.ingestion.scheduler import FetchScheduler
from server.models import ContentItem, FetchRun, ReadingState, Source
from server.repositories.content_repository import ContentRepository


class FeedItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    summary: str | None
    source_id: str
    source_name: str
    author: str | None
    published_at: datetime | None
    canonical_url: str
    is_read: bool = False
    saved: bool = False


class SourceItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    slug: str
    enabled: bool
    provider_type: str
    homepage_url: str | None = None
    fetch_interval_seconds: int
    last_fetched_at: datetime | None = None
    last_success_at: datetime | None = None
    consecutive_failures: int = 0
    last_error: str | None = None


class SourceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    feed_url: str = Field(min_length=1, max_length=2048)
    fetch_interval_seconds: int = Field(default=21600, ge=300, le=2592000)

    @classmethod
    def validate_url(cls, value: str) -> str:
        parsed = urlparse(value.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("feed_url must be an HTTP or HTTPS URL")
        return value.strip()

    def model_post_init(self, __context: object) -> None:
        self.name = self.name.strip()
        self.feed_url = self.validate_url(self.feed_url)
        if not self.name:
            raise ValueError("name must not be empty")


class SourceDiscover(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    url: str = Field(min_length=1, max_length=2048)

    def model_post_init(self, __context: object) -> None:
        parsed = urlparse(self.url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("url must be an HTTP or HTTPS URL")
        self.url = parsed.geturl()
        if self.name is not None:
            self.name = self.name.strip() or None


class HtmlSourceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=1, max_length=2048)
    item_selector: str = Field(min_length=1, max_length=300)
    title_selector: str = Field(min_length=1, max_length=300)
    link_selector: str = Field(min_length=1, max_length=300)
    summary_selector: str | None = Field(default=None, max_length=300)
    fetch_interval_seconds: int = Field(default=21600, ge=300, le=2592000)

    def model_post_init(self, __context: object) -> None:
        parsed = urlparse(self.url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("url must be an HTTP or HTTPS URL")
        self.url = parsed.geturl()
        self.name = self.name.strip()


class SourceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    enabled: bool | None = None
    fetch_interval_seconds: int | None = Field(default=None, ge=300, le=2592000)


class SourceAction(BaseModel):
    id: str
    name: str
    slug: str
    enabled: bool
    source: SourceItem
    discovered: int
    inserted: int
    updated: int
    ok: bool
    error: str | None = None


class SourceFetchAction(SourceAction):
    pass


class FetchRunItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    started_at: datetime
    finished_at: datetime | None
    status: str
    items_discovered: int
    items_created: int
    items_updated: int
    items_skipped: int
    error_message: str | None = None


class ExportRequest(BaseModel):
    include_reading_state: bool = True


class ImportRequest(BaseModel):
    items: list[dict[str, object]] | None = Field(default=None, max_length=10000)
    sources: list[dict[str, object]] | None = Field(default=None, max_length=1000)
    articles: list[dict[str, object]] | None = Field(default=None, max_length=10000)
    reading_states: list[dict[str, object]] | None = Field(default=None, max_length=10000)


class ImportResult(BaseModel):
    imported: int
    skipped: int
    errors: list[str]


class BackupResponse(BaseModel):
    generated_at: datetime
    sources: list[dict[str, object]]
    articles: list[dict[str, object]]
    reading_states: list[dict[str, object]]


def _source_id(name: str, feed_url: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "source"
    suffix = abs(hash(feed_url)) % 1000000
    return f"custom-{base[:80]}-{suffix:06d}"


def _discover_feed(body: bytes, page_url: str) -> str | None:
    soup = BeautifulSoup(body, "html.parser")
    for link in soup.find_all("link", href=True):
        rel = {str(value).lower() for value in (link.get("rel") or [])}
        kind = str(link.get("type") or "").lower().split(";", 1)[0]
        feed_types = {
            "application/rss+xml",
            "application/atom+xml",
            "application/xml",
            "text/xml",
        }
        if "alternate" in rel and kind in feed_types:
            candidate = str(link["href"]).strip()
            if candidate:
                from urllib.parse import urljoin

                return urljoin(page_url, candidate)
    return None


class ArticleItem(FeedItem):
    content_html: str | None
    content_text: str | None
    source_url: str | None
    is_read: bool = False
    saved: bool = False
    read_at: datetime | None = None
    saved_at: datetime | None = None


class StateUpdate(BaseModel):
    is_read: bool | None = None
    saved: bool | None = None


class FeedPage(BaseModel):
    items: list[FeedItem]
    next_cursor: str | None


class FeedCursor(BaseModel):
    published_at: datetime | None
    id: str = Field(min_length=1, max_length=128)

    def encode(self) -> str:
        return base64.urlsafe_b64encode(self.model_dump_json().encode()).decode()

    @classmethod
    def decode(cls, value: str) -> "FeedCursor":
        try:
            return cls.model_validate_json(base64.b64decode(value, altchars=b"-_", validate=True))
        except (ValueError, binascii.Error) as exc:
            raise HTTPException(status_code=422, detail="Invalid feed cursor") from exc


class SyncStatus(BaseModel):
    enabled: bool
    running: bool
    current_source: str | None
    last_checked_at: datetime | None
    last_error: str | None
    revision: int
    failed_sources: int


def _read_feed(
    session: Session,
    *,
    source_id: str | None,
    q: str | None,
    status: str,
    limit: int,
    offset: int = 0,
    cursor: FeedCursor | None = None,
) -> list[FeedItem]:
    statement = (
        select(ContentItem, Source.name, ReadingState.is_read, ReadingState.saved)
        .join(Source, Source.id == ContentItem.source_id)
        .outerjoin(ReadingState, ReadingState.content_id == ContentItem.id)
        .order_by(ContentItem.published_at.desc().nulls_last(), ContentItem.id.desc())
        .offset(offset)
        .limit(limit)
    )
    if source_id:
        statement = statement.where(ContentItem.source_id == source_id)
    if q and q.strip():
        literal = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{literal}%"
        statement = statement.where(
            or_(
                ContentItem.title.ilike(pattern, escape="\\"),
                ContentItem.summary.ilike(pattern, escape="\\"),
                ContentItem.author.ilike(pattern, escape="\\"),
            )
        )
    if status == "unread":
        statement = statement.where(
            or_(ReadingState.content_id.is_(None), ReadingState.is_read.is_(False))
        )
    elif status == "saved":
        statement = statement.where(ReadingState.saved.is_(True))
    elif status == "archived":
        statement = statement.where(ReadingState.saved.is_(False), ReadingState.is_read.is_(True))
    if cursor is not None:
        if cursor.published_at is None:
            statement = statement.where(
                ContentItem.published_at.is_(None), ContentItem.id < cursor.id
            )
        else:
            statement = statement.where(
                or_(
                    ContentItem.published_at < cursor.published_at,
                    and_(
                        ContentItem.published_at == cursor.published_at, ContentItem.id < cursor.id
                    ),
                    ContentItem.published_at.is_(None),
                )
            )
    return [
        FeedItem(
            id=item.id,
            title=item.title,
            summary=item.summary,
            source_id=item.source_id,
            source_name=source_name,
            author=item.author,
            published_at=item.published_at,
            canonical_url=item.canonical_url,
            is_read=bool(is_read),
            saved=bool(saved),
        )
        for item, source_name, is_read, saved in session.execute(statement)
    ]


def create_app(
    database_url: str | None = None,
    *,
    auto_fetch: bool = False,
    poll_seconds: float = 30,
    fetcher_factory: Callable[[], Fetcher] = Fetcher,
) -> FastAPI:
    """Create an API instance; ``database_url`` is injectable for tests."""

    url = database_url or get_database_url()
    engine = create_db_engine(url)
    factory = create_session_factory(engine)
    scheduler = FetchScheduler(factory, poll_seconds=poll_seconds, fetcher_factory=fetcher_factory)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if auto_fetch:
            scheduler.start()
        try:
            yield
        finally:
            await scheduler.stop()
            engine.dispose()

    app = FastAPI(title="有得看", version="0.1.0", lifespan=lifespan)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/sync/status", response_model=SyncStatus)
    async def sync_status() -> SyncStatus:
        with factory() as session:
            failed = (
                session.scalar(
                    select(func.count())
                    .select_from(Source)
                    .where(
                        Source.enabled.is_(True),
                        Source.consecutive_failures > 0,
                    )
                )
                or 0
            )
        return SyncStatus(
            enabled=auto_fetch,
            running=scheduler.current_source is not None,
            current_source=scheduler.current_source,
            last_checked_at=scheduler.last_checked_at,
            last_error=scheduler.last_error,
            revision=scheduler.revision,
            failed_sources=failed,
        )

    @app.get("/sources", response_model=list[SourceItem])
    def sources() -> list[SourceItem]:
        with factory() as session:
            rows = session.scalars(select(Source).order_by(Source.name)).all()
            return [SourceItem.model_validate(row) for row in rows]

    @app.post("/sources", response_model=SourceAction, status_code=201)
    def create_source(payload: SourceCreate) -> SourceAction:
        source_id = _source_id(payload.name, payload.feed_url)
        with factory() as session:
            if session.get(Source, source_id) is not None:
                raise HTTPException(status_code=409, detail="source already exists")
            row = Source(
                id=source_id,
                name=payload.name,
                slug=source_id,
                provider_type="rss",
                homepage_url=f"{urlparse(payload.feed_url).scheme}://{urlparse(payload.feed_url).netloc}",
                enabled=True,
                config_json={"feed_url": payload.feed_url},
                fetch_interval_seconds=payload.fetch_interval_seconds,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            source_data = SourceItem.model_validate(row)

        async def fetch_now() -> FetchResult:
            with factory() as fetch_session:
                async with fetcher_factory() as fetcher:
                    result = await IngestionPipeline(fetch_session, fetcher).fetch_source(source_id)
                fetch_session.commit()
                return result

        result = asyncio.run(fetch_now())
        return SourceAction(
            id=source_data.id,
            name=source_data.name,
            slug=source_data.slug,
            enabled=source_data.enabled,
            source=source_data,
            discovered=result.discovered,
            inserted=result.inserted,
            updated=result.updated,
            ok=result.ok,
            error=result.error,
        )

    @app.post("/sources/discover")
    def discover_source(payload: SourceDiscover) -> dict[str, str]:
        async def fetch_page() -> bytes:
            async with fetcher_factory() as fetcher:
                response = await fetcher.get(payload.url)
                return response.body

        feed_url = _discover_feed(asyncio.run(fetch_page()), payload.url)
        if feed_url is None:
            raise HTTPException(
                status_code=422,
                detail="No RSS or Atom feed was advertised by this page",
            )
        return {"feed_url": feed_url, "name": payload.name or urlparse(payload.url).netloc}

    @app.post("/sources/html", response_model=SourceAction, status_code=201)
    def create_html_source(payload: HtmlSourceCreate) -> SourceAction:
        source_id = _source_id(payload.name, payload.url)
        config: dict[str, object] = {
            "list": {"url": payload.url},
            "selectors": {"item": payload.item_selector},
            "fields": {
                "title": {"selector": payload.title_selector},
                "url": {"selector": payload.link_selector, "attr": "href"},
            },
        }
        if payload.summary_selector:
            fields = config["fields"]
            assert isinstance(fields, dict)
            fields["summary"] = {"selector": payload.summary_selector}
        with factory() as session:
            if session.get(Source, source_id) is not None:
                raise HTTPException(status_code=409, detail="source already exists")
            row = Source(
                id=source_id,
                name=payload.name,
                slug=source_id,
                provider_type="html",
                homepage_url=payload.url,
                enabled=True,
                config_json=config,
                fetch_interval_seconds=payload.fetch_interval_seconds,
            )
            session.add(row)
            session.commit()
            source_data = SourceItem.model_validate(row)

        async def fetch_now() -> FetchResult:
            with factory() as session:
                async with fetcher_factory() as fetcher:
                    result = await IngestionPipeline(session, fetcher).fetch_source(source_id)
                session.commit()
                return result

        result = asyncio.run(fetch_now())
        return SourceAction(
            id=source_data.id,
            name=source_data.name,
            slug=source_data.slug,
            enabled=source_data.enabled,
            source=source_data,
            discovered=result.discovered,
            inserted=result.inserted,
            updated=result.updated,
            ok=result.ok,
            error=result.error,
        )

    @app.patch("/sources/{source_id}", response_model=SourceItem)
    def update_source(source_id: str, payload: SourceUpdate) -> SourceItem:
        with factory() as session:
            row = session.get(Source, source_id)
            if row is None:
                raise HTTPException(status_code=404, detail="source not found")
            if payload.name is not None:
                row.name = payload.name.strip()
            if payload.enabled is not None:
                row.enabled = payload.enabled
            if payload.fetch_interval_seconds is not None:
                row.fetch_interval_seconds = payload.fetch_interval_seconds
            session.commit()
            session.refresh(row)
            return SourceItem.model_validate(row)

    @app.post("/sources/{source_id}/fetch", response_model=SourceFetchAction)
    def fetch_source_now(source_id: str) -> SourceFetchAction:
        with factory() as session:
            row = session.get(Source, source_id)
            if row is None:
                raise HTTPException(status_code=404, detail="source not found")
            source_data = SourceItem.model_validate(row)

        async def fetch_now() -> FetchResult:
            with factory() as session:
                async with fetcher_factory() as fetcher:
                    result = await IngestionPipeline(session, fetcher).fetch_source(source_id)
                session.commit()
                return result

        result = asyncio.run(fetch_now())
        return SourceFetchAction(
            id=source_data.id,
            name=source_data.name,
            slug=source_data.slug,
            enabled=source_data.enabled,
            source=source_data,
            discovered=result.discovered,
            inserted=result.inserted,
            updated=result.updated,
            ok=result.ok,
            error=result.error,
        )

    @app.get("/sources/{source_id}/runs", response_model=list[FetchRunItem])
    def source_runs(
        source_id: str, limit: int = Query(default=20, ge=1, le=100)
    ) -> list[FetchRunItem]:
        with factory() as session:
            if session.get(Source, source_id) is None:
                raise HTTPException(status_code=404, detail="source not found")
            rows = session.scalars(
                select(FetchRun)
                .where(FetchRun.source_id == source_id)
                .order_by(FetchRun.started_at.desc())
                .limit(limit)
            ).all()
            return [FetchRunItem.model_validate(row) for row in rows]

    @app.get("/export/content")
    def export_content(include_reading_state: bool = True) -> list[dict[str, object]]:
        with factory() as session:
            rows = session.execute(
                select(ContentItem, Source.name, ReadingState.is_read, ReadingState.saved)
                .join(Source, Source.id == ContentItem.source_id)
                .outerjoin(ReadingState, ReadingState.content_id == ContentItem.id)
                .order_by(ContentItem.published_at.desc().nulls_last(), ContentItem.id.desc())
            )
            output: list[dict[str, object]] = []
            for item, source_name, is_read, saved in rows:
                record: dict[str, object] = {
                    "id": item.id,
                    "source_id": item.source_id,
                    "source_name": source_name,
                    "title": item.title,
                    "summary": item.summary,
                    "content_html": item.content_html,
                    "content_text": item.content_text,
                    "canonical_url": item.canonical_url,
                    "author": item.author,
                    "published_at": item.published_at.isoformat() if item.published_at else None,
                }
                if include_reading_state:
                    record["is_read"] = bool(is_read)
                    record["saved"] = bool(saved)
                output.append(record)
            return output

    @app.get("/backup", response_model=BackupResponse)
    def backup() -> BackupResponse:
        with factory() as session:
            source_rows = session.scalars(select(Source).order_by(Source.id)).all()
            article_rows = session.scalars(select(ContentItem).order_by(ContentItem.id)).all()
            state_rows = session.scalars(
                select(ReadingState).order_by(ReadingState.content_id)
            ).all()
            return BackupResponse(
                generated_at=datetime.now(UTC),
                sources=[
                    {
                        "id": row.id,
                        "name": row.name,
                        "slug": row.slug,
                        "provider_type": row.provider_type,
                        "homepage_url": row.homepage_url,
                        "enabled": row.enabled,
                        "config_json": row.config_json,
                        "fetch_interval_seconds": row.fetch_interval_seconds,
                    }
                    for row in source_rows
                ],
                articles=[
                    {
                        "id": row.id,
                        "source_id": row.source_id,
                        "external_id": row.external_id,
                        "content_type": row.content_type.value,
                        "title": row.title,
                        "summary": row.summary,
                        "content_html": row.content_html,
                        "content_text": row.content_text,
                        "canonical_url": row.canonical_url,
                        "source_url": row.source_url,
                        "author": row.author,
                        "published_at": row.published_at.isoformat() if row.published_at else None,
                        "fetched_at": row.fetched_at.isoformat(),
                    }
                    for row in article_rows
                ],
                reading_states=[
                    {
                        "content_id": row.content_id,
                        "is_read": row.is_read,
                        "saved": row.saved,
                        "read_at": row.read_at.isoformat() if row.read_at else None,
                        "saved_at": row.saved_at.isoformat() if row.saved_at else None,
                    }
                    for row in state_rows
                ],
            )

    @app.post("/import/content", response_model=ImportResult)
    def import_content(payload: ImportRequest) -> ImportResult:
        imported = 0
        skipped = 0
        errors: list[str] = []
        records = payload.articles if payload.articles is not None else payload.items
        if not records:
            raise HTTPException(status_code=422, detail="Import file contains no articles")
        with factory() as session:
            source_cache: dict[str, Source] = {}
            for source_data in payload.sources or []:
                source_id = str(source_data.get("id") or "")[:128]
                if not source_id:
                    continue
                row = session.get(Source, source_id)
                if row is None:
                    row = Source(
                        id=source_id,
                        name=str(source_data.get("name") or source_id)[:200],
                        slug=str(source_data.get("slug") or f"imported-{source_id}")[:200],
                        provider_type=str(source_data.get("provider_type") or "import"),
                        homepage_url=(
                            str(source_data.get("homepage_url"))
                            if source_data.get("homepage_url")
                            else None
                        ),
                        enabled=bool(source_data.get("enabled", False)),
                        config_json=(
                            source_data.get("config_json")
                            if isinstance(source_data.get("config_json"), dict)
                            else {}
                        ),
                        fetch_interval_seconds=(
                            int(value)
                            if isinstance(
                                value := source_data.get("fetch_interval_seconds"), (int, str)
                            )
                            else 86400
                        ),
                    )
                    session.add(row)
                source_cache[source_id] = row
            session.flush()
            for index, record in enumerate(records):
                try:
                    source_id = str(record.get("source_id") or "imported")[:128]
                    source = source_cache.get(source_id) or session.get(Source, source_id)
                    if source is None:
                        source = Source(
                            id=source_id,
                            name=str(record.get("source_name") or source_id)[:200],
                            slug=f"imported-{source_id}",
                            provider_type="import",
                            enabled=False,
                            config_json={},
                            fetch_interval_seconds=86400,
                        )
                        session.add(source)
                        session.flush()
                    source_cache[source_id] = source
                    canonical = str(record.get("canonical_url") or "").strip()
                    title = str(record.get("title") or "").strip()
                    if not canonical or not title:
                        raise ValueError("title and canonical_url are required")
                    repository = ContentRepository(session)
                    if repository.get_by_url_hash(canonical):
                        skipped += 1
                        continue
                    restored = dict(record)
                    restored["source_id"] = source.id
                    restored.setdefault("id", f"import-{index}-{abs(hash(canonical))}")
                    repository.restore(restored)
                    imported += 1
                except Exception as exc:
                    skipped += 1
                    errors.append(f"item {index + 1}: {exc}")
            session.commit()
            for state_data in payload.reading_states or []:
                content_id = str(state_data.get("content_id") or "")
                if not content_id or session.get(ContentItem, content_id) is None:
                    continue
                state = session.get(ReadingState, content_id) or ReadingState(content_id=content_id)
                state.is_read = bool(state_data.get("is_read", False))
                state.saved = bool(state_data.get("saved", False))
                session.add(state)
            session.commit()
        return ImportResult(imported=imported, skipped=skipped, errors=errors[:50])

    @app.delete("/sources/{source_id}", status_code=204)
    def delete_source(source_id: str) -> None:
        with factory() as session:
            row = session.get(Source, source_id)
            if row is None:
                raise HTTPException(status_code=404, detail="source not found")
            if not source_id.startswith("custom-"):
                raise HTTPException(status_code=409, detail="built-in sources cannot be deleted")
            session.delete(row)
            session.commit()

    @app.get("/feed", response_model=list[FeedItem])
    def feed(
        source_id: str | None = None,
        q: str | None = Query(default=None, max_length=200),
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0, le=100_000),
        status: str = Query(default="all", pattern="^(all|unread|saved|archived)$"),
    ) -> list[FeedItem]:
        with factory() as session:
            return _read_feed(
                session, source_id=source_id, q=q, status=status, limit=limit, offset=offset
            )

    @app.get("/feed/page", response_model=FeedPage)
    def feed_page(
        source_id: str | None = None,
        q: str | None = Query(default=None, max_length=200),
        limit: int = Query(default=50, ge=1, le=200),
        status: str = Query(default="all", pattern="^(all|unread|saved|archived)$"),
        cursor: str | None = Query(default=None, max_length=512),
    ) -> FeedPage:
        before = FeedCursor.decode(cursor) if cursor is not None else None
        with factory() as session:
            items = _read_feed(
                session, source_id=source_id, q=q, status=status, limit=limit + 1, cursor=before
            )
        next_cursor = None
        if len(items) > limit:
            last = items[limit - 1]
            next_cursor = FeedCursor(published_at=last.published_at, id=last.id).encode()
        return FeedPage(items=items[:limit], next_cursor=next_cursor)

    @app.get("/content/{content_id}", response_model=ArticleItem)
    def content(content_id: str) -> ArticleItem:
        with factory() as session:
            row = session.execute(
                select(ContentItem, Source.name)
                .join(Source, Source.id == ContentItem.source_id)
                .where(ContentItem.id == content_id)
            ).one_or_none()
            if row is None:
                raise HTTPException(status_code=404, detail="content not found")
            item, source_name = row
            state_row = session.get(ReadingState, content_id)
            return ArticleItem(
                id=item.id,
                title=item.title,
                summary=item.summary,
                source_id=item.source_id,
                source_name=source_name,
                author=item.author,
                published_at=item.published_at,
                canonical_url=item.canonical_url,
                content_html=item.content_html,
                content_text=item.content_text,
                source_url=item.source_url,
                is_read=bool(state_row and state_row.is_read),
                saved=bool(state_row and state_row.saved),
                read_at=state_row.read_at if state_row else None,
                saved_at=state_row.saved_at if state_row else None,
            )

    @app.put("/content/{content_id}/state")
    def update_state(content_id: str, update: StateUpdate) -> dict[str, bool]:
        with factory() as session:
            if session.get(ContentItem, content_id) is None:
                raise HTTPException(status_code=404, detail="content not found")
            state = session.get(ReadingState, content_id)
            if state is None:
                state = ReadingState(content_id=content_id)
                session.add(state)
            now = datetime.now(UTC)
            if update.is_read is not None:
                state.is_read = update.is_read
                state.read_at = now if update.is_read else None
            if update.saved is not None:
                state.saved = update.saved
                state.saved_at = now if update.saved else None
            state.updated_at = now
            session.commit()
            return {"is_read": state.is_read, "saved": state.saved}

    return app


app = create_app()
