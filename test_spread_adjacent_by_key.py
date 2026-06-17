"""Unit tests for ``_spread_adjacent_by_key`` — the post-sample anti-cluster
pass used by the Classical Shuffle playlist generator.

Goal: after uniform-random sampling, reorder so two recordings of the same
work don't land adjacent. Statistically possible under uniform sampling
when 3+ recordings of one piece exist in the pool; the user noticed it on
2026-06-12 with Stockholm Beethoven 8 immediately followed by Philadelphia
Beethoven 8.

Run standalone:
    python test_spread_adjacent_by_key.py

Or with pytest:
    pytest test_spread_adjacent_by_key.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from gui import _spread_adjacent_by_key


def _adjacent_collisions(out, key_fn):
    return sum(1 for a, b in zip(out, out[1:]) if key_fn(a) == key_fn(b))


def test_empty_returns_empty():
    assert _spread_adjacent_by_key([], lambda x: x) == []


def test_single_item_returns_single():
    assert _spread_adjacent_by_key(["a"], lambda x: x) == ["a"]


def test_all_unique_keys_no_op():
    items = ["a", "b", "c", "d"]
    out = _spread_adjacent_by_key(items, lambda x: x)
    assert out == items


def test_input_not_mutated():
    items = ["a", "a", "b"]
    snapshot = list(items)
    _spread_adjacent_by_key(items, lambda x: x)
    assert items == snapshot


def test_two_adjacent_same_key_broken():
    items = ["a", "a", "b"]
    out = _spread_adjacent_by_key(items, lambda x: x)
    assert _adjacent_collisions(out, lambda x: x) == 0
    assert sorted(out) == sorted(items)


def test_aabb_interleaves():
    items = ["a", "a", "b", "b"]
    out = _spread_adjacent_by_key(items, lambda x: x)
    assert _adjacent_collisions(out, lambda x: x) == 0


def test_three_keys_balanced_zero_collisions():
    items = ["a"] * 3 + ["b"] * 3 + ["c"] * 2
    out = _spread_adjacent_by_key(items, lambda x: x)
    assert _adjacent_collisions(out, lambda x: x) == 0
    assert sorted(out) == sorted(items)


def test_dominant_key_residual_collisions_accepted():
    """5 a's + 3 b's in 8 slots: max same > (n+1)//2 means some collisions
    are mathematically unavoidable. The optimal count is
    (2 * max_count - n - 1) = 2*5 - 8 - 1 = 1. The helper should hit that
    floor (not more) and never raise.
    """
    items = ["a"] * 5 + ["b"] * 3
    out = _spread_adjacent_by_key(items, lambda x: x)
    assert sorted(out) == sorted(items)
    assert _adjacent_collisions(out, lambda x: x) == 1


def test_extreme_dominance_one_key_only():
    """All items share one key — every adjacent pair must collide."""
    items = ["a"] * 4
    out = _spread_adjacent_by_key(items, lambda x: x)
    assert out == items
    assert _adjacent_collisions(out, lambda x: x) == 3


def test_deterministic_same_input_same_output():
    items = ["a", "b", "a", "c", "b", "a"]
    out1 = _spread_adjacent_by_key(items, lambda x: x)
    out2 = _spread_adjacent_by_key(items, lambda x: x)
    assert out1 == out2


def test_within_bucket_order_preserved():
    """Items sharing a key emit in their input order. Helps make the
    function predictable for downstream code that cares about (e.g.)
    perf identity tracking by sampler order.
    """
    items = [("a", 1), ("a", 2), ("a", 3), ("b", 1), ("b", 2)]
    out = _spread_adjacent_by_key(items, lambda x: x[0])
    # Filter to just the 'a' subsequence — should be (a,1), (a,2), (a,3).
    a_subseq = [x for x in out if x[0] == "a"]
    assert a_subseq == [("a", 1), ("a", 2), ("a", 3)]
    b_subseq = [x for x in out if x[0] == "b"]
    assert b_subseq == [("b", 1), ("b", 2)]


def test_stockholm_philadelphia_beethoven_8_real_shape():
    """The 2026-06-12 case. Sampler picked Stockholm Beethoven 8 then
    Philadelphia Beethoven 8 back-to-back from a pool that also held a
    Brahms 1, a Mahler 5, etc. Without this pass: two adjacent Beethoven 8s.
    With it: one of the other works goes between.
    """
    items = [
        ("stockholm beethoven 8", "beethoven: sym 8"),
        ("philadelphia beethoven 8", "beethoven: sym 8"),
        ("karajan brahms 1", "brahms: sym 1"),
        ("bernstein mahler 5", "mahler: sym 5"),
        ("solti dvorak 9", "dvorak: sym 9"),
    ]
    out = _spread_adjacent_by_key(items, lambda x: x[1])
    assert sorted(out) == sorted(items)
    assert _adjacent_collisions(out, lambda x: x[1]) == 0


def test_key_fn_called_once_per_item():
    """Defensive: the helper shouldn't re-invoke key_fn (which could be
    expensive for callers). Verified by a counter."""
    items = [object() for _ in range(20)]  # all unique by identity
    calls = {"count": 0}

    def key(it):
        calls["count"] += 1
        return id(it)

    _spread_adjacent_by_key(items, key)
    assert calls["count"] == len(items)


def test_empty_string_keys_treated_as_one_bucket():
    """Defensive: items whose key is "" all bucket together. Real Classical
    data essentially never produces this (every track has a colon-prefixed
    title), but generator code passes empty string for perfs with no titles
    rather than skipping them."""
    items = [
        ("p1", ""),
        ("p2", ""),
        ("p3", "real key"),
    ]
    out = _spread_adjacent_by_key(items, lambda x: x[1])
    # The two empty-keyed items should be split by the "real key" one
    # (or be at opposite ends).
    assert sorted(out) == sorted(items)


def test_long_realistic_classical_sample_no_collisions():
    """Realistic shape: 12 picks from 6 different works, 2 recordings of one
    work, 3 of another, singletons for the rest. Helper should reach zero
    collisions (max count = 3, n = 12, plenty of headroom)."""
    items = (
        [("a", "beethoven 8")] * 3
        + [("b", "brahms 1")] * 2
        + [("c", "mahler 5")]
        + [("d", "dvorak 9")]
        + [("e", "schubert 9")]
        + [("f", "haydn 104")]
        + [("g", "mozart 41")]
        + [("h", "tchaikovsky 6")]
        + [("i", "rachmaninoff 2")]
    )
    out = _spread_adjacent_by_key(items, lambda x: x[1])
    assert sorted(out) == sorted(items)
    assert _adjacent_collisions(out, lambda x: x[1]) == 0


if __name__ == "__main__":
    import inspect
    mod = sys.modules[__name__]
    tests = [
        (name, fn) for name, fn in inspect.getmembers(mod, inspect.isfunction)
        if name.startswith("test_")
    ]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ok  {name}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {name}: {e}")
        except Exception as e:
            failed += 1
            print(f"  ERR  {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
