"""Standalone desktop backend. Resources are read-only; user data lives outside the app."""

import argparse
import os
import socket
import sqlite3
import sys
import tempfile
import threading
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from sqlalchemy.engine import URL

from server.run import initialize_database


def prepare_database(data_dir: Path, import_database: Path | None = None) -> Path:
    """Copy a legacy SQLite snapshot (including WAL) only on the first launch."""
    data_dir.mkdir(parents=True, exist_ok=True)
    target = data_dir / "youdekan.sqlite"
    if import_database is not None and not target.exists():
        with tempfile.NamedTemporaryFile(dir=data_dir, suffix=".sqlite", delete=False) as file:
            temporary = Path(file.name)
        try:
            with sqlite3.connect(import_database.resolve().as_uri() + "?mode=ro", uri=True) as src:
                with sqlite3.connect(temporary) as dst:
                    src.backup(dst)
                    if dst.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                        raise ValueError("原数据库完整性检查失败，未迁移数据")
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
    return target


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="有得看内置服务")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--import-database", type=Path)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--no-auto-fetch", action="store_true")
    parser.add_argument("--watch-parent", action="store_true")
    args = parser.parse_args(argv)
    database = prepare_database(args.data_dir.resolve(), args.import_database)
    os.environ["DATABASE_URL"] = URL.create("sqlite", database=str(database)).render_as_string()
    root = Path(__file__).resolve().parent.parent
    initialize_database(root)
    if args.prepare_only:
        print(database, flush=True)
        return 0

    # Import after configuring the database: server.web also exposes a module-level ASGI app.
    from server.web import create_web_app

    app = create_web_app(auto_fetch=not args.no_auto_fetch)
    original_lifespan = app.router.lifespan_context
    # Keep the socket bound until Uvicorn takes it over: no port-selection race.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]

        @asynccontextmanager
        async def lifespan(app: FastAPI) -> AsyncIterator[None]:
            async with original_lifespan(app):
                print(f"YOUDEKAN_READY=http://127.0.0.1:{port}", flush=True)
                yield

        app.router.lifespan_context = lifespan
        server = uvicorn.Server(uvicorn.Config(
            app, loop="asyncio", http="h11", ws="none", timeout_graceful_shutdown=5,
        ))
        if args.watch_parent:
            def watch_parent() -> None:
                sys.stdin.buffer.read()
                server.should_exit = True

            threading.Thread(target=watch_parent, daemon=True).start()
        server.run(sockets=[listener])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
