"""Offline smoke for the 2026-06-13/14 Classical Shuffle generator fixes.

Runs entirely against cached xlsx — no browser, no login, no Amazon Music
traffic. Verifies:

  Bug 1 (grouper) — Arrau's Grieg PC + Schumann PC coupling album in
    Classical/Grieg/Piano Concerto in A Minor splits into two perfs
    (3-track Grieg + 3-track Schumann) instead of one 6-track glob.

  Bug 3 (anti-cluster) — On a wide Classical scope with a 1h target, run
    the sampler several times under different rng seeds; the spread pass
    should drive adjacent same-work pairs to zero (or to the
    mathematically-forced floor) in every iteration.

Bug 2 (web-restricted menu-hidden) needs a live Amazon-Music run and
isn't covered here.

Run:
    python smoke_classical_shuffle_fixes.py
"""
from __future__ import annotations

import os
import random
import sys

sys.path.insert(0, os.path.dirname(__file__))

from organization import load_org
from gui import (
    Performance,
    _build_classical_performance_pool,
    _piece_prefix,
    _sample_for_duration_target,
    _spread_adjacent_by_key,
)


def _work_key(p: Performance) -> str:
    return _piece_prefix(p.titles[0]) if p.titles else ""


def _adjacent_collisions(picks: list[Performance]) -> int:
    return sum(
        1 for a, b in zip(picks, picks[1:])
        if _work_key(a) and _work_key(a) == _work_key(b)
    )


def _min_possible_collisions(picks: list[Performance]) -> int:
    """If one work key strictly dominates the picks, some clustering is
    unavoidable. Returns max(0, 2 * max_bucket_count - n - 1)."""
    if len(picks) < 2:
        return 0
    keys = [k for k in (_work_key(p) for p in picks) if k]
    if not keys:
        return 0
    counts: dict[str, int] = {}
    for k in keys:
        counts[k] = counts.get(k, 0) + 1
    max_count = max(counts.values())
    n = len(picks)
    return max(0, 2 * max_count - n - 1)


def check_bug1_grieg_arrau_splits(pool: list[Performance]) -> bool:
    """The Arrau Grieg+Schumann coupling album should split into
    a 3-track Grieg perf + a 3-track Schumann perf, not one 6-track glob."""
    grieg_url_substring = "Grieg"
    arrau_perfs = [
        p for p in pool
        if "claudio arrau" in (p.artist or "").lower()
        and grieg_url_substring.lower() in (p.playlist_name or "").lower()
    ]
    print(f"\n[Bug 1] Arrau perfs in Grieg PC scope: {len(arrau_perfs)}")
    for p in arrau_perfs:
        first = p.titles[0] if p.titles else "<no title>"
        print(f"   - {p.track_count}-track perf, first title: {first!r}")

    if len(arrau_perfs) < 2:
        print("[Bug 1] FAIL — fewer than 2 Arrau perfs; expected the "
              "coupling album to split.")
        return False
    # Look for one Grieg perf (Op. 16 in first title) and one Schumann perf
    # (Op. 54 in first title), each with track_count = 3.
    grieg_perf = next(
        (p for p in arrau_perfs
         if p.titles and "op. 16" in p.titles[0].lower()),
        None,
    )
    schumann_perf = next(
        (p for p in arrau_perfs
         if p.titles and "op. 54" in p.titles[0].lower()),
        None,
    )
    if not grieg_perf:
        print("[Bug 1] FAIL — no Arrau perf starting at Grieg Op. 16.")
        return False
    if not schumann_perf:
        print("[Bug 1] FAIL — no Arrau perf starting at Schumann Op. 54.")
        return False
    if grieg_perf.track_count != 3:
        print(f"[Bug 1] FAIL — Grieg perf has {grieg_perf.track_count} "
              "tracks (expected 3).")
        return False
    if schumann_perf.track_count != 3:
        print(f"[Bug 1] FAIL — Schumann perf has {schumann_perf.track_count} "
              "tracks (expected 3).")
        return False
    print("[Bug 1] PASS — Arrau coupling album split as (3 Grieg, 3 Schumann).")
    return True


def check_bug3_anticluster(pool: list[Performance], target_seconds: int,
                           seeds=(1, 2, 3, 4, 5)) -> bool:
    """Sample with several seeds; in every case, the spread should drive
    adjacent same-work pairs to the math floor or below."""
    all_ok = True
    print(f"\n[Bug 3] target = {target_seconds // 60}m; trying "
          f"{len(seeds)} rng seeds:")
    for seed in seeds:
        rng = random.Random(seed)
        picked = _sample_for_duration_target(pool, target_seconds, rng=rng)
        before = _adjacent_collisions(picked)
        spread = _spread_adjacent_by_key(picked, _work_key)
        after = _adjacent_collisions(spread)
        floor = _min_possible_collisions(picked)
        verdict = "ok" if after <= floor else "FAIL"
        if verdict != "ok":
            all_ok = False
        # Multi-recording works present?
        work_counts: dict[str, int] = {}
        for p in picked:
            k = _work_key(p)
            if k:
                work_counts[k] = work_counts.get(k, 0) + 1
        multi = {k: c for k, c in work_counts.items() if c > 1}
        multi_str = (
            " " + " ".join(f"{k.split(':')[0][:18]}=x{c}" for k, c in multi.items())
            if multi else ""
        )
        print(f"   seed {seed}: picked {len(picked)} perfs, "
              f"adjacent same-work before={before} after={after} "
              f"(floor={floor}, {verdict}){multi_str}")
    if all_ok:
        print("[Bug 3] PASS — every seed reached the math floor.")
    else:
        print("[Bug 3] FAIL — spread left collisions above the math floor.")
    return all_ok


def main() -> int:
    print("Loading org…")
    org = load_org()

    print("Building Grieg pool…")
    grieg_pool, grieg_skipped, grieg_restricted = (
        _build_classical_performance_pool(org, ["Classical", "Grieg"])
    )
    print(f"   pool: {len(grieg_pool)} perfs, skipped: {len(grieg_skipped)}, "
          f"restricted-filtered: {grieg_restricted}")
    bug1_ok = check_bug1_grieg_arrau_splits(grieg_pool)

    print("\nBuilding full Classical pool…")
    classical_pool, classical_skipped, classical_restricted = (
        _build_classical_performance_pool(org, ["Classical"])
    )
    print(f"   pool: {len(classical_pool)} perfs, "
          f"skipped: {len(classical_skipped)}, "
          f"restricted-filtered: {classical_restricted}")
    if not classical_pool:
        print("[Bug 3] SKIP — full Classical pool is empty.")
        bug3_ok = True
    else:
        # 4h target gets us 6–10 picks against this pool; long enough to see
        # at least one multi-recording work appear in the sample under most
        # seeds. The pool has many works with multiple recordings (Beethoven
        # 5, Dvořák 9, Mahler 5, etc.), so seeds that produce zero adjacent
        # collisions don't exercise the spread but are still a healthy
        # sanity outcome.
        bug3_ok = check_bug3_anticluster(
            classical_pool, target_seconds=4 * 60 * 60,
            seeds=tuple(range(1, 21)),
        )

    # Bug 3 stress: a scope with known multi-recordings (Beethoven 8 has
    # Stockholm + Philadelphia + Tennstedt in one playlist). 30m target
    # forces the sampler to pick all three perfs (each is ~25-30m), so
    # the spread WILL have multiple same-work pairs to break up.
    print("\nBuilding Beethoven 8 pool for spread stress test…")
    beet8_pool, _, _ = _build_classical_performance_pool(
        org, ["Classical", "Beethoven", "Symphonies", "Symphony 8"],
    )
    print(f"   pool: {len(beet8_pool)} perfs")
    for p in beet8_pool:
        first = p.titles[0] if p.titles else "<none>"
        print(f"   - {p.track_count}-track  {p.artist!r}  first={first!r}")
    if len(beet8_pool) >= 2:
        # Inject a second-piece perf so the spread has *somewhere* to put
        # the dispersed Beethoven 8s — without it the only pool key is
        # Beethoven 8 and clustering is mathematically forced. Pull one
        # from the wider Classical pool whose work key differs.
        beet8_key = _work_key(beet8_pool[0])
        decoys = [
            p for p in classical_pool
            if _work_key(p) and _work_key(p) != beet8_key
        ][:4]
        stress_pool = beet8_pool + decoys
        print(f"   + {len(decoys)} decoy perfs from other works "
              f"({[d.playlist_name[:30] for d in decoys]})")
        # Long target so all multi-recording entries land in the sample.
        bug3_stress_ok = check_bug3_anticluster(
            stress_pool, target_seconds=10 * 60 * 60,
            seeds=tuple(range(1, 11)),
        )
    else:
        print("[Bug 3 stress] SKIP — not enough perfs in Beethoven 8 pool.")
        bug3_stress_ok = True

    print()
    print("=" * 60)
    print(f"Bug 1 (grouper coupling album):       "
          f"{'PASS' if bug1_ok else 'FAIL'}")
    print(f"Bug 3 (anti-cluster, Classical):      "
          f"{'PASS' if bug3_ok else 'FAIL'}")
    print(f"Bug 3 (anti-cluster, B8 stress):      "
          f"{'PASS' if bug3_stress_ok else 'FAIL'}")
    print("=" * 60)
    return 0 if bug1_ok and bug3_ok and bug3_stress_ok else 1


if __name__ == "__main__":
    sys.exit(main())
