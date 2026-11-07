"""Serve the browser reader and its API from one local process."""

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


class WebStaticFiles(StaticFiles):
    """Keep browser asset types independent of Windows file associations."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
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
    )
    app.mount("/static", WebStaticFiles(directory=WEB_ROOT), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> HTMLResponse:
        markup = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(markup.replace(TOKEN_PLACEHOLDER, escape(token, quote=True)))

    return app


app = create_web_app()
