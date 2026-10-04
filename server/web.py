"""Serve the browser reader and its API from one local process."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from server.api import create_app

WEB_ROOT = Path(__file__).resolve().parent.parent / "web"


def create_web_app(
    database_url: str | None = None, *, auto_fetch: bool = True, poll_seconds: float = 30
) -> FastAPI:
    app = create_app(database_url, auto_fetch=auto_fetch, poll_seconds=poll_seconds)
    app.mount("/static", StaticFiles(directory=WEB_ROOT), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(WEB_ROOT / "index.html")

    return app


app = create_web_app()
