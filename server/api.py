"""Local reader API, with optional application-owned source polling."""

import asyncio
import base64
import binascii
import hashlib
import html
import math
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.orm import Session
from starlette.responses import Response

from server import __version__
from server.backup import BackupManager, default_backup_dir
from server.body_fetch import BodyFetchManager
from server.cleanup import CleanupManager
from server.covers import first_image_url
from server.database.session import create_db_engine, create_session_factory, get_database_url
from server.ingestion.dedup import canonical_url_hash
from server.ingestion.fetcher import Fetcher
from server.ingestion.keyword_filter import source_keywords as read_source_keywords
from server.ingestion.pipeline import FetchResult
from server.ingestion.scheduler import FetchScheduler
from server.models import ContentItem, FetchRun, ReadingState, Source
from server.repositories.content_repository import ContentRepository
from server.security import (
    MAX_BODY_BYTES,
    MAX_HTML_LENGTH,
    UnsafeUrlError,
    is_loopback_host,
    safe_http_url,
    sanitize_html,
    token_matches,
    validate_source_url,
)

# Applied by the middleware so the browser, Tauri and the Android WebView all
# get the same second line of defence behind server-side HTML sanitization.
CONTENT_SECURITY_POLICY = (
    "default-src 'self' blob:; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob: http: https:; "
    "connect-src 'self'; "
    "object-src 'none'; base-uri 'none'; frame-src 'none'"
)


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
    tag: str | None = None
    cover_url: str | None = None
    has_body: bool = False


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
    keywords: list[str] = Field(default_factory=list)
    stale: bool = False
    days_since_update: int | None = None


# A source is only called "quiet" after it has been silent for at least a week,
# and at least twice its own polling interval. A daily feed is not stale after
# two days; a monthly digest is not stale after eight.
STALE_FLOOR_DAYS = 7


def _source_keywords(row: Source) -> list[str]:
    """Show the UI exactly the same allowlist the ingestion pipeline uses."""

    return read_source_keywords(row)


def _days_since_update(row: Source, now: datetime) -> int | None:
    stamp = row.last_success_at or row.last_fetched_at or row.created_at
    if stamp is None:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return max(0, (now - stamp).days)


def _source_stale(row: Source, now: datetime) -> bool:
    days = _days_since_update(row, now)
    if days is None:
        return False
    quiet_days = max(
        STALE_FLOOR_DAYS, math.ceil(row.fetch_interval_seconds * 2 / 86400)
    )
    return days >= quiet_days


def _source_item(row: Source, now: datetime | None = None) -> SourceItem:
    """Add the computed UI hints (keyword list, quiet-source flag)."""

    moment = now or datetime.now(UTC)
    item = SourceItem.model_validate(row)
    item.keywords = _source_keywords(row)
    item.days_since_update = _days_since_update(row, moment)
    item.stale = _source_stale(row, moment)
    return item


def _clean_name(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("name must not be empty")
    return cleaned


def _clean_url(value: str) -> str:
    try:
        return validate_source_url(value)
    except UnsafeUrlError as exc:
        raise ValueError(str(exc)) from exc


def _clean_keywords(values: list[str]) -> list[str]:
    """One canonical lowercase list, shared by create and edit paths."""

    cleaned: list[str] = []
    for value in values:
        word = value.strip().lower()
        if word and word not in cleaned:
            cleaned.append(word)
    return cleaned[:20]


class SourceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    feed_url: str = Field(min_length=1, max_length=2048)
    fetch_interval_seconds: int = Field(default=21600, ge=300, le=2592000)
    keywords: list[str] = Field(default_factory=list, max_length=20)

    def model_post_init(self, __context: object) -> None:
        self.name = _clean_name(self.name)
        self.feed_url = _clean_url(self.feed_url)
        self.keywords = _clean_keywords(self.keywords)


class SourceDiscover(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    url: str = Field(min_length=1, max_length=2048)

    def model_post_init(self, __context: object) -> None:
        self.url = _clean_url(self.url)
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
    keywords: list[str] = Field(default_factory=list, max_length=20)

    def model_post_init(self, __context: object) -> None:
        self.url = _clean_url(self.url)
        self.name = _clean_name(self.name)
        self.keywords = _clean_keywords(self.keywords)


class SourceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    enabled: bool | None = None
    fetch_interval_seconds: int | None = Field(default=None, ge=300, le=2592000)
    keywords: list[str] | None = Field(default=None, max_length=20)

    def model_post_init(self, __context: object) -> None:
        if self.name is not None:
            self.name = _clean_name(self.name)
        if self.keywords is not None:
            self.keywords = _clean_keywords(self.keywords)


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


class ImportSource(BaseModel):
    """A source record from a backup. Imported sources are always offline."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1, max_length=128)
    name: str | None = Field(default=None, max_length=200)
    slug: str | None = Field(default=None, max_length=200)
    provider_type: str | None = Field(default=None, max_length=64)
    homepage_url: str | None = Field(default=None, max_length=2048)
    config_json: dict[str, Any] = Field(default_factory=dict)
    fetch_interval_seconds: int = Field(default=86400, ge=300, le=2592000)


class ImportArticle(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str | None = Field(default=None, max_length=36)
    source_id: str | None = Field(default=None, max_length=128)
    source_name: str | None = Field(default=None, max_length=200)
    external_id: str | None = Field(default=None, max_length=512)
    content_type: str | None = Field(default=None, max_length=32)
    title: str = Field(min_length=1, max_length=4000)
    summary: str | None = Field(default=None, max_length=20000)
    content_html: str | None = Field(default=None, max_length=MAX_HTML_LENGTH)
    content_text: str | None = Field(default=None, max_length=MAX_HTML_LENGTH)
    canonical_url: str = Field(min_length=1, max_length=2048)
    source_url: str | None = Field(default=None, max_length=2048)
    author: str | None = Field(default=None, max_length=512)
    published_at: str | None = Field(default=None, max_length=64)
    fetched_at: str | None = Field(default=None, max_length=64)


class ImportState(BaseModel):
    model_config = ConfigDict(extra="ignore")

    content_id: str = Field(min_length=1, max_length=36)
    is_read: bool = False
    saved: bool = False
    tag: str | None = Field(default=None, pattern="^(like|later)$")


class ImportRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    items: list[ImportArticle] | None = Field(default=None, max_length=10000)
    sources: list[ImportSource] | None = Field(default=None, max_length=1000)
    articles: list[ImportArticle] | None = Field(default=None, max_length=10000)
    reading_states: list[ImportState] | None = Field(default=None, max_length=10000)


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
    """Stable across restarts: Python's builtin ``hash`` is salted per process."""

    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "source"
    suffix = hashlib.sha256(feed_url.strip().encode("utf-8")).hexdigest()[:10]
    return f"custom-{base[:80]}-{suffix}"


def _source_action(source: SourceItem, result: FetchResult) -> SourceAction:
    return SourceAction(
        id=source.id,
        name=source.name,
        slug=source.slug,
        enabled=source.enabled,
        source=source,
        discovered=result.discovered,
        inserted=result.inserted,
        updated=result.updated,
        ok=result.ok,
        error=result.error,
    )


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
    # "none" is the explicit way to clear a sub-label; JSON null means "leave
    # the tag alone" so callers can change is_read without touching it.
    tag: str | None = Field(default=None, pattern="^(like|later|none)$")


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


class BackupStatus(BaseModel):
    enabled: bool
    format: str
    directory: str
    last_backup_at: datetime | None = None
    latest_file: str | None = None
    files: int = 0
    last_error: str | None = None


class BackupRunResult(BaseModel):
    ok: bool
    path: str | None = None
    error: str | None = None


class SavedLink(BaseModel):
    title: str
    url: str
    source_name: str
    tag: str | None = None
    published_at: str | None = None


class CleanupStatus(BaseModel):
    enabled: bool
    keep_recent: int
    min_age_days: int
    last_run_at: datetime | None = None
    last_deleted: int = 0
    last_error: str | None = None


class CleanupRunResult(BaseModel):
    ok: bool
    deleted: int = 0
    error: str | None = None


class BodyFetchStatus(BaseModel):
    enabled: bool
    pending: int = 0
    attempted: int = 0
    completed: int = 0
    failed: int = 0
    last_run_at: str | None = None
    last_error: str | None = None


class BodyFetchRunResult(BaseModel):
    ok: bool
    queued: int = 0
    error: str | None = None


_NO_BODY_LABELS = frozenset(
    {
        "comments",
        "comment",
        "read more",
        "continue reading",
        "link",
        "discuss",
        "discussion",
        "原文链接",
        "查看原文",
        "阅读全文",
    }
)
_SCRIPT_STYLE = re.compile(
    r"<\s*(?:script|style)\b[^>]*>[\s\S]*?<\s*/\s*(?:script|style)\s*>",
    re.IGNORECASE,
)
_HTML_TAG = re.compile(r"<[^>]*>")


def _has_body(item: ContentItem) -> bool:
    """True when the stored article has text worth reading inside the app.

    Link-aggregator feeds often keep only a URL and a ``Comments`` link. Those
    rows should open the original site directly instead of showing an empty
    reader, so the feed tells the UI which kind of card it is rendering.
    """
    raw = item.content_html or item.content_text
    if not raw or not raw.strip():
        return False
    cleaned = _SCRIPT_STYLE.sub(" ", html.unescape(raw))
    text = re.sub(r"\s+", " ", _HTML_TAG.sub(" ", cleaned)).strip()
    return len(text) >= 24 and text.lower() not in _NO_BODY_LABELS


def _cover_for(item: ContentItem) -> str | None:
    """Return a cover for new rows and for older rows that stored none.

    Thousands of articles predate cover extraction, but many of them still
    have an image inside the saved body or summary. Derive it at read time
    instead of migrating the database.
    """
    stored = item.cover_url
    if isinstance(stored, str) and stored.lower().startswith(("http://", "https://")):
        return stored
    from_body = first_image_url(item.content_html, item.canonical_url)
    if from_body is not None:
        return from_body
    return first_image_url(item.summary, item.canonical_url)


def _read_feed(
    session: Session,
    *,
    source_id: str | None,
    q: str | None,
    status: str,
    limit: int,
    offset: int = 0,
    cursor: FeedCursor | None = None,
    seen_after: datetime | None = None,
) -> list[FeedItem]:
    statement = (
        select(
            ContentItem,
            Source.name,
            ReadingState.is_read,
            ReadingState.saved,
            ReadingState.tag,
        )
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
    if seen_after is not None:
        statement = statement.where(ReadingState.read_at >= seen_after)
        statement = statement.order_by(None).order_by(
            ReadingState.read_at.desc(), ContentItem.id.desc()
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
            tag=tag,
            cover_url=_cover_for(item),
            has_body=_has_body(item),
        )
        for item, source_name, is_read, saved, tag in session.execute(statement)
    ]


def create_app(
    database_url: str | None = None,
    *,
    auto_fetch: bool = False,
    poll_seconds: float = 30,
    fetcher_factory: Callable[..., Fetcher] = Fetcher,
    auth_token: str | None = None,
    auto_backup: bool = False,
    backup_dir: Path | None = None,
    auto_cleanup: bool = False,
    cleanup_keep_recent: int = 200,
    auto_body_fetch: bool = False,
) -> FastAPI:
    """Create an API instance; ``database_url`` is injectable for tests.

    ``auth_token`` is generated by the web entry points. Tests and embedding
    callers that pass no token keep the legacy open behaviour.
    """

    url = database_url or get_database_url()
    engine = create_db_engine(url)
    factory = create_session_factory(engine)
    scheduler = FetchScheduler(factory, poll_seconds=poll_seconds, fetcher_factory=fetcher_factory)
    resolved_backup_dir = backup_dir or default_backup_dir(url)
    backup_manager = BackupManager(
        factory,
        resolved_backup_dir or Path("data/backups"),
        enabled=auto_backup and resolved_backup_dir is not None,
    )
    cleanup_manager = CleanupManager(
        factory, enabled=auto_cleanup, keep_recent=cleanup_keep_recent
    )
    body_manager = BodyFetchManager(
        factory, enabled=auto_body_fetch, fetcher_factory=fetcher_factory
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if auto_fetch:
            scheduler.start()
        # Backups start before cleanup so a long-running session always has a
        # snapshot of today before any old link is pruned.
        backup_manager.start()
        cleanup_manager.start()
        body_manager.start()
        try:
            yield
        finally:
            body_manager.stop()
            cleanup_manager.stop()
            backup_manager.stop()
            await scheduler.stop()
            engine.dispose()

    app = FastAPI(title="有得看", version=__version__, lifespan=lifespan)
    app.state.local_token = auth_token

    @app.middleware("http")
    async def local_guard(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Reject cross-site and DNS-rebound requests to the local API.

        A browser page cannot learn the token, and a rebound hostname is not a
        loopback host, so both are refused before any endpoint runs.
        """

        path = request.url.path
        public = path in {"/", "/health", "/docs", "/redoc", "/openapi.json"}
        public = public or path.startswith("/static/")
        provided = request.headers.get("x-local-token", "")
        authorized = auth_token is not None and token_matches(provided, auth_token)
        if auth_token is not None:
            host = request.headers.get("host", "")
            if not is_loopback_host(host) and not authorized:
                return JSONResponse(status_code=400, content={"detail": "invalid host header"})
            if not public and not authorized:
                return JSONResponse(
                    status_code=401, content={"detail": "missing or invalid local token"}
                )
            declared = request.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
                return JSONResponse(
                    status_code=413, content={"detail": "request body too large"}
                )
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        return response

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/backup/status", response_model=BackupStatus)
    def backup_status() -> BackupStatus:
        return BackupStatus.model_validate(backup_manager.status())

    @app.post("/backup/run", response_model=BackupRunResult)
    def backup_run() -> BackupRunResult:
        ok = backup_manager.run_once()
        return BackupRunResult(
            ok=ok,
            path=str(backup_manager.last_path) if ok else None,
            error=None if ok else (backup_manager.last_error or "backup disabled"),
        )

    @app.get("/cleanup/status", response_model=CleanupStatus)
    def cleanup_status() -> CleanupStatus:
        return CleanupStatus.model_validate(cleanup_manager.status())

    @app.post("/cleanup/run", response_model=CleanupRunResult)
    def cleanup_run() -> CleanupRunResult:
        ok = cleanup_manager.run_once()
        status = cleanup_manager.status()
        return CleanupRunResult(
            ok=ok,
            deleted=int(status["last_deleted"]) if ok else 0,
            error=None if ok else (status["last_error"] or "cleanup disabled"),
        )

    @app.get("/body-fetch/status", response_model=BodyFetchStatus)
    def body_fetch_status() -> BodyFetchStatus:
        return BodyFetchStatus.model_validate(body_manager.status())

    @app.post("/body-fetch/run", response_model=BodyFetchRunResult)
    def body_fetch_run() -> BodyFetchRunResult:
        if not body_manager.status()["enabled"]:
            return BodyFetchRunResult(ok=False, error="body fetch is disabled")
        try:
            queued = body_manager.enqueue_all_pending(force=True)
            return BodyFetchRunResult(ok=True, queued=queued)
        except Exception as exc:
            return BodyFetchRunResult(ok=False, error=str(exc)[:300])

    @app.get("/seen-today", response_model=list[FeedItem])
    def seen_today() -> list[FeedItem]:
        """Everything read since local midnight, most recent first."""
        now_local = datetime.now().astimezone()
        start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        start_utc = start_local.astimezone(UTC).replace(tzinfo=None)
        with factory() as session:
            return _read_feed(
                session,
                source_id=None,
                q=None,
                status="all",
                limit=200,
                seen_after=start_utc,
            )

    @app.get("/export/saved-links", response_model=list[SavedLink])
    def export_saved_links() -> list[SavedLink]:
        """A plain link list for the collection page, without article bodies."""

        with factory() as session:
            rows = session.execute(
                select(ContentItem, Source.name, ReadingState.tag)
                .join(Source, Source.id == ContentItem.source_id)
                .join(ReadingState, ReadingState.content_id == ContentItem.id)
                .where(ReadingState.saved.is_(True))
                .order_by(ContentItem.published_at.desc().nulls_last(), ContentItem.id.desc())
            ).all()
            return [
                SavedLink(
                    title=item.title,
                    url=item.canonical_url,
                    source_name=source_name,
                    tag=tag,
                    published_at=item.published_at.isoformat()
                    if item.published_at
                    else None,
                )
                for item, source_name, tag in rows
            ]

    @app.get("/sync/status", response_model=SyncStatus)
    def sync_status() -> SyncStatus:
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
            return [_source_item(row) for row in rows]

    @app.post("/sources", response_model=SourceAction, status_code=201)
    def create_source(payload: SourceCreate) -> SourceAction:
        parsed = urlparse(payload.feed_url)
        source_id = _source_id(payload.name, payload.feed_url)
        with factory() as session:
            if session.get(Source, source_id) is not None:
                raise HTTPException(status_code=409, detail="source already exists")
            row = Source(
                id=source_id,
                name=payload.name,
                slug=source_id,
                provider_type="rss",
                homepage_url=f"{parsed.scheme}://{parsed.netloc}",
                enabled=True,
                config_json={"feed_url": payload.feed_url, "keywords": payload.keywords},
                fetch_interval_seconds=payload.fetch_interval_seconds,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            source_data = _source_item(row)

        result = scheduler.fetch_now_sync(source_id)
        return _source_action(source_data, result)

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
        try:
            feed_url = validate_source_url(feed_url)
        except UnsafeUrlError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
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
        config["keywords"] = payload.keywords
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
            source_data = _source_item(row)

        result = scheduler.fetch_now_sync(source_id)
        return _source_action(source_data, result)

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
            if payload.keywords is not None:
                # JSON columns are not tracked for in-place mutation.
                config = dict(row.config_json) if isinstance(row.config_json, dict) else {}
                config["keywords"] = payload.keywords
                row.config_json = config
            session.commit()
            session.refresh(row)
            return _source_item(row)

    @app.post("/sources/{source_id}/fetch", response_model=SourceAction)
    def fetch_source_now(source_id: str) -> SourceAction:
        with factory() as session:
            row = session.get(Source, source_id)
            if row is None:
                raise HTTPException(status_code=404, detail="source not found")
            source_data = _source_item(row)

        result = scheduler.fetch_now_sync(source_id)
        return _source_action(source_data, result)

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
                select(
                    ContentItem,
                    Source.name,
                    ReadingState.is_read,
                    ReadingState.saved,
                    ReadingState.tag,
                )
                .join(Source, Source.id == ContentItem.source_id)
                .outerjoin(ReadingState, ReadingState.content_id == ContentItem.id)
                .order_by(ContentItem.published_at.desc().nulls_last(), ContentItem.id.desc())
            )
            output: list[dict[str, object]] = []
            for item, source_name, is_read, saved, tag in rows:
                record: dict[str, object] = {
                    "id": item.id,
                    "source_id": item.source_id,
                    "source_name": source_name,
                    "title": item.title,
                    "summary": item.summary,
                    "content_html": sanitize_html(item.content_html),
                    "content_text": item.content_text,
                    "canonical_url": item.canonical_url,
                    "author": item.author,
                    "published_at": item.published_at.isoformat() if item.published_at else None,
                }
                if include_reading_state:
                    record["is_read"] = bool(is_read)
                    record["saved"] = bool(saved)
                    record["tag"] = tag
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
                        "content_html": sanitize_html(row.content_html),
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
                        "tag": row.tag,
                        "read_at": row.read_at.isoformat() if row.read_at else None,
                        "saved_at": row.saved_at.isoformat() if row.saved_at else None,
                    }
                    for row in state_rows
                ],
            )

    @app.post("/import/content", response_model=ImportResult)
    def import_content(payload: ImportRequest) -> ImportResult:
        records = payload.articles if payload.articles is not None else payload.items
        if not records:
            raise HTTPException(status_code=422, detail="Import file contains no articles")
        imported = 0
        skipped = 0
        errors: list[str] = []
        # Backup ids are remapped when a URL already exists locally so the
        # reading states in the same file still attach to the right row.
        id_map: dict[str, str] = {}
        with factory() as session:
            try:
                source_cache: dict[str, Source] = {}
                for index, source_data in enumerate(payload.sources or []):
                    try:
                        source_id = source_data.id[:128]
                        row = session.get(Source, source_id)
                        if row is None:
                            config = dict(source_data.config_json)
                            if len(repr(config)) > 100_000:
                                raise ValueError("config_json is too large")
                            slug = (source_data.slug or f"imported-{source_id}")[:200]
                            owner = session.scalar(select(Source.id).where(Source.slug == slug))
                            if owner is not None and owner != source_id:
                                suffix = hashlib.sha256(source_id.encode("utf-8")).hexdigest()[:8]
                                slug = f"{slug[:180]}-{suffix}"
                            row = Source(
                                id=source_id,
                                name=(source_data.name or source_id)[:200],
                                slug=slug,
                                provider_type=source_data.provider_type or "import",
                                homepage_url=source_data.homepage_url,
                                # Backups must never restart automatic fetching.
                                enabled=False,
                                config_json=config,
                                fetch_interval_seconds=source_data.fetch_interval_seconds,
                            )
                            session.add(row)
                        source_cache[source_id] = row
                    except Exception as exc:
                        errors.append(f"source {index + 1}: {exc}")
                session.flush()

                repository = ContentRepository(session)
                for index, record in enumerate(records):
                    try:
                        source_id = (record.source_id or "imported")[:128]
                        source = source_cache.get(source_id) or session.get(Source, source_id)
                        if source is None:
                            source = Source(
                                id=source_id,
                                name=(record.source_name or source_id)[:200],
                                slug=f"imported-{source_id}",
                                provider_type="import",
                                enabled=False,
                                config_json={},
                                fetch_interval_seconds=86400,
                            )
                            session.add(source)
                            session.flush()
                        source_cache[source_id] = source
                        canonical = record.canonical_url.strip()
                        title = record.title.strip()
                        if not canonical or not title:
                            raise ValueError("title and canonical_url are required")
                        original_id = record.id or ""
                        existing = repository.get_by_url_hash(canonical_url_hash(canonical))
                        if existing is not None:
                            if original_id:
                                id_map[original_id] = existing.id
                            skipped += 1
                            continue
                        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
                        item_id = original_id or f"import-{digest}"
                        restored: dict[str, Any] = dict(record.model_dump(exclude_none=True))
                        restored["id"] = item_id
                        restored["source_id"] = source.id
                        restored["title"] = title
                        restored["canonical_url"] = canonical
                        stored = repository.restore(restored)
                        id_map[original_id or item_id] = stored.id
                        if stored.id == item_id:
                            imported += 1
                        else:
                            skipped += 1
                    except Exception as exc:
                        skipped += 1
                        errors.append(f"item {index + 1}: {exc}")
                session.commit()
                for state_data in payload.reading_states or []:
                    content_id = id_map.get(state_data.content_id, state_data.content_id)
                    if not content_id or session.get(ContentItem, content_id) is None:
                        continue
                    state = session.get(ReadingState, content_id) or ReadingState(
                        content_id=content_id
                    )
                    state.is_read = state_data.is_read
                    state.saved = state_data.saved
                    state.tag = state_data.tag
                    session.add(state)
                session.commit()
            except Exception as exc:
                session.rollback()
                return ImportResult(
                    imported=0,
                    skipped=len(records),
                    errors=[f"import aborted: {exc}"][:50],
                )
        return ImportResult(imported=imported, skipped=skipped, errors=errors[:50])

    @app.delete("/sources/{source_id}", status_code=204)
    def delete_source(source_id: str) -> None:
        with factory() as session:
            row = session.get(Source, source_id)
            if row is None:
                raise HTTPException(status_code=404, detail="source not found")
            if not source_id.startswith("custom-"):
                raise HTTPException(status_code=409, detail="built-in sources cannot be deleted")
            # Old databases lack ON DELETE CASCADE, so remove children in the
            # same transaction before deleting the parent.
            session.execute(
                delete(ReadingState).where(
                    ReadingState.content_id.in_(
                        select(ContentItem.id).where(ContentItem.source_id == source_id)
                    )
                )
            )
            session.execute(delete(FetchRun).where(FetchRun.source_id == source_id))
            session.execute(delete(ContentItem).where(ContentItem.source_id == source_id))
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
                cover_url=_cover_for(item),
                has_body=_has_body(item),
                content_html=sanitize_html(item.content_html),
                content_text=item.content_text,
                source_url=safe_http_url(item.source_url) or None,
                is_read=bool(state_row and state_row.is_read),
                saved=bool(state_row and state_row.saved),
                tag=state_row.tag if state_row else None,
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
            if update.tag is not None:
                if update.tag == "none":
                    state.tag = None
                else:
                    # Tagging is a kind of saving; an item cannot be "liked"
                    # and not saved at the same time.
                    state.tag = update.tag
                    state.saved = True
                    state.saved_at = state.saved_at or now
            if update.saved is False:
                state.tag = None
            state.updated_at = now
            session.commit()
            # Saving a link-only article asks the background worker to fetch
            # its full text. This only queues work; the request never waits.
            if state.saved:
                body_manager.enqueue(content_id)
            # Keep the response shape stable for existing callers; the tag is
            # visible through /content/{id} and /feed instead.
            return {"is_read": state.is_read, "saved": state.saved}

    return app


app = create_app()
