"""Unit tests for `gui._sample_for_duration_target`.

Pure function — (pool: list[Performance], target_seconds: int, rng) →
list[Performance]. Used by the Classical Shuffle playlist generator
(Step 5 — see [[project_classical_shuffle_playlist_gen_plan]]) to pick
a random set of performances whose summed durations meet or exceed the
user's target. One overshoot allowed; no per-pick cap (decided
2026-06-04).

Run standalone:
    python test_sample_for_duration_target.py

Or with pytest:
    pytest test_sample_for_duration_target.py
"""
import sys
import os
import random

sys.path.insert(0, os.path.dirname(__file__))

from gui import Performance, _sample_for_duration_target


# ---- Helpers -------------------------------------------------------------

def _perf(label: str, duration_seconds: int) -> Performance:
    """Build a minimal Performance with a unique label so we can assert
    no-duplicates without ambiguity. Only the fields exercised by the
    sampler need to be meaningful — others get harmless defaults."""
    return Performance(
        playlist_url=f"url://{label}",
        playlist_name=label,
        artist="<artist>",
        album=label,
        track_count=1,
        start_row=0,
        titles=(label.lower(),),
        artist_tokens=frozenset(),
        duration_seconds=duration_seconds,
    )


# ---- Empty / zero-target edge cases --------------------------------------

def test_empty_pool_returns_empty():
    assert _sample_for_duration_target([], 3600, rng=random.Random(0)) == []


def test_zero_target_returns_empty():
    # "User asked for 0 hours" — produce nothing rather than one random pick.
    pool = [_perf("A", 600), _perf("B", 900)]
    assert _sample_for_duration_target(pool, 0, rng=random.Random(0)) == []


def test_negative_target_returns_empty():
    # Defensive — UI never produces negative, but the function shouldn't
    # explode if a caller misuses it.
    pool = [_perf("A", 600)]
    assert _sample_for_duration_target(pool, -10, rng=random.Random(0)) == []


# ---- Single-overshoot behavior ------------------------------------------

def test_single_perf_already_exceeds_target():
    # Target 30 min, only one perf at ~80 min. One overshoot allowed,
    # so we still pick it. (Mirrors the Mahler-2-closing-3h-target case
    # in miniature.)
    p = _perf("Mahler2", 80 * 60)
    out = _sample_for_duration_target([p], 30 * 60, rng=random.Random(0))
    assert out == [p]


def test_stops_after_first_pick_that_crosses_threshold():
    # Five 10-minute perfs, target 25 minutes. The 3rd pick takes us to
    # 30 min (overshoot of 5 min) — we MUST stop there, not keep filling.
    pool = [_perf(f"P{i}", 10 * 60) for i in range(5)]
    out = _sample_for_duration_target(pool, 25 * 60, rng=random.Random(42))
    assert len(out) == 3
    total = sum(p.duration_seconds for p in out)
    assert total >= 25 * 60
    # And no second overshoot — the prior cumulative was below target.
    prior = sum(p.duration_seconds for p in out[:-1])
    assert prior < 25 * 60


def test_target_exactly_met_no_overshoot():
    # Three 20-min perfs, target exactly 60 min. Cumulative hits 60 at the
    # 3rd pick — we return all 3 and stop (>= threshold is met).
    pool = [_perf("A", 20 * 60), _perf("B", 20 * 60), _perf("C", 20 * 60)]
    out = _sample_for_duration_target(pool, 60 * 60, rng=random.Random(0))
    assert len(out) == 3
    assert sum(p.duration_seconds for p in out) == 60 * 60


# ---- Pool too small -----------------------------------------------------

def test_pool_total_less_than_target_returns_whole_pool():
    # Three 10-min perfs total = 30 min, target = 60 min. We never reach
    # target; return everything we have. The dialog will show a "pool too
    # short" message based on the sum after the call.
    pool = [_perf("A", 10 * 60), _perf("B", 10 * 60), _perf("C", 10 * 60)]
    out = _sample_for_duration_target(pool, 60 * 60, rng=random.Random(0))
    assert len(out) == 3
    assert set(out) == set(pool)


# ---- Determinism + no-duplicates ----------------------------------------

def test_deterministic_with_seeded_rng():
    # Same seed → same picks in the same order.
    pool = [_perf(f"P{i}", 5 * 60) for i in range(20)]
    a = _sample_for_duration_target(pool, 30 * 60, rng=random.Random(123))
    b = _sample_for_duration_target(pool, 30 * 60, rng=random.Random(123))
    assert a == b


def test_different_seeds_produce_different_picks():
    # Sanity — without this, we couldn't tell if shuffle was even running.
    # With 20 perfs and target 30 min, the chance of two different seeds
    # producing identical picks is vanishingly small.
    pool = [_perf(f"P{i}", 5 * 60) for i in range(20)]
    a = _sample_for_duration_target(pool, 30 * 60, rng=random.Random(1))
    b = _sample_for_duration_target(pool, 30 * 60, rng=random.Random(99))
    assert a != b


def test_no_duplicates_in_result():
    # Without-replacement: each picked perf must appear exactly once in
    # the result.
    pool = [_perf(f"P{i}", 5 * 60) for i in range(20)]
    out = _sample_for_duration_target(pool, 45 * 60, rng=random.Random(7))
    assert len(out) == len(set(out))


# ---- Zero-duration tolerance --------------------------------------------

def test_zero_duration_perfs_do_not_break_loop():
    # A perf whose duration_seconds is 0 (e.g. an xlsx exported before
    # the duration-capture column existed) must NOT cause an infinite-ish
    # walk or a divide-by-zero. The loop just keeps drawing until a real-
    # duration perf trips the threshold.
    pool = [
        _perf("Z1", 0), _perf("Z2", 0), _perf("Z3", 0),
        _perf("Real", 30 * 60),
    ]
    out = _sample_for_duration_target(pool, 20 * 60, rng=random.Random(0))
    # We must include the real perf (only one that contributes any time)
    # and may include some of the zero-duration ones along the way.
    assert any(p.album == "Real" for p in out)
    assert sum(p.duration_seconds for p in out) == 30 * 60


def test_all_zero_duration_returns_whole_pool():
    # Pool total = 0 < any positive target → degenerate "pool too short"
    # case. Walks the entire pool and never trips, returns everything.
    pool = [_perf(f"Z{i}", 0) for i in range(4)]
    out = _sample_for_duration_target(pool, 60 * 60, rng=random.Random(0))
    assert set(out) == set(pool)
    assert sum(p.duration_seconds for p in out) == 0


# ---- Default-rng smoke (module-level random, no seed) -------------------

def test_default_rng_does_not_crash():
    # Don't assert order — just that calling without an rng argument works.
    pool = [_perf(f"P{i}", 5 * 60) for i in range(10)]
    out = _sample_for_duration_target(pool, 15 * 60)
    assert sum(p.duration_seconds for p in out) >= 15 * 60


# ---- Realistic classical sanity check -----------------------------------

def test_realistic_three_hour_target():
    # A mini classical pool with plausible perf lengths. Target 3 h.
    # Spot-check the sampler returns enough perfs to plausibly meet the
    # target without becoming the whole pool.
    pool = [
        _perf("Beethoven Sym 5", 35 * 60),
        _perf("Beethoven Sym 9", 65 * 60),
        _perf("Mahler 2", 80 * 60),
        _perf("Mahler 5", 70 * 60),
        _perf("Dvorak Sym 9", 42 * 60),
        _perf("Brahms VC", 38 * 60),
        _perf("Schubert Sym 9", 55 * 60),
        _perf("Grieg PC", 30 * 60),
        _perf("Rach PC 2", 32 * 60),
        _perf("Tchaik PC 1", 35 * 60),
    ]
    out = _sample_for_duration_target(pool, 3 * 3600, rng=random.Random(2026))
    total = sum(p.duration_seconds for p in out)
    assert total >= 3 * 3600
    # Reasonable upper bound — even with one Mahler 2 overshoot, the
    # sampler should stop short of 4.5 h for this pool / target.
    assert total < 4.5 * 3600
    # And without-replacement constraint must hold.
    assert len(out) == len(set(out))


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
