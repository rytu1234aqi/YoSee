"""Run database migrations and serve the local browser reader."""

import argparse
import math
import sys
from collections.abc import Sequence
from pathlib import Path

from alembic import command
from alembic.config import Config

from server.database.session import create_db_engine, create_session_factory, get_database_url
from server.ingestion.round import (
    DEFAULT_MAX_SOURCES_PER_ROUND,
    validate_max_sources_per_round,
)
from server.ingestion.sources import sync_sources


def initialize_database(
    root: Path, sources_dir: Path | None = None, *, migrate: bool = True,
) -> None:
    """Initialize from explicit resource paths, including inside a frozen desktop runtime."""
    if migrate:
        command.upgrade(Config(str(root / "alembic.ini")), "head")
    engine = create_db_engine(get_database_url())
    try:
        with create_session_factory(engine)() as session:
            sync_sources(session, sources_dir or root / "configs/sources")
            session.commit()
    finally:
        engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m server.run")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-migrate", action="store_true")
    parser.add_argument(
        "--no-auto-fetch", action="store_true", help="read without external requests"
    )
    parser.add_argument(
        "--poll-seconds", type=float, default=30, help="check due sources every N seconds"
    )
    parser.add_argument(
        "--max-sources-per-round",
        type=int,
        default=DEFAULT_MAX_SOURCES_PER_ROUND,
        help="fetch at most N sources per round tick (default: 15, allowed 1-10000)",
    )
    parser.add_argument("--sources-dir", type=Path, default=None)
    parser.add_argument(
        "--no-auto-backup", action="store_true", help="disable daily lightweight backups"
    )
    parser.add_argument(
        "--backup-dir", type=Path, default=None, help="override the daily backup folder"
    )
    parser.add_argument(
        "--no-auto-cleanup", action="store_true", help="keep every seen link forever"
    )
    parser.add_argument(
        "--cleanup-keep",
        type=int,
        default=200,
        help="always keep this many newest links (default: 200)",
    )
    parser.add_argument(
        "--no-body-fetch",
        action="store_true",
        help="do not fetch full article text when an item is saved",
    )
    args = parser.parse_args(argv)
    if not 50 <= args.cleanup_keep <= 100_000:
        parser.error("--cleanup-keep must be between 50 and 100000")
    if not math.isfinite(args.poll_seconds) or args.poll_seconds <= 0:
        parser.error("--poll-seconds must be finite and positive")
    try:
        validate_max_sources_per_round(args.max_sources_per_round)
    except ValueError as exc:
        parser.error(str(exc))
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        print(
            "warning: binding a non-loopback host exposes the local reader to the network; "
            "keep the generated API token private",
            file=sys.stderr,
        )

    root = Path(__file__).resolve().parent.parent
    initialize_database(root, args.sources_dir, migrate=not args.no_migrate)

    try:
        import uvicorn
    except ImportError as exc:
        raise SystemExit("uvicorn is required; run `uv sync` first") from exc
    from server.web import create_web_app

    app = create_web_app(
        auto_fetch=not args.no_auto_fetch,
        poll_seconds=args.poll_seconds,
        max_sources_per_round=args.max_sources_per_round,
        auto_backup=not args.no_auto_backup,
        backup_dir=args.backup_dir,
        auto_cleanup=not args.no_auto_cleanup,
        cleanup_keep_recent=args.cleanup_keep,
        auto_body_fetch=not args.no_body_fetch,
    )
    uvicorn.run(app, host=args.host, port=args.port, reload=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
