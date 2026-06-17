"""Unit tests for `gui._pick_next_perf` and the ⏪/⏩ state machine.

The pick helper is a pure function that mutates a `future` deque in place
but has no other side effects (no logging, no async, no DOM). The rest of
the Classical Shuffle loop is just wiring around it: handlers set
`replay_perf` / push onto `future`, the pick helper decides what to play
next, the caller pushes to `history` / `recency`. That's the whole state
machine, and it's all testable here.

Run standalone:
    python test_classical_shuffle_pick.py

Or with pytest:
    pytest test_classical_shuffle_pick.py

Each end-to-end test below mirrors a real scenario — the
2026-05-24 ⏪⏪⏪⏩ bug is the headline case at the bottom.
"""
import sys
import os
from collections import deque

sys.path.insert(0, os.path.dirname(__file__))

from gui import _pick_next_perf, PickResult, Performance


def _perf(name: str) -> Performance:
    """Build a Performance with just enough fields to be hashable + unique
    by `name`. The pick logic only reads playlist_url, artist, album for
    the recency key, so we use the same name across all three."""
    return Performance(
        playlist_url=f"url-{name}",
        playlist_name=f"playlist-{name}",
        artist=name,
        album=name,
        track_count=1,
        start_row=0,
        titles=(name.lower(),),
    )


# ---------------------------------------------------------------------------
# Priority 1: replay_perf set (⏪ replay).
# ---------------------------------------------------------------------------

def test_replay_perf_wins_over_future_and_pool():
    """When ⏪ has set replay_perf, the helper returns it unconditionally —
    `future` and `pool` are ignored. came_from_back=True signals to the
    caller that current_perf was already pushed to `future` by the ⏪
    handler, so caller must NOT also push to history."""
    a, b, c, d = _perf("A"), _perf("B"), _perf("C"), _perf("D")
    future = deque([d])
    pool = [a, b, c, d]
    result = _pick_next_perf(
        replay_perf=a, future=future, pool=pool, recency=None,
    )
    assert result == PickResult(
        perf=a, is_replay=True, came_from_back=True,
        log_msg=None, stop_reason=None,
    )
    # future was NOT touched (we didn't walk forward).
    assert list(future) == [d]


# ---------------------------------------------------------------------------
# Priority 2: future non-empty → forward walk (⏩ or natural-end).
# ---------------------------------------------------------------------------

def test_forward_walk_pops_right_most_from_future():
    """⏩ (or natural end-of-perf) with future=[E, D] picks D first
    (right-most = most-recently rewound past). is_replay=True so caller
    skips the recency push; came_from_back=False so caller DOES push
    previous current to history (we're moving forward past it)."""
    d, e = _perf("D"), _perf("E")
    future = deque([e, d])
    pool = [d, e]
    result = _pick_next_perf(
        replay_perf=None, future=future, pool=pool, recency=None,
    )
    assert result.perf == d
    assert result.is_replay is True
    assert result.came_from_back is False
    assert result.stop_reason is None
    assert "Walking forward to:" in (result.log_msg or "")
    assert "1 more queued" in (result.log_msg or "")
    assert list(future) == [e]


def test_forward_walk_log_message_counts_correctly():
    """The '(N more queued)' count should reflect future's length AFTER
    the pop, so the user sees how many more ⏩s would still walk forward."""
    perfs = [_perf(c) for c in "ABCDE"]
    future = deque(perfs)  # [A, B, C, D, E]
    result = _pick_next_perf(
        replay_perf=None, future=future, pool=perfs, recency=None,
    )
    assert result.perf == perfs[-1]  # E
    assert "4 more queued" in (result.log_msg or "")
    # Two pops in a row → "3 more queued", etc.
    r2 = _pick_next_perf(
        replay_perf=None, future=future, pool=perfs, recency=None,
    )
    assert r2.perf == perfs[-2]  # D
    assert "3 more queued" in (r2.log_msg or "")


def test_forward_walk_drains_dropped_perfs_off_top():
    """If a perf in future got bailout-dropped from pool (e.g., while we
    were on a ⏪ replay), it should be silently drained, not played."""
    a, dropped, c = _perf("A"), _perf("dropped"), _perf("C")
    # pool excludes `dropped` — it got bailout-removed
    pool = [a, c]
    future = deque([a, dropped])  # right-most (dropped) should be drained
    result = _pick_next_perf(
        replay_perf=None, future=future, pool=pool, recency=None,
    )
    assert result.perf == a
    assert list(future) == []  # `a` popped, `dropped` drained


def test_forward_walk_falls_back_to_random_when_future_fully_drained():
    """If every perf in future is dropped, the helper should fall through
    to the random-pick branch — not return None."""
    surviving = _perf("survivor")
    d1, d2 = _perf("dropped1"), _perf("dropped2")
    pool = [surviving]  # both d1 and d2 are no longer in pool
    future = deque([d1, d2])
    result = _pick_next_perf(
        replay_perf=None, future=future, pool=pool, recency=None,
    )
    assert result.perf == surviving
    assert result.is_replay is False  # fell through to random branch
    assert result.came_from_back is False
    assert list(future) == []


# ---------------------------------------------------------------------------
# Priority 3: random pick from pool minus recency.
# ---------------------------------------------------------------------------

def test_random_pick_when_future_and_replay_empty():
    a = _perf("A")
    result = _pick_next_perf(
        replay_perf=None, future=deque(), pool=[a], recency=None,
    )
    assert result.perf == a
    assert result.is_replay is False
    assert result.came_from_back is False
    assert result.log_msg is None
    assert result.stop_reason is None


def test_random_pick_respects_recency_window():
    """Perfs in recency are excluded from the eligible set."""
    a, b = _perf("A"), _perf("B")
    recency = deque([(a.playlist_url, a.artist.lower(), a.album.lower())])
    # A is in recency, only B is eligible
    result = _pick_next_perf(
        replay_perf=None, future=deque(), pool=[a, b], recency=recency,
    )
    assert result.perf == b


def test_random_pick_falls_back_to_full_pool_when_recency_covers_everyone():
    """If every perf in pool is in recency (shouldn't normally happen
    given the maxlen cap), fall back to random over the full pool —
    don't return None."""
    a = _perf("A")
    recency = deque([(a.playlist_url, a.artist.lower(), a.album.lower())])
    result = _pick_next_perf(
        replay_perf=None, future=deque(), pool=[a], recency=recency,
    )
    assert result.perf == a  # only choice


def test_empty_pool_returns_stop_reason():
    result = _pick_next_perf(
        replay_perf=None, future=deque(), pool=[], recency=None,
    )
    assert result.perf is None
    assert result.stop_reason == "pool_empty"


# ---------------------------------------------------------------------------
# End-to-end state-machine simulations.
# ---------------------------------------------------------------------------

class _StateMachine:
    """A tiny harness mirroring `_build_classical_shuffle_coroutine`'s
    state + ⏪/⏩ handlers. NOT a reimplementation — it calls the real
    `_pick_next_perf` and replicates ONLY the deque bookkeeping that
    lives in the action handlers and the post-pick code path. Lets us
    drive ⏪/⏩/natural-end sequences and assert state at every step.
    """

    def __init__(self, pool):
        self.pool = list(pool)
        self.history: deque = deque(maxlen=20)
        self.future: deque = deque(maxlen=20)
        self.current_perf = None
        self.replay_perf = None
        self.recency = None  # tests don't exercise recency here
        self.log: list[str] = []

    def _advance(self):
        """Run one pick step. Mirrors the post-pick code in the
        coroutine: assert pool not empty, push current to history if
        not came_from_back, then update current_perf."""
        pick = _pick_next_perf(
            replay_perf=self.replay_perf,
            future=self.future,
            pool=self.pool,
            recency=self.recency,
        )
        if pick.stop_reason:
            raise RuntimeError(f"pool exhausted: {pick.stop_reason}")
        self.replay_perf = None
        if pick.log_msg:
            self.log.append(pick.log_msg)
        if (not pick.came_from_back and self.current_perf is not None
                and self.current_perf in self.pool):
            self.history.append(self.current_perf)
        self.current_perf = pick.perf

    def natural_end(self):
        """A track drifted off → 2-poll streak → advance via pick logic
        (future-first, then random)."""
        self._advance()

    def skip_forward(self):
        """⏩ — identical to natural_end since neither sets replay_perf;
        the pick helper handles future-vs-random."""
        self._advance()

    def rewind(self):
        """⏪ handler — push current to future, pop history, set
        replay_perf, then advance."""
        # Belt-and-suspenders drain (matches coroutine code).
        while self.history and self.history[-1] not in self.pool:
            self.history.pop()
        if not self.history:
            self.log.append("  No previous performance to go back to.")
            return  # no-op
        target = self.history.pop()
        if self.current_perf is not None and self.current_perf in self.pool:
            self.future.append(self.current_perf)
        self.replay_perf = target
        self._advance()


def test_e2e_tonights_bug_repro():
    """The exact bug from the 2026-05-24 log: user listened to
    Bernstein Brahms 3, pressed ⏪⏪⏪ → Perlman/Mutter/Wiener replays,
    pressed ⏩ expecting to walk forward back to Mutter — old code
    picked a fresh random perf instead.

    With the fix: ⏩ pops from `future`, walking forward correctly.
    """
    p1, p3, p6, p7, p9 = (_perf(n) for n in ("p1", "p3", "p6", "p7", "p9"))
    sm = _StateMachine(pool=[p1, p3, p6, p7, p9])

    # Set initial state directly: pretend the user just finished
    # natural progression p1 → p3 → p6 → p7 and is now on p9. (We
    # can't drive 5 "random picks" through the helper deterministically
    # without mocking random.choice, and the state setup here is the
    # point of the test, not the random pick path.)
    sm.history.extend([p1, p3, p6, p7])
    sm.current_perf = p9
    assert list(sm.future) == []

    # ⏪⏪⏪ — walk backwards three steps.
    sm.rewind()  # → replay p7; push p9 to future
    assert sm.current_perf == p7
    assert list(sm.history) == [p1, p3, p6]
    assert list(sm.future) == [p9]

    sm.rewind()  # → replay p6; push p7 to future
    assert sm.current_perf == p6
    assert list(sm.history) == [p1, p3]
    assert list(sm.future) == [p9, p7]

    sm.rewind()  # → replay p3; push p6 to future
    assert sm.current_perf == p3
    assert list(sm.history) == [p1]
    assert list(sm.future) == [p9, p7, p6]

    # ⏩ — THE BUG. Old code picked a fresh random. New code walks
    # forward back through future: should be p6 next.
    sm.skip_forward()
    assert sm.current_perf == p6, (
        f"⏩ after ⏪⏪⏪ should walk forward to p6, got {sm.current_perf}"
    )
    assert list(sm.history) == [p1, p3]  # p3 (prev current) pushed back
    assert list(sm.future) == [p9, p7]

    # Two more ⏩s should walk forward through the rest of future.
    sm.skip_forward()
    assert sm.current_perf == p7
    assert list(sm.future) == [p9]

    sm.skip_forward()
    assert sm.current_perf == p9
    assert list(sm.future) == []

    # Now future is empty — next advance is a random pick. With a
    # single-element pool that'd just re-pick something. We don't
    # exercise that here; the point is that the forward walk worked.


def test_e2e_natural_end_drains_future_before_random():
    """If user ⏪'d mid-session and then just lets it play, the natural
    end-of-perf advance should walk forward through `future` before
    picking fresh. Mirrors the music-player model: ⏪ to mid-history,
    listen, and the queue continues forward through what was rewound."""
    a, b, c = _perf("A"), _perf("B"), _perf("C")
    sm = _StateMachine(pool=[a, b, c])
    # Initial state: user has listened to A, B, C in sequence.
    sm.history.extend([a, b])
    sm.current_perf = c
    sm.rewind()  # → b, future=[c]
    sm.rewind()  # → a, future=[c, b]
    assert sm.current_perf == a
    # Natural end (just let it play). Should walk forward to b, not pick random.
    sm.natural_end()
    assert sm.current_perf == b
    assert list(sm.future) == [c]


def test_e2e_rewind_at_empty_history_is_noop():
    a = _perf("A")
    sm = _StateMachine(pool=[a])
    sm.replay_perf = a; sm._advance()
    # No history yet — ⏪ should be a no-op, not crash.
    sm.rewind()
    assert sm.current_perf == a  # unchanged
    assert list(sm.future) == []  # nothing pushed
    assert any("No previous performance" in m for m in sm.log)


def test_e2e_dropped_perf_in_future_skipped_on_forward_walk():
    """If a perf gets bailout-dropped while sitting in `future`, ⏩
    should drain past it, not try to replay a known-broken perf."""
    a, broken, c = _perf("A"), _perf("broken"), _perf("C")
    sm = _StateMachine(pool=[a, broken, c])
    # Manually arrange: current=a, future=[c, broken] (broken is right-most).
    sm.current_perf = a
    sm.future.extend([c, broken])
    # Simulate bailout drop of `broken`.
    sm.pool.remove(broken)
    # ⏩ — drain `broken`, pop `c`.
    sm.skip_forward()
    assert sm.current_perf == c
    assert list(sm.future) == []  # broken drained, c popped
    # a (prev current) was pushed to history because came_from_back=False.
    assert list(sm.history) == [a]


# ---------------------------------------------------------------------------
# Self-runner.
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import traceback
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except Exception:
            print(f"  FAIL  {t.__name__}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
