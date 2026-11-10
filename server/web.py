"""Serve the browser reader and its API from one local process."""

import hashlib
import re
import secrets
from html import escape
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.types import Scope

from server.api import create_app
from server.ingestion.round import DEFAULT_MAX_SOURCES_PER_ROUND

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


# app.js imports its sibling modules with bare absolute paths. Versioning only
# the HTML-level <link>/<script> leaves those imports stuck on URLs a WebView
# may have cached forever; one missing export then aborts the whole module.
MODULE_IMPORT = re.compile(r"""(["'])(/static/[A-Za-z0-9_.-]+\.mjs)(?:\?v=[^"']*)?\1""")


def versioned_app_js(version: str) -> str:
    """Give every module import the same content fingerprint as app.js itself.

    The rewrite is idempotent (an existing ``?v=`` is replaced, not appended)
    and happens in the response only: the file on disk stays plain, so
    ``asset_version()`` remains stable between the HTML and the script.
    """

    source = (WEB_ROOT / "app.js").read_text(encoding="utf-8")
    return MODULE_IMPORT.sub(
        lambda match: f"{match.group(1)}{match.group(2)}?v={version}{match.group(1)}",
        source,
    )


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
    max_sources_per_round: int = DEFAULT_MAX_SOURCES_PER_ROUND,
    auth_token: str | None = None,
    auto_backup: bool = True,
    backup_dir: Path | None = None,
    auto_cleanup: bool = True,
    cleanup_keep_recent: int = 200,
    auto_body_fetch: bool = True,
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
        max_sources_per_round=max_sources_per_round,
        auth_token=token,
        auto_backup=auto_backup,
        backup_dir=backup_dir,
        auto_cleanup=auto_cleanup,
        cleanup_keep_recent=cleanup_keep_recent,
        auto_body_fetch=auto_body_fetch,
    )
    # Register before the /static mount so this exact path wins over the
    # catch-all StaticFiles route.
    @app.get("/static/app.js", include_in_schema=False)
    def versioned_script() -> Response:
        return Response(
            versioned_app_js(asset_version()),
            media_type="text/javascript",
            headers={"Cache-Control": "no-store"},
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
