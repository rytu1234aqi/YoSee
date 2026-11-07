"""Android (Chaquopy) entry point for the embedded 有得看 backend.

Mirrors server/desktop.py, but runs Uvicorn in-process: MainActivity calls
``start`` on a background thread (it blocks until shutdown) and ``stop`` from
``onDestroy``. Resources ship read-only inside the APK; user data lives in the
app-private ``data_dir`` (Context.getFilesDir).
"""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from sqlalchemy.engine import URL

_server: uvicorn.Server | None = None


def start(data_dir: str, port: int) -> None:
    """Initialize the database and serve the web app until ``stop`` is called."""
    global _server
    os.environ["DATABASE_URL"] = URL.create(
        "sqlite", database=str(Path(data_dir) / "youdekan.sqlite")
    ).render_as_string()
    root = Path(__file__).resolve().parent

    from server.run import initialize_database

    initialize_database(root)

    # Import after configuring the database: server.web reads DATABASE_URL at import time.
    from server.web import create_web_app

    app = create_web_app(auto_fetch=True)
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with original_lifespan(app):
            print(f"YOUDEKAN_READY=http://127.0.0.1:{port}", flush=True)
            yield

    app.router.lifespan_context = lifespan
    _server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, loop="asyncio", http="h11", ws="none",
        timeout_graceful_shutdown=5,
    ))
    _server.run()


def stop() -> None:
    """Ask Uvicorn to shut down; the blocked ``start`` call then returns."""
    if _server is not None:
        _server.should_exit = True
