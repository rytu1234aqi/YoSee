"""Pure, IO-free bookkeeping for round-based source polling.

A *round* walks through every enabled source exactly once. A round is split
into one or more ticks (one ``FetchScheduler.run_once()`` call per tick), and a
tick fetches at most ``max_sources_per_round`` sources. The cursor and the
per-round counters live in memory for the lifetime of the process: they are
deliberately **not** persisted, because ``next_fetch_at()`` already stores the
durable "do not fetch again before this time" decision in the database.
"""

from dataclasses import dataclass

# The stream view shows roughly 15 sources per "换一批" batch, while production
# ships 68 sources. Fifteen keeps one tick comparable to what a user sees and
# spreads a full pass over ~5 ticks; it is also >= the number of due sources in
# every existing scheduler test, so callers that omit the argument behave just
# like before. It is a named default, not a hardcoded limit: the value travels
# from the CLI through create_web_app/create_app into FetchScheduler.
DEFAULT_MAX_SOURCES_PER_ROUND = 15

# Guard against a mistyped huge value silently turning the limit off. Ten
# thousand is far above any realistic source list (68 today).
MAX_SOURCES_PER_ROUND_CEILING = 10_000


def validate_max_sources_per_round(value: object) -> int:
    """Return ``value`` as a usable per-round limit, or raise ``ValueError``.

    Both the CLI and ``FetchScheduler`` call this, so a bad value is rejected
    with the same plain-language message at the outermost entry point and at
    the constructor. Every message names the parameter and contains
    ``positive``, and ``bool`` is rejected even though it subclasses ``int``.
    Floats (including NaN and infinity), decimals and strings are not accepted;
    zero/negative values and implausibly large ones are rejected too.

    Args:
        value: Candidate limit supplied by a caller or by argparse.

    Returns:
        The validated integer limit.

    Raises:
        ValueError: If ``value`` is not a whole number in
            ``1..MAX_SOURCES_PER_ROUND_CEILING``.
    """

    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            "max_sources_per_round must be a positive whole number like 15 "
            f"(got {type(value).__name__}); decimals, NaN and infinity are not allowed"
        )
    if value <= 0:
        raise ValueError(
            f"max_sources_per_round must be a positive whole number >= 1 (got {value})"
        )
    if value > MAX_SOURCES_PER_ROUND_CEILING:
        raise ValueError(
            "max_sources_per_round must be a positive whole number no larger than "
            f"{MAX_SOURCES_PER_ROUND_CEILING}; the given value is too large"
        )
    return value


@dataclass(frozen=True)
class RoundSnapshot:
    """Point-in-time, observable state of the most recent round.

    ``round_complete`` is ``True`` while no round is in progress, i.e. the next
    tick will start a fresh round. It is ``False`` only while a round still has
    uncovered targets. ``round_number`` starts at ``0`` (no round has run yet)
    and increases by one whenever a new round opens.

    Once a round completes, its counters stay visible until the next round
    opens: they are not reset to zero, so a snapshot taken between rounds
    describes the round that just finished. The four counters
    (``round_total``/``round_pending``/``round_fetched``/``round_processed``)
    are all ``0`` only when the process has never opened a round at all.

    ``round_fetched`` counts the sources this round *decided* to fetch (fetch
    attempts, including failures), not successful fetches. It is incremented
    inside ``plan_tick`` when an id is selected, before any network work.
    """

    round_number: int
    max_sources_per_round: int
    round_total: int
    round_pending: int
    # Fetch attempts decided this round (including failures), not successes.
    round_fetched: int
    round_processed: int
    round_complete: bool


@dataclass(frozen=True)
class TickPlan:
    """The work one ``run_once()`` tick should perform."""

    round_number: int
    fetch_ids: tuple[str, ...]


class RoundTracker:
    """Track which enabled sources have been covered in the current round.

    ``targets`` is the ordered list of enabled source ids at the start of a
    tick; ``due_ids`` is the subset that ``next_fetch_at()`` says may be fetched
    right now. A source that is not due (or that disappeared since the round
    started) is still *covered*: the cursor advances past it without consuming
    the per-tick fetch budget. Otherwise a freshly fetched source would stay
    not-due for a whole interval and no round could ever finish.

    A round completes when the cursor reaches the end of the locked target
    list. The next tick opens a fresh round and re-walks the *full* list, so a
    source that only becomes due later is picked up in that later round instead
    of being starved behind the first ``N`` ids. Fetch failures also count as
    processed (the scheduler decides to fetch them, then records the failure),
    so one broken source cannot stall a round.

    The target list is locked when a round opens. Sources added mid-round join
    the next round; sources disabled or deleted mid-round are simply skipped.

    An empty target list opens no new round, fetches nothing and leaves
    ``round_number`` unchanged; the snapshot keeps the most recent round's
    statistics. This is the normal state when every site has been disabled,
    not a bug.
    """

    def __init__(self, max_sources_per_round: int = DEFAULT_MAX_SOURCES_PER_ROUND) -> None:
        self._limit = validate_max_sources_per_round(max_sources_per_round)
        self._round_number = 0
        self._targets: tuple[str, ...] = ()
        self._cursor = 0
        self._fetched = 0
        self._active = False
        self._complete = True  # idle: no round in progress, nothing pending

    @property
    def limit(self) -> int:
        """The configured per-tick fetch ceiling."""

        return self._limit

    def _start_round(self, targets: tuple[str, ...]) -> None:
        """Open a new round over a fresh snapshot of the enabled sources."""

        self._round_number += 1
        self._targets = targets
        self._cursor = 0
        self._fetched = 0
        self._active = True
        self._complete = False

    def plan_tick(self, targets: list[str], due_ids: set[str]) -> TickPlan:
        """Advance the cursor once and return at most ``limit`` ids to fetch.

        Args:
            targets: Enabled source ids, ordered by id, as read this tick.
            due_ids: Ids whose ``next_fetch_at()`` allows a fetch right now.

        Returns:
            A plan holding the ids to fetch. Skipped (not-due or gone) ids are
            covered by the cursor but never appear here, so
            ``len(fetch_ids) <= limit`` always holds. An empty target list
            returns an empty plan without opening a round.
        """

        ordered = tuple(dict.fromkeys(targets))
        if not self._active:
            if not ordered:
                # No enabled sources: nothing to fetch, no round to open, and
                # the round number stays put until a source appears again.
                return TickPlan(round_number=self._round_number, fetch_ids=())
            self._start_round(ordered)

        fetch_ids: list[str] = []
        while self._cursor < len(self._targets):
            source_id = self._targets[self._cursor]
            self._cursor += 1
            if source_id in due_ids:
                fetch_ids.append(source_id)
                self._fetched += 1
                if len(fetch_ids) >= self._limit:
                    break
            # Not due (or no longer enabled): skipped, but still covered. It
            # does not consume the fetch budget and cannot block the round.

        if self._cursor >= len(self._targets):
            self._complete = True
            self._active = False
        return TickPlan(round_number=self._round_number, fetch_ids=tuple(fetch_ids))

    def snapshot(self) -> RoundSnapshot:
        """Return a point-in-time view of the round for ``/sync/status``."""

        total = len(self._targets)
        return RoundSnapshot(
            round_number=self._round_number,
            max_sources_per_round=self._limit,
            round_total=total,
            round_pending=max(0, total - self._cursor),
            round_fetched=self._fetched,
            round_processed=min(self._cursor, total),
            round_complete=self._complete,
        )
