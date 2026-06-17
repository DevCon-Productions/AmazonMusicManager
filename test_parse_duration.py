"""Unit tests for `gui._parse_duration_to_seconds`.

Pure function — string in, int seconds out. Used at pool-build time to
turn the Duration cell from each xlsx row into a number the
duration-target sampler can sum. Failure mode is "return 0" for any bad
input — never raise — because one corrupt cell shouldn't make the whole
pool unbuildable.

Run standalone:
    python test_parse_duration.py

Or with pytest:
    pytest test_parse_duration.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from gui import _parse_duration_to_seconds as p


# ---- Happy MM:SS ---------------------------------------------------------

def test_simple_mmss():
    assert p("11:52") == 11 * 60 + 52
    assert p("08:37") == 8 * 60 + 37
    assert p("00:30") == 30
    assert p("00:00") == 0


def test_mmss_single_digit_minutes():
    # Amazon Music shows zero-padded ("08:37"), but be defensive about
    # any future format change to "8:37".
    assert p("8:37") == 8 * 60 + 37
    assert p("0:30") == 30


def test_long_track_mmss():
    # A single 1h+ track might still display as MM:SS (e.g. 89:30) on
    # Amazon — be tolerant. Larger minute values are valid here.
    assert p("99:59") == 99 * 60 + 59
    assert p("60:00") == 60 * 60


# ---- Happy HH:MM:SS / H:MM:SS -------------------------------------------

def test_hhmmss():
    assert p("1:02:30") == 3600 + 2 * 60 + 30
    assert p("1:00:00") == 3600
    assert p("01:02:30") == 3600 + 2 * 60 + 30
    assert p("10:00:00") == 36000


# ---- Empty / None / whitespace ------------------------------------------

def test_empty_string():
    assert p("") == 0


def test_none():
    assert p(None) == 0


def test_whitespace_only():
    assert p("   ") == 0
    assert p("\t\n") == 0


def test_leading_trailing_whitespace_is_stripped():
    # Amazon's HTML scrape uses .trim() already, but xlsx cells could
    # have stray whitespace if the user hand-edited a row.
    assert p("  11:52  ") == 11 * 60 + 52
    assert p("\t08:37\n") == 8 * 60 + 37


# ---- Malformed input — return 0, never raise -----------------------------

def test_no_colon():
    assert p("712") == 0  # just seconds; ambiguous, refuse to guess
    assert p("11.52") == 0  # period not colon


def test_too_many_segments():
    assert p("1:2:3:4") == 0


def test_non_numeric_segments():
    assert p("ab:cd") == 0
    assert p("11:xx") == 0
    assert p("garbage") == 0


def test_negative_segments():
    # If someone hand-edited a cell to "-1:30", we don't want to
    # subtract 30 from somewhere.
    assert p("-1:30") == 0
    assert p("11:-30") == 0


def test_single_colon_with_empty_part():
    # ":52" or "11:" — unparseable, return 0.
    assert p(":52") == 0
    assert p("11:") == 0


# ---- Real classical samples (sanity) ------------------------------------

def test_dvorak_sym9_movements():
    # From the 2026-06-03 backfill smoke test — values verified against
    # the Amazon Music DOM ground truth.
    assert p("08:37") == 517   # Szell mvt I
    assert p("12:08") == 728   # Szell mvt II
    assert p("11:52") == 712   # Berliner Phil mvt IV (our Step 3 target)


def test_dvorak_sym9_total_perf():
    # Sum the 4 Berliner Phil mvts (09:24, 13:05, 08:05, 11:52) =
    # 9*60+24 + 13*60+5 + 8*60+5 + 11*60+52 = 564+785+485+712 = 2546 sec
    # = ~42:26 — a typical Dvorak Sym 9 length, sanity check.
    total = p("09:24") + p("13:05") + p("08:05") + p("11:52")
    assert total == 2546
    assert 2400 <= total <= 2700  # 40-45 min range


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
