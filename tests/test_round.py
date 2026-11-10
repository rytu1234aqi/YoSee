"""Pure, IO-free invariants of the round bookkeeping module.

These tests exercise ``server.ingestion.round`` directly: no database, no HTTP,
no event loop. They lock down validation, immutability, cursor semantics and
the exact numbers behind the seven ``RoundSnapshot`` fields.
"""

import dataclasses
from decimal import Decimal

import pytest
from server.ingestion.round import (
    DEFAULT_MAX_SOURCES_PER_ROUND,
    MAX_SOURCES_PER_ROUND_CEILING,
    RoundSnapshot,
    RoundTracker,
    TickPlan,
    validate_max_sources_per_round,
)

LEGAL_LIMITS = (1, 2, 3, 15, 9999, MAX_SOURCES_PER_ROUND_CEILING)

ILLEGAL_LIMITS = [
    pytest.param(0, id="zero"),
    pytest.param(-1, id="negative"),
    pytest.param(-100, id="large-negative"),
    pytest.param(1.5, id="fractional-float"),
    pytest.param(2.0, id="integral-float"),
    pytest.param(float("nan"), id="nan"),
    pytest.param(float("inf"), id="inf"),
    pytest.param(float("-inf"), id="neg-inf"),
    pytest.param(Decimal("15"), id="decimal"),
    pytest.param("15", id="str"),
    pytest.param("", id="empty-str"),
    pytest.param(True, id="true"),
    pytest.param(False, id="false"),
    pytest.param(10**9, id="billion"),
    pytest.param(MAX_SOURCES_PER_ROUND_CEILING + 1, id="ceiling-plus-one"),
    pytest.param(None, id="none"),
    pytest.param([], id="list"),
    pytest.param({}, id="dict"),
]


def test_named_constants_are_the_documented_defaults() -> None:
    assert DEFAULT_MAX_SOURCES_PER_ROUND == 15
    assert MAX_SOURCES_PER_ROUND_CEILING == 10_000
    assert validate_max_sources_per_round(DEFAULT_MAX_SOURCES_PER_ROUND) == 15


@pytest.mark.parametrize("value", LEGAL_LIMITS)
def test_validate_accepts_inclusive_legal_bounds(value: int) -> None:
    assert validate_max_sources_per_round(value) == value
    assert RoundTracker(value).limit == value


@pytest.mark.parametrize("value", ILLEGAL_LIMITS)
def test_validate_rejects_every_illegal_shape(value: object) -> None:
    with pytest.raises(ValueError, match="positive"):
        validate_max_sources_per_round(value)
    with pytest.raises(ValueError, match="positive"):
        RoundTracker(value)  # type: ignore[arg-type]


def test_round_snapshot_is_immutable_with_seven_fields() -> None:
    snapshot = RoundSnapshot(
        round_number=1,
        max_sources_per_round=2,
        round_total=5,
        round_pending=3,
        round_fetched=2,
        round_processed=2,
        round_complete=False,
    )
    assert len(dataclasses.fields(snapshot)) == 7
    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.round_number = 99  # type: ignore[misc]
    replaced = dataclasses.replace(snapshot, round_number=7)
    assert replaced.round_number == 7
    assert snapshot.round_number == 1


def test_tick_plan_is_immutable_with_tuple_fetch_ids() -> None:
    plan = TickPlan(round_number=2, fetch_ids=("a", "b"))
    assert isinstance(plan.fetch_ids, tuple)
    assert len(dataclasses.fields(plan)) == 2
    with pytest.raises(dataclasses.FrozenInstanceError):
        plan.round_number = 3  # type: ignore[misc]


def test_fresh_tracker_reports_idle_state() -> None:
    """All four counters are 0 ONLY when no round has ever been opened."""

    tracker = RoundTracker(3)
    assert tracker.limit == 3
    assert tracker.snapshot() == RoundSnapshot(
        round_number=0,
        max_sources_per_round=3,
        round_total=0,
        round_pending=0,
        round_fetched=0,
        round_processed=0,
        round_complete=True,
    )


def test_empty_targets_never_open_a_round() -> None:
    """Empty targets on a FRESH tracker: no round, no counters, no new round.

    Do not generalise the all-zero result: after a round has completed, the
    snapshot keeps the most recent round's statistics (see the integration
    test ``test_empty_targets_after_completed_round_report_most_recent_round_statistics``).
    """

    tracker = RoundTracker(2)
    targets: list[str] = []
    due: set[str] = set()
    for _ in range(3):
        plan = tracker.plan_tick(targets, due)
        assert plan.round_number == 0
        assert plan.fetch_ids == ()
        assert tracker.snapshot() == RoundSnapshot(0, 2, 0, 0, 0, 0, True)
    assert targets == []
    assert due == set()  # inputs are never mutated


def test_plan_tick_deduplicates_preserves_order_and_respects_budget() -> None:
    tracker = RoundTracker(2)
    targets = ["c", "a", "c", "b", "a", "d"]
    due = {"a", "b", "c", "d"}
    first = tracker.plan_tick(targets, due)
    assert first.round_number == 1
    assert first.fetch_ids == ("c", "a")  # dict.fromkeys keeps first-seen order
    assert isinstance(first.fetch_ids, tuple)
    assert len(first.fetch_ids) == len(set(first.fetch_ids)) <= 2
    assert tracker.snapshot() == RoundSnapshot(1, 2, 4, 2, 2, 2, False)

    second = tracker.plan_tick(targets, due)
    assert second.round_number == 1
    assert second.fetch_ids == ("b", "d")
    assert tracker.snapshot() == RoundSnapshot(1, 2, 4, 0, 4, 4, True)
    assert targets == ["c", "a", "c", "b", "a", "d"]
    assert due == {"a", "b", "c", "d"}


def test_not_due_targets_are_covered_without_consuming_budget() -> None:
    tracker = RoundTracker(2)
    targets = ["a", "b", "c", "d", "e"]
    plan = tracker.plan_tick(targets, {"e"})
    assert plan.fetch_ids == ("e",)  # four not-due ids skipped, only one fetched
    # Skipped sources still advance the cursor: the round finishes in one tick.
    assert tracker.snapshot() == RoundSnapshot(1, 2, 5, 0, 1, 5, True)


def test_counters_are_round_cumulative_and_reset_on_new_round() -> None:
    tracker = RoundTracker(2)
    targets = ["a", "b", "c", "d", "e"]
    due = set(targets)
    assert tracker.plan_tick(targets, due).fetch_ids == ("a", "b")
    assert tracker.snapshot() == RoundSnapshot(1, 2, 5, 3, 2, 2, False)
    assert tracker.plan_tick(targets, due).fetch_ids == ("c", "d")
    assert tracker.snapshot() == RoundSnapshot(1, 2, 5, 1, 4, 4, False)
    assert tracker.plan_tick(targets, due).fetch_ids == ("e",)
    assert tracker.snapshot() == RoundSnapshot(1, 2, 5, 0, 5, 5, True)

    # A new round resets the cumulative counters but keeps round_total (5 ids).
    restart = tracker.plan_tick(targets, due)
    assert restart.round_number == 2
    assert restart.fetch_ids == ("a", "b")
    assert tracker.snapshot() == RoundSnapshot(2, 2, 5, 3, 2, 2, False)


def test_round_number_only_advances_when_a_new_round_opens() -> None:
    tracker = RoundTracker(1)
    assert tracker.plan_tick([], set()).round_number == 0
    assert tracker.plan_tick(["a"], {"a"}).round_number == 1
    assert tracker.snapshot().round_complete is True  # single target finished
    assert tracker.plan_tick(["a"], {"a"}).round_number == 2
    assert tracker.snapshot().round_complete is True
    assert tracker.snapshot().round_number == 2


def test_targets_are_locked_for_the_whole_round() -> None:
    tracker = RoundTracker(1)
    due = {"a", "b", "c", "d"}
    assert tracker.plan_tick(["a", "b", "c"], due).fetch_ids == ("a",)
    # Mid-round: b/c gone, d added. The locked list still yields b, not d.
    assert tracker.plan_tick(["a", "d"], due).fetch_ids == ("b",)
    assert tracker.snapshot().round_complete is False
    assert tracker.plan_tick(["a", "d"], due).fetch_ids == ("c",)
    assert tracker.snapshot() == RoundSnapshot(1, 1, 3, 0, 3, 3, True)
    # Only now does the new source list become the next round's targets.
    fresh = tracker.plan_tick(["a", "d"], due)
    assert fresh.round_number == 2
    assert fresh.fetch_ids == ("a",)
    assert tracker.snapshot().round_total == 2


def test_source_disabled_mid_round_is_skipped_without_stalling() -> None:
    tracker = RoundTracker(1)
    assert tracker.plan_tick(["a", "b", "c", "d"], {"a", "b", "c", "d"}).fetch_ids == ("a",)
    # d disabled mid-round; it stays in the locked list but is no longer due.
    assert tracker.plan_tick(["a", "b", "c"], {"b", "c"}).fetch_ids == ("b",)
    assert tracker.plan_tick(["a", "b", "c"], {"c"}).fetch_ids == ("c",)
    assert tracker.plan_tick(["a", "b", "c"], set()).fetch_ids == ()
    assert tracker.snapshot() == RoundSnapshot(1, 1, 4, 0, 3, 4, True)


def test_snapshot_invariants_hold_at_every_tick() -> None:
    tracker = RoundTracker(2)
    targets = [f"s{i}" for i in range(7)]
    due = set(targets)
    for _ in range(4):
        plan = tracker.plan_tick(targets, due)
        snap = tracker.snapshot()
        assert len(plan.fetch_ids) == len(set(plan.fetch_ids)) <= tracker.limit
        assert snap.round_pending + snap.round_processed == snap.round_total
        assert 0 <= snap.round_fetched <= snap.round_processed
        assert snap.round_complete is (snap.round_pending == 0)
    assert tracker.snapshot() == RoundSnapshot(1, 2, 7, 0, 7, 7, True)
