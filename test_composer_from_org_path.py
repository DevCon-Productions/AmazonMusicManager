"""Unit tests for `gui._composer_from_org_path`.

The helper is a pure function — single list[str] in, str|None out — with
no side effects. Easy and worth testing because the Classical-Shuffle
playlist generator (see [[project_classical_shuffle_playlist_gen_plan]])
calls it per-org-path to tag performances with their composer, and
mis-tagging would silently corrupt the composer multi-select filter.

Run standalone:
    python test_composer_from_org_path.py

Or with pytest:
    pytest test_composer_from_org_path.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from gui import _composer_from_org_path as f


# ---- Happy path ----------------------------------------------------------

def test_simple_classical_path():
    assert f(["Classical", "Beethoven", "Piano Sonatas", "Piano Sonata 28"]) == "Beethoven"


def test_minimal_two_segment_path():
    # "Classical / Mozart" — composer-only, no piece subtree yet.
    assert f(["Classical", "Mozart"]) == "Mozart"


def test_deeply_nested_path():
    # Bach is filed extra-deep ("Classical / Bach / Cantatas / Late Cantatas / BWV 140").
    # The helper must still return "Bach" — it stops at the segment right after
    # "Classical", not at the leaf.
    assert f(["Classical", "Bach", "Cantatas", "Late Cantatas", "BWV 140"]) == "Bach"


def test_classical_not_at_root():
    # Defensive: if some future org layout nests Classical under a parent
    # (e.g., "Music / Classical / Beethoven / ..."), the helper still works.
    # Plan: "walk path past Classical, return next segment."
    assert f(["Music", "Classical", "Beethoven", "Concertos"]) == "Beethoven"


# ---- None cases ----------------------------------------------------------

def test_empty_path():
    assert f([]) is None


def test_path_without_classical():
    assert f(["Rock", "Limp Bizkit", "Hot Dog"]) is None


def test_classical_only():
    # The "Classical" root folder itself — no composer beneath in this path.
    assert f(["Classical"]) is None


def test_classical_as_last_segment():
    # Nonsensical but defensible — no composer to extract.
    assert f(["Music", "Classical"]) is None


def test_empty_segment_after_classical():
    # Defensive: if the path has an empty segment immediately after Classical,
    # treat as no composer rather than returning "" (which would mis-tag).
    assert f(["Classical", "", "Beethoven"]) is None


def test_whitespace_segment_after_classical():
    # Same defensive rule for whitespace-only segments.
    assert f(["Classical", "   ", "Beethoven"]) is None


# ---- Case-sensitivity ----------------------------------------------------

def test_case_sensitive_match_is_required():
    # Org tree consistently uses "Classical" (capital C). The helper does not
    # downcase. If the tree ever uses "classical" instead, the caller's
    # responsibility is to canonicalize before calling — not this helper's.
    assert f(["classical", "Beethoven"]) is None
    assert f(["CLASSICAL", "Beethoven"]) is None


# ---- Multi-composer case (handled at caller, but document behavior) -----

def test_only_first_classical_is_used():
    # If a path somehow contains "Classical" twice (unusual but possible if
    # a composer's name is itself "Classical"), the helper takes the segment
    # after the FIRST occurrence. list.index() returns the first match.
    assert f(["Classical", "Classical", "Foo"]) == "Classical"


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
