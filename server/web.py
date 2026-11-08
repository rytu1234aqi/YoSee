"""Serve the browser reader and its API from one local process."""

import hashlib
import secrets
from html import escape
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.types import Scope

from server.api import create_app

WEB_ROOT = Path(__file__).resolve().parent.parent / "web"
TOKEN_PLACEHOLDER = "__LOCAL_TOKEN__"
ASSET_VERSION_PLACEHOLDER = "__ASSET_VERSION__"


def asset_version() -> str:
    """Fingerprint the shipped assets so an embedded WebView cannot reuse them.

    ``Cache-Control: no-store`` only affects responses the client actually
    fetches. WebViews that already cached ``/static/app.css`` keep serving that
    copy forever without a request, so a changed stylesheet silently leaves
    newer markup laid out by older CSS. A content-derived query string gives
    every asset change a URL the cache has never seen.
    """

    digest = hashlib.sha256()
    for path in sorted(WEB_ROOT.rglob("*")):
        if not path.is_file() or path.name == "index.html":
            continue
        digest.update(path.relative_to(WEB_ROOT).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


class WebStaticFiles(StaticFiles):
    """Keep browser asset types independent of Windows file associations."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        # Embedded WebViews cache aggressively. These files are local, so always
        # serve the current copy instead of letting a stale CSS/JS survive a reload.
        response.headers["cache-control"] = "no-store"
        media_type = {
            ".js": "text/javascript; charset=utf-8",
            ".mjs": "text/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
        }.get(Path(path).suffix.lower())
        if media_type and response.status_code in (200, 206):
            response.headers["content-type"] = media_type
        return response


def create_web_app(
    database_url: str | None = None,
    *,
    auto_fetch: bool = True,
    poll_seconds: float = 30,
    auth_token: str | None = None,
    auto_backup: bool = True,
    backup_dir: Path | None = None,
) -> FastAPI:
    """Build the production app with a per-process token for the local API.

    The token is injected into the single page served to the client. Any other
    local process or rebound browser origin has no way to read it, so write and
    export endpoints cannot be driven from outside this UI.
    """

    token = auth_token if auth_token is not None else secrets.token_urlsafe(32)
    app = create_app(
        database_url,
        auto_fetch=auto_fetch,
        poll_seconds=poll_seconds,
        auth_token=token,
        auto_backup=auto_backup,
        backup_dir=backup_dir,
    )
    app.mount("/static", WebStaticFiles(directory=WEB_ROOT), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> HTMLResponse:
        markup = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
        markup = markup.replace(TOKEN_PLACEHOLDER, escape(token, quote=True))
        markup = markup.replace(ASSET_VERSION_PLACEHOLDER, asset_version())
        return HTMLResponse(markup, headers={"Cache-Control": "no-store"})

    return app


app = create_web_app()
