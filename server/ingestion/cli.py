"""Command line for the Milestone 0 ingestion pipeline.

``load-sources`` copies ``configs/sources/*.yaml`` into the database.
``fetch-source`` fetches one id. ``fetch-all`` fetches every enabled source.
"""

import argparse
import asyncio
import sys
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

from sqlalchemy.orm import Session

from server.database.session import create_db_engine, create_session_factory, get_database_url
from server.ingestion.fetcher import Fetcher
from server.ingestion.pipeline import FetchResult, IngestionPipeline
from server.ingestion.sources import DEFAULT_SOURCES_DIR, SourceConfigError, sync_sources


def main(argv: Sequence[str] | None = None, *, fetcher: Fetcher | None = None) -> int:
    """Run one CLI command. Pass ``fetcher`` only when the caller owns HTTP."""

    parser = argparse.ArgumentParser(prog="python -m server.ingestion.cli")
    commands = parser.add_subparsers(dest="command", required=True)
    fetch = commands.add_parser("fetch-source", help="fetch one source by its id")
    fetch.add_argument("source_id")
    commands.add_parser("fetch-all", help="fetch every enabled source")
    load = commands.add_parser("load-sources", help="load configs/sources into the database")
    load.add_argument("--dir", default=str(DEFAULT_SOURCES_DIR), dest="directory")
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command == "fetch-source":
        return asyncio.run(_fetch_source(args.source_id, fetcher))
    if args.command == "fetch-all":
        return asyncio.run(_fetch_all(fetcher))
    if args.command == "load-sources":
        return _load_sources(Path(args.directory))
    return 2


def format_result(result: FetchResult) -> str:
    """Render one source in the Milestone 0 fetch report."""

    lines = [f"Fetching {result.source_name}..."]
    if not result.ok:
        lines.append(f"failed: {result.error}")
        return "\n".join(lines)
    lines.extend(
        [
            f"{result.discovered} discovered",
            f"{result.inserted} new",
            f"{result.updated} duplicated",
        ]
    )
    if result.skipped:
        lines.append(f"{result.skipped} skipped")
    return "\n".join(lines)


def format_summary(results: list[FetchResult]) -> str:
    """Render the totals after every enabled source has been attempted."""

    fatal = sum(1 for result in results if not result.ok)
    return "\n".join(
        [
            f"{len(results)} sources fetched",
            f"{sum(result.discovered for result in results)} items discovered",
            f"{sum(result.inserted for result in results)} items inserted",
            f"{sum(result.updated for result in results)} duplicates",
            f"{fatal} fatal errors",
        ]
    )


def _load_sources(directory: Path) -> int:
    engine = create_db_engine(get_database_url())
    try:
        with create_session_factory(engine)() as session:
            try:
                rows = sync_sources(session, directory)
            except SourceConfigError as exc:
                print(f"failed: {exc}", file=sys.stderr)
                session.rollback()
                return 1
            session.commit()
        print(f"loaded {len(rows)} sources", file=sys.stdout)
        return 0
    finally:
        engine.dispose()


async def _fetch_source(source_id: str, fetcher: Fetcher | None) -> int:
    result = await _with_pipeline(fetcher, lambda pipeline: pipeline.fetch_source(source_id))
    print(format_result(result), file=sys.stdout)
    return 0 if result.ok else 1


async def _fetch_all(fetcher: Fetcher | None) -> int:
    results = await _with_pipeline(fetcher, lambda pipeline: pipeline.fetch_enabled())
    body = "\n\n".join(format_result(result) for result in results)
    if body:
        print(body, file=sys.stdout)
        print(file=sys.stdout)
    print(format_summary(results), file=sys.stdout)
    return 0 if all(result.ok for result in results) else 1


async def _with_pipeline[T](
    fetcher: Fetcher | None,
    call: Callable[[IngestionPipeline], Awaitable[T]],
) -> T:
    engine = create_db_engine(get_database_url())
    try:
        with create_session_factory(engine)() as session:
            result = await _call(session, fetcher, call)
            session.commit()
            return result
    finally:
        engine.dispose()


async def _call[T](
    session: Session,
    fetcher: Fetcher | None,
    call: Callable[[IngestionPipeline], Awaitable[T]],
) -> T:
    if fetcher is None:
        async with Fetcher() as owned:
            return await call(IngestionPipeline(session, owned))
    return await call(IngestionPipeline(session, fetcher))


if __name__ == "__main__":
    raise SystemExit(main())
