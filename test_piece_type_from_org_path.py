"""Unit tests for `gui._piece_type_from_org_path`.

Mirror of test_composer_from_org_path.py one level deeper in the
Classical hierarchy. The helper extracts the piece-type folder (e.g.
"Symphonies", "Violin Concertos") from an org-tree path. Used by the
Classical Shuffle playlist generator's piece-type multi-select filter.

Run standalone:
    python test_piece_type_from_org_path.py

Or with pytest:
    pytest test_piece_type_from_org_path.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from gui import _piece_type_from_org_path as f


# ---- Happy path ----------------------------------------------------------

def test_simple_classical_path():
    assert f(["Classical", "Beethoven", "Piano Sonatas", "Piano Sonata 28"]) == "Piano Sonatas"


def test_symphony_path():
    assert f(["Classical", "Mahler", "Symphonies", "Symphony 6"]) == "Symphonies"


def test_deeply_nested_path():
    # Bach is filed extra-deep — piece type is still the segment two below
    # "Classical" ("Cantatas"), regardless of how deep the leaf sits.
    assert f(["Classical", "Bach", "Cantatas", "Late Cantatas", "BWV 140"]) == "Cantatas"


def test_classical_not_at_root():
    # Defensive: nested-Classical layout still works (e.g., "Music/Classical/...").
    assert f(["Music", "Classical", "Beethoven", "Concertos"]) == "Concertos"


# ---- None cases ----------------------------------------------------------

def test_empty_path():
    assert f([]) is None


def test_path_without_classical():
    assert f(["Rock", "Limp Bizkit", "Hot Dog"]) is None


def test_classical_only():
    assert f(["Classical"]) is None


def test_classical_composer_only_no_piece_type():
    # Two-segment path stops at the composer — no piece-type subfolder yet.
    assert f(["Classical", "Beethoven"]) is None


def test_classical_as_last_segment():
    assert f(["Music", "Classical"]) is None


def test_empty_segment_for_piece_type():
    # Defensive: empty segment in the piece-type slot returns None instead
    # of "" (which would mis-tag).
    assert f(["Classical", "Beethoven", "", "Sonata 28"]) is None


def test_whitespace_segment_for_piece_type():
    assert f(["Classical", "Beethoven", "   ", "Sonata 28"]) is None


# ---- Case-sensitivity ----------------------------------------------------

def test_case_sensitive_match_is_required():
    # Org tree uses "Classical" (capital C) consistently. Helper does not
    # downcase — caller's responsibility to canonicalize.
    assert f(["classical", "Beethoven", "Symphonies"]) is None
    assert f(["CLASSICAL", "Beethoven", "Symphonies"]) is None


# ---- Multi-Classical case (handled at caller) ----------------------------

def test_only_first_classical_is_used():
    # If "Classical" appears twice, take the segment two below the FIRST.
    # list.index() returns the first match.
    assert f(["Classical", "Foo", "Bar"]) == "Bar"


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
