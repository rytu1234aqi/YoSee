"""Round-based scheduling: integration tests against a real in-process scheduler.

All tests use an injectable SQLite session factory, a controllable clock and an
httpx.MockTransport fetcher, so nothing touches the network. Every tick sequence
runs inside ONE ``asyncio.run`` call because ``asyncio.Lock`` binds to the loop
on first use and cannot hop between event loops.

Naming note: ``max_sources_per_round`` caps ONE ``run_once()`` tick, while
``round_number`` counts a FULL PASS over every enabled source. A single round
therefore spans several ticks and its cumulative fetch count may exceed N. See
``test_tick_is_capped_but_round_cumulative_can_exceed_limit``.
"""

import asyncio
import dataclasses
import math
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import server.run as run_module
import uvicorn
from fastapi.testclient import TestClient
from server.api import create_app
from server.database.base import Base
from server.database.session import create_db_engine, create_session_factory
from server.ingestion.fetcher import Fetcher
from server.ingestion.pipeline import FetchResult
from server.ingestion.round import DEFAULT_MAX_SOURCES_PER_ROUND, RoundSnapshot
from server.ingestion.scheduler import FetchScheduler
from server.models import FetchRun, FetchStatus, Source
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

FEED = (Path(__file__).parent / "fixtures/feeds/sample.rss").read_bytes()

ROUND_FIELDS = (
    "round_number",
    "max_sources_per_round",
    "round_total",
    "round_pending",
    "round_fetched",
    "round_processed",
    "round_complete",
)


class Clock:
    """A mutable, injectable ``now`` so due times can be advanced deterministically."""

    def __init__(self, moment: datetime | None = None) -> None:
        self.moment = moment or datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.moment


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'round.sqlite'}")
    Base.metadata.create_all(engine)
    yield create_session_factory(engine)
    engine.dispose()


def source(source_id: str, **overrides: object) -> Source:
    values: dict[str, object] = dict(
        id=source_id,
        name=source_id,
        slug=source_id,
        provider_type="rss",
        config_json={"feed_url": f"https://example.test/{source_id}"},
        fetch_interval_seconds=3600,
        consecutive_failures=0,
    )
    values.update(overrides)
    return Source(**values)


def seed(factory: sessionmaker[Session], rows: list[Source]) -> None:
    with factory() as session:
        session.add_all(rows)
        session.commit()


def recorder(
    requested: list[str], *, fail_ids: frozenset[str] = frozenset()
) -> Callable[..., Fetcher]:
    """Record the source id parsed from the requested URL; optionally fail some."""

    def handler(request: httpx.Request) -> httpx.Response:
        source_id = request.url.path.lstrip("/")
        requested.append(source_id)
        if source_id in fail_ids:
            return httpx.Response(404)  # 404 is not retried, so tests stay fast
        return httpx.Response(200, content=FEED)

    return lambda: Fetcher(transport=httpx.MockTransport(handler))


def make_scheduler(
    factory: sessionmaker[Session],
    limit: int,
    requested: list[str],
    *,
    now: Clock,
    fail_ids: frozenset[str] = frozenset(),
) -> FetchScheduler:
    return FetchScheduler(
        factory,
        max_sources_per_round=limit,
        fetcher_factory=recorder(requested, fail_ids=fail_ids),
        now=now,
    )


async def collect(
    scheduler: FetchScheduler, ticks: int
) -> list[tuple[list[FetchResult], RoundSnapshot]]:
    observed: list[tuple[list[FetchResult], RoundSnapshot]] = []
    for _ in range(ticks):
        results = await scheduler.run_once()
        observed.append((results, scheduler.round_status()))
    return observed


async def drain_round(scheduler: FetchScheduler, max_ticks: int = 12) -> list[FetchResult]:
    """Run ticks until the current round reports complete; return all results."""

    results: list[FetchResult] = []
    for _ in range(max_ticks):
        results.extend(await scheduler.run_once())
        if scheduler.round_status().round_complete:
            return results
    raise AssertionError("round did not complete within max_ticks")


async def complete_round_then_disable_all(
    factory: sessionmaker[Session], scheduler: FetchScheduler
) -> tuple[list[FetchResult], RoundSnapshot]:
    """Finish a round, disable EVERY source, then run one more tick."""

    await drain_round(scheduler)
    with factory() as session:
        rows = list(session.scalars(select(Source)).all())
        for row in rows:
            row.enabled = False
        session.commit()
    empty_results = await scheduler.run_once()
    return empty_results, scheduler.round_status()


def assert_round_fields(status: dict[str, object]) -> None:
    assert all(key in status for key in ROUND_FIELDS)
    for key in ROUND_FIELDS[:-1]:
        assert isinstance(status[key], int), f"{key} should be int, got {type(status[key])}"
    assert isinstance(status["round_complete"], bool)


# ---------------------------------------------------------------------------
# 1. Per-tick cap and gradual spread over several ticks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("limit", [1, 2, 3, 15])
def test_single_tick_fetch_count_never_exceeds_limit(
    factory: sessionmaker[Session], limit: int
) -> None:
    total = limit + 3
    ids = [f"s{i:02d}" for i in range(total)]
    seed(factory, [source(i) for i in ids])
    requested: list[str] = []
    scheduler = make_scheduler(factory, limit, requested, now=Clock())

    results, snap = asyncio.run(collect(scheduler, 1))[0]

    assert len(results) == limit
    assert len(requested) == limit
    assert snap == RoundSnapshot(1, limit, total, 3, limit, limit, False)
    assert snap.round_fetched <= limit


@pytest.mark.parametrize(("total", "limit"), [(5, 2), (7, 3), (4, 1)])
def test_round_spreads_over_ticks_and_reaches_the_last_sources(
    factory: sessionmaker[Session], total: int, limit: int
) -> None:
    ids = [f"s{i:02d}" for i in range(total)]
    seed(factory, [source(i) for i in ids])
    requested: list[str] = []
    scheduler = make_scheduler(factory, limit, requested, now=Clock())
    ticks = math.ceil(total / limit)

    observed = asyncio.run(collect(scheduler, ticks))

    assert all(len(results) <= limit for results, _ in observed)
    # Sorted-last sources are NOT starved: every id is fetched exactly once.
    assert requested == ids
    assert len(requested) == len(set(requested)) == total
    assert [snap.round_pending for _, snap in observed] == [
        max(0, total - limit * (i + 1)) for i in range(ticks)
    ]
    assert [snap.round_complete for _, snap in observed] == [False] * (ticks - 1) + [True]
    final = observed[-1][1]
    assert final == RoundSnapshot(1, limit, total, 0, total, total, True)
    assert all(snap.round_number == 1 for _, snap in observed)
    with factory() as session:
        runs = list(session.scalars(select(FetchRun)).all())
        assert len(runs) == total
        assert {run.source_id for run in runs} == set(ids)
        assert all(run.status == FetchStatus.SUCCESS for run in runs)


def test_pending_fetched_processed_complete_progress_across_a_round(
    factory: sessionmaker[Session],
) -> None:
    ids = ["a", "b", "c", "d", "e"]
    seed(factory, [source(i) for i in ids])
    requested: list[str] = []
    scheduler = make_scheduler(factory, 2, requested, now=Clock())

    async def exercise() -> tuple[RoundSnapshot, list[tuple[list[FetchResult], RoundSnapshot]]]:
        initial = scheduler.round_status()
        return initial, await collect(scheduler, 4)

    initial, observed = asyncio.run(exercise())

    assert initial == RoundSnapshot(0, 2, 0, 0, 0, 0, True)
    assert [snap for _, snap in observed] == [
        # round 1: 待抓 3 -> 1 -> 0, 已抓/已处理 2 -> 4 -> 5, 完成 False -> True
        RoundSnapshot(1, 2, 5, 3, 2, 2, False),
        RoundSnapshot(1, 2, 5, 1, 4, 4, False),
        RoundSnapshot(1, 2, 5, 0, 5, 5, True),
        # round 2 opens but no source is due yet: 0 fetched, still completes.
        RoundSnapshot(2, 2, 5, 0, 0, 5, True),
    ]
    assert [len(results) for results, _ in observed] == [2, 2, 1, 0]
    assert requested == ids


# ---------------------------------------------------------------------------
# 2. Round termination, new rounds and due-time gating
# ---------------------------------------------------------------------------


def test_new_round_opens_and_only_due_sources_are_fetched(
    factory: sessionmaker[Session],
) -> None:
    ids = ["a", "b", "c"]
    seed(factory, [source(i) for i in ids])
    requested: list[str] = []
    clock = Clock()
    scheduler = make_scheduler(factory, 3, requested, now=clock)

    async def exercise() -> list[tuple[list[FetchResult], RoundSnapshot]]:
        observed = await collect(scheduler, 2)
        clock.moment += timedelta(hours=2)  # all intervals (3600s) have elapsed
        observed.extend(await collect(scheduler, 1))
        return observed

    observed = asyncio.run(exercise())

    assert [len(results) for results, _ in observed] == [3, 0, 3]
    assert [snap.round_number for _, snap in observed] == [1, 2, 3]
    assert [snap.round_fetched for _, snap in observed] == [3, 0, 3]
    assert all(snap.round_complete for _, snap in observed)
    # No repeat inside the interval; every source is fetched once per due window.
    assert requested == ids + ids


def test_failed_sources_are_processed_once_and_never_retried_in_round(
    factory: sessionmaker[Session],
) -> None:
    ids = ["a-bad", "b-good", "c-good"]
    seed(factory, [source(i) for i in ids])
    requested: list[str] = []
    scheduler = make_scheduler(
        factory, 2, requested, now=Clock(), fail_ids=frozenset({"a-bad"})
    )

    results, snap = asyncio.run(_drain_with_snapshot(scheduler))

    assert [result.ok for result in results] == [False, True, True]
    assert requested == ids  # a-bad is NOT retried inside the same round
    assert snap == RoundSnapshot(1, 2, 3, 0, 3, 3, True)
    with factory() as session:
        failed = session.get(Source, "a-bad")
        assert failed is not None
        assert failed.consecutive_failures == 1
        runs = list(session.scalars(select(FetchRun).order_by(FetchRun.source_id)).all())
        assert [run.status for run in runs] == [
            FetchStatus.FAILED,
            FetchStatus.SUCCESS,
            FetchStatus.SUCCESS,
        ]


def test_not_due_sources_skip_budget_and_let_round_complete(
    factory: sessionmaker[Session],
) -> None:
    moment = datetime.now(UTC)
    ids = ["a", "b", "c", "d"]
    seed(
        factory,
        [source(i) for i in ids[:2]]
        + [source(i, last_fetched_at=moment) for i in ids[2:]],
    )
    requested: list[str] = []
    scheduler = make_scheduler(factory, 2, requested, now=Clock(moment))

    observed = asyncio.run(collect(scheduler, 2))

    assert [len(results) for results, _ in observed] == [2, 0]
    assert requested == ["a", "b"]  # c/d not due, but covered without budget
    assert observed[0][1] == RoundSnapshot(1, 2, 4, 2, 2, 2, False)
    assert observed[1][1] == RoundSnapshot(1, 2, 4, 0, 2, 4, True)


# ---------------------------------------------------------------------------
# 3. Edge cases: empty list, small totals, disabled/added/removed sources
# ---------------------------------------------------------------------------


def test_fresh_empty_target_list_keeps_round_idle(factory: sessionmaker[Session]) -> None:
    requested: list[str] = []
    scheduler = make_scheduler(factory, 3, requested, now=Clock())

    observed = asyncio.run(collect(scheduler, 3))

    assert requested == []
    assert all(results == [] for results, _ in observed)
    assert all(snap == RoundSnapshot(0, 3, 0, 0, 0, 0, True) for _, snap in observed)


def test_fewer_sources_than_limit_completes_in_one_tick(factory: sessionmaker[Session]) -> None:
    seed(factory, [source("a"), source("b"), source("c")])
    requested: list[str] = []
    scheduler = make_scheduler(factory, 5, requested, now=Clock())

    results, snap = asyncio.run(collect(scheduler, 1))[0]

    assert len(results) == 3
    assert requested == ["a", "b", "c"]
    assert snap == RoundSnapshot(1, 5, 3, 0, 3, 3, True)


def test_exactly_limit_sources_completes_in_one_tick(factory: sessionmaker[Session]) -> None:
    ids = ["a", "b", "c", "d", "e"]
    seed(factory, [source(i) for i in ids])
    requested: list[str] = []
    scheduler = make_scheduler(factory, 5, requested, now=Clock())

    results, snap = asyncio.run(collect(scheduler, 1))[0]

    assert len(results) == 5
    assert requested == ids
    assert snap == RoundSnapshot(1, 5, 5, 0, 5, 5, True)


def test_disabled_sources_are_not_round_targets(factory: sessionmaker[Session]) -> None:
    seed(
        factory,
        [
            source("a"),
            source("b"),
            source("c"),
            source("x-off", enabled=False),
            source("y-off", enabled=False),
        ],
    )
    requested: list[str] = []
    scheduler = make_scheduler(factory, 5, requested, now=Clock())

    results, snap = asyncio.run(collect(scheduler, 1))[0]

    assert len(results) == 3
    assert requested == ["a", "b", "c"]
    assert snap == RoundSnapshot(1, 5, 3, 0, 3, 3, True)


def test_sources_added_mid_round_join_next_round(factory: sessionmaker[Session]) -> None:
    seed(factory, [source("a"), source("b"), source("c")])
    requested: list[str] = []
    scheduler = make_scheduler(factory, 1, requested, now=Clock())

    async def exercise() -> list[tuple[list[FetchResult], RoundSnapshot]]:
        first = await collect(scheduler, 1)
        with factory() as session:
            session.add(source("d"))
            session.commit()
        rest = await collect(scheduler, 3)
        return first + rest

    observed = asyncio.run(exercise())

    assert [len(results) for results, _ in observed] == [1, 1, 1, 1]
    assert requested == ["a", "b", "c", "d"]  # d only appears in round 2
    assert observed[0][1].round_total == 3
    assert observed[2][1].round_complete is True  # round 1 finished before d joined
    assert observed[3][1].round_number == 2
    assert observed[3][1].round_total == 4


def test_sources_disabled_or_deleted_mid_round_do_not_block_completion(
    factory: sessionmaker[Session],
) -> None:
    seed(factory, [source("a"), source("b"), source("c"), source("d")])
    requested: list[str] = []
    scheduler = make_scheduler(factory, 1, requested, now=Clock())

    async def exercise() -> list[tuple[list[FetchResult], RoundSnapshot]]:
        first = await collect(scheduler, 1)
        with factory() as session:
            row_b = session.get(Source, "b")
            row_d = session.get(Source, "d")
            assert row_b is not None and row_d is not None
            row_b.enabled = False
            session.delete(row_d)  # never fetched, so no child rows to cascade
            session.commit()
        return first + await collect(scheduler, 2)

    observed = asyncio.run(exercise())

    assert [len(results) for results, _ in observed] == [1, 1, 0]
    assert requested == ["a", "c"]
    assert observed[2][1] == RoundSnapshot(1, 1, 4, 0, 2, 4, True)


# ---------------------------------------------------------------------------
# 4. The three reviewed suspicions — locked as contract, not defects
# ---------------------------------------------------------------------------


def test_empty_targets_after_completed_round_report_most_recent_round_statistics(
    factory: sessionmaker[Session],
) -> None:
    """REVIEWED SUSPICION 1 (merged): 5 sources / N=2, finish a round, disable
    every source, then tick once.

    CONTRACT, not a defect: ``RoundSnapshot`` describes the MOST RECENT round.
    After a round completes its counters stay visible until the next round opens
    (round.py:66-82, 122-125; README 第 51-53 行), so ``round_total`` keeps the
    5 locked targets of the round that just finished. All four counters are 0
    only when the process never opened a round at all (see
    ``test_fresh_tracker_reports_idle_state``). An empty target list opens no
    new round, fetches nothing and leaves ``round_number`` unchanged.

    This is the single authoritative lock for that sequence: the former xfail
    sentinel asserting ``round_total == 0`` and the duplicate逐字段用例 have
    been merged here, because that expectation contradicts the aligned contract.
    """

    ids = ["a", "b", "c", "d", "e"]
    seed(factory, [source(i) for i in ids])
    requested: list[str] = []
    scheduler = make_scheduler(factory, 2, requested, now=Clock())

    empty_results, snap = asyncio.run(complete_round_then_disable_all(factory, scheduler))

    print("空目标列表后的 7 个字段:", dataclasses.asdict(snap))
    assert empty_results == []  # 本 tick 不抓取
    assert requested == ids  # 无新增抓取、不重复抓
    assert snap.round_number == 1, f"不开新轮: 实际 {snap.round_number}"
    assert snap.max_sources_per_round == 2
    assert snap.round_total == 5, f"最近一轮 5 个目标: 实际 {snap.round_total}"
    assert snap.round_pending == 0
    assert snap.round_fetched == 5, f"本轮累计尝试数: 实际 {snap.round_fetched}"
    assert snap.round_processed == 5, f"最近一轮已覆盖数: 实际 {snap.round_processed}"
    assert snap.round_complete is True
    assert snap == RoundSnapshot(1, 2, 5, 0, 5, 5, True)


def test_round_fetched_counts_failed_attempts_not_successes(
    factory: sessionmaker[Session],
) -> None:
    """REVIEWED SUSPICION 2 (contract): every fetch fails, yet round_fetched is 3.

    ``plan_tick`` increments ``_fetched`` when it DECIDES to fetch (round.py:181),
    before ``run_once`` executes ``_fetch_one``. The counter is "本轮累计决定抓取 /
    尝试数（含失败）", NOT "成功抓取数". This is now explicit in the
    ``RoundSnapshot`` docstring (round.py:79-81) and README 第 51-52 行
    「尝试抓过几个（抓失败的也算一次尝试）」— lock it here as the contract.
    """

    ids = ["a", "b", "c"]
    seed(factory, [source(i) for i in ids])
    requested: list[str] = []
    scheduler = make_scheduler(factory, 2, requested, now=Clock(), fail_ids=frozenset(ids))

    results, snap = asyncio.run(_drain_with_snapshot(scheduler))

    assert len(results) == 3
    assert all(not result.ok for result in results), "全部抓取均失败"
    assert requested == ids, "失败源不在同轮重试"
    assert snap == RoundSnapshot(1, 2, 3, 0, 3, 3, True)
    assert snap.round_fetched == 3, f"0 次成功却累计 {snap.round_fetched} 次「已抓」"
    with factory() as session:
        runs = list(session.scalars(select(FetchRun)).all())
        assert len(runs) == 3
        assert all(run.status == FetchStatus.FAILED for run in runs)


def test_round_fetched_is_incremented_at_plan_time_before_execution(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """REVIEWED SUSPICION 2b (contract): exception before any fetch executes.

    The counter is already 2 even though zero HTTP requests were made, proving
    it is incremented at plan time (as documented in round.py:79-81). In
    production ``_fetch_one`` never raises (scheduler.py:185-215 catches
    Exception), so every planned id yields one result: the "decided, not
    successful" semantics are intentional, not a functional failure.
    """

    seed(factory, [source("a"), source("b"), source("c")])
    requested: list[str] = []
    scheduler = make_scheduler(factory, 2, requested, now=Clock())

    async def boom(source_id: str) -> FetchResult:
        del source_id
        raise RuntimeError("simulated crash before any fetch")

    monkeypatch.setattr(scheduler, "_fetch_one", boom)
    with pytest.raises(RuntimeError, match="simulated crash"):
        asyncio.run(scheduler.run_once())

    snap = scheduler.round_status()
    assert requested == []
    assert snap.round_fetched == 2, f"计划时已 +2，实际 {snap.round_fetched}"
    assert snap.round_processed == 2
    assert snap.round_complete is False


def test_tick_is_capped_but_round_cumulative_can_exceed_limit(
    factory: sessionmaker[Session],
) -> None:
    """REVIEWED SUSPICION 3 (contract): lock down BOTH layers of "轮".

    Layer A (per tick): ``max_sources_per_round`` bounds ONE ``run_once()`` call,
    so every tick fetches at most N. Here N=2 and each tick fetches exactly 2.
    Layer B (per round): ``round_number`` counts ONE FULL PASS over all enabled
    sources; that pass spans 3 ticks and cumulatively fetches 6 > N sources.

    This is the agreed reading of the user's literal "每轮最多抓 N 个": N caps
    per CHECK, not per FULL PASS (README 第 35-37 行已明确两层含义). Two
    different meanings must not be conflated by future maintainers.
    """

    ids = ["a", "b", "c", "d", "e", "f"]
    seed(factory, [source(i) for i in ids])
    requested: list[str] = []
    scheduler = make_scheduler(factory, 2, requested, now=Clock())

    observed = asyncio.run(collect(scheduler, 3))

    assert [len(results) for results, _ in observed] == [2, 2, 2]
    assert all(len(results) <= 2 for results, _ in observed)  # Layer A
    assert requested == ids
    final = observed[-1][1]
    assert final.round_number == 1  # one single full pass (Layer B)
    assert final.round_fetched == 6 > 2  # cumulative can exceed N
    assert final == RoundSnapshot(1, 2, 6, 0, 6, 6, True)


# ---------------------------------------------------------------------------
# 5. Invalid N: constructor, CLI and full parameter plumbing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(0, id="zero"),
        pytest.param(-1, id="negative"),
        pytest.param(1.5, id="float"),
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="inf"),
        pytest.param(Decimal("15"), id="decimal"),
        pytest.param(True, id="true"),
        pytest.param(False, id="false"),
        pytest.param(10**9, id="billion"),
        pytest.param(10_001, id="over-ceiling"),
        pytest.param("15", id="str"),
        pytest.param(None, id="none"),
    ],
)
def test_scheduler_constructor_rejects_invalid_limits(
    factory: sessionmaker[Session], value: object
) -> None:
    with pytest.raises(ValueError, match="positive"):
        FetchScheduler(factory, max_sources_per_round=value)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "value",
    ["0", "-1", "-3", "10001", "1000000000", "1.5", "nan", "inf", "abc", ""],
)
def test_cli_rejects_invalid_limits_with_exit_code_2(
    value: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        run_module.main(["--max-sources-per-round", value])
    assert exc.value.code == 2
    # int-parseable values reach validate_max_sources_per_round -> "positive".
    # Non-integers are rejected earlier by argparse's type=int, still exit 2.
    if value in {"0", "-1", "-3", "10001", "1000000000"}:
        assert "positive" in capsys.readouterr().err.lower()


@pytest.mark.parametrize("value", ["1", "10000", str(DEFAULT_MAX_SOURCES_PER_ROUND)])
def test_cli_accepts_legal_boundary_limits(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite://")
    monkeypatch.setattr(run_module, "initialize_database", lambda *a, **k: None)
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    assert run_module.main(["--no-auto-fetch", "--max-sources-per-round", value]) == 0


def test_create_app_exposes_limit_and_all_round_fields_in_sync_status(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'status.sqlite'}"
    engine = create_db_engine(url)
    Base.metadata.create_all(engine)
    engine.dispose()

    client = TestClient(create_app(url, max_sources_per_round=3))
    response = client.get("/sync/status")
    assert response.status_code == 200
    status = response.json()
    assert_round_fields(status)
    assert status["max_sources_per_round"] == 3
    assert (status["round_number"], status["round_total"], status["round_pending"]) == (0, 0, 0)
    assert (status["round_fetched"], status["round_processed"]) == (0, 0)
    assert status["round_complete"] is True
    for legacy in (
        "enabled",
        "running",
        "current_source",
        "last_checked_at",
        "last_error",
        "revision",
        "failed_sources",
    ):
        assert legacy in status
    engine.dispose()


def test_create_web_app_propagates_limit_to_sync_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = f"sqlite:///{tmp_path / 'web-status.sqlite'}"
    engine = create_db_engine(url)
    Base.metadata.create_all(engine)
    engine.dispose()
    monkeypatch.setenv("DATABASE_URL", url)

    from server.web import create_web_app

    app = create_web_app(
        url,
        max_sources_per_round=3,
        auth_token="qa-token",
        auto_fetch=False,
        auto_backup=False,
        auto_cleanup=False,
        auto_body_fetch=False,
    )
    client = TestClient(app)
    client.headers["X-Local-Token"] = "qa-token"
    status = client.get("/sync/status").json()
    assert_round_fields(status)
    assert status["max_sources_per_round"] == 3
    assert status["round_complete"] is True
    engine.dispose()


def test_legacy_calls_default_to_fifteen(
    tmp_path: Path, factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    url = f"sqlite:///{tmp_path / 'legacy.sqlite'}"
    engine = create_db_engine(url)
    Base.metadata.create_all(engine)
    engine.dispose()
    monkeypatch.setenv("DATABASE_URL", url)

    client = TestClient(create_app(url))
    assert client.get("/sync/status").json()["max_sources_per_round"] == 15
    assert FetchScheduler(factory).round_status().max_sources_per_round == 15

    from server.web import create_web_app

    web_app = create_web_app(
        url,
        auth_token="qa-token",
        auto_fetch=False,
        auto_backup=False,
        auto_cleanup=False,
        auto_body_fetch=False,
    )
    client = TestClient(web_app)
    client.headers["X-Local-Token"] = "qa-token"
    assert client.get("/sync/status").json()["max_sources_per_round"] == 15
    engine.dispose()


def test_manual_fetch_does_not_open_or_advance_a_round(factory: sessionmaker[Session]) -> None:
    seed(factory, [source("a")])
    requested: list[str] = []
    scheduler = make_scheduler(factory, 2, requested, now=Clock())

    result = scheduler.fetch_now_sync("a")

    assert result.ok is True
    assert requested == ["a"]
    assert scheduler.round_status() == RoundSnapshot(0, 2, 0, 0, 0, 0, True)


async def _drain_with_snapshot(
    scheduler: FetchScheduler,
) -> tuple[list[FetchResult], RoundSnapshot]:
    results = await drain_round(scheduler)
    return results, scheduler.round_status()
