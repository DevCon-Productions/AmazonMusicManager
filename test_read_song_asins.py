"""Unit tests for `gui._read_song_asins`.

Round-trips a tempfile xlsx through the exporter's header schema and
reads ASIN slices back via the new helper. Covers happy path, empty
cells, out-of-range slices, and missing-column behavior.

Step 6 of the playlist generator (see
[[project_classical_shuffle_playlist_gen_plan]]) calls this once per
sampled performance — getting it wrong silently drops tracks.

Run standalone:
    python test_read_song_asins.py

Or with pytest:
    pytest test_read_song_asins.py
"""
import os
import sys
import tempfile

from openpyxl import Workbook

sys.path.insert(0, os.path.dirname(__file__))

from gui import _read_song_asins as f


# Matches exporter.export_to_xlsx column order so the helper exercises
# the same shape it'll see in production xlsx files.
HEADERS_FULL = [
    "#", "Song Name", "Artist", "Album", "Release Date", "Genre",
    "Duration", "Track ASIN", "Album ASIN",
]


def _write_xlsx(rows: list[list], headers: list[str] = HEADERS_FULL) -> str:
    """Write a temp xlsx with *headers* + *rows* and return its path."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Tracks"
    for col_idx, h in enumerate(headers, 1):
        ws.cell(row=1, column=col_idx, value=h)
    for row_idx, row in enumerate(rows, 2):
        for col_idx, v in enumerate(row, 1):
            ws.cell(row=row_idx, column=col_idx, value=v)
    fd, path = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    wb.save(path)
    wb.close()
    return path


def _full_row(num, title, artist, album, track_asin, album_asin):
    return [num, title, artist, album, "", "", "", track_asin, album_asin]


# ---- Happy path ----------------------------------------------------------

def test_reads_full_slice():
    path = _write_xlsx([
        _full_row(1, "Mvt I", "Karajan", "Sym 9", "T1", "A1"),
        _full_row(2, "Mvt II", "Karajan", "Sym 9", "T2", "A1"),
        _full_row(3, "Mvt III", "Karajan", "Sym 9", "T3", "A1"),
        _full_row(4, "Mvt IV", "Karajan", "Sym 9", "T4", "A1"),
    ])
    try:
        assert f(path, 0, 4) == [
            ("T1", "A1"), ("T2", "A1"), ("T3", "A1"), ("T4", "A1"),
        ]
    finally:
        os.remove(path)


def test_reads_middle_slice():
    path = _write_xlsx([
        _full_row(1, "A", "x", "y", "T1", "A1"),
        _full_row(2, "B", "x", "y", "T2", "A1"),
        _full_row(3, "C", "x", "y", "T3", "A1"),
        _full_row(4, "D", "x", "y", "T4", "A1"),
        _full_row(5, "E", "x", "y", "T5", "A1"),
    ])
    try:
        assert f(path, 2, 2) == [("T3", "A1"), ("T4", "A1")]
    finally:
        os.remove(path)


def test_reads_single_row():
    path = _write_xlsx([
        _full_row(1, "Only", "x", "y", "T1", "A1"),
    ])
    try:
        assert f(path, 0, 1) == [("T1", "A1")]
    finally:
        os.remove(path)


def test_different_album_asins_per_row():
    # Compilation album where consecutive rows live on different "albums"
    # (Amazon's compilation behavior). The slice should preserve per-row
    # album ASIN.
    path = _write_xlsx([
        _full_row(1, "A", "x", "y", "T1", "ALB_X"),
        _full_row(2, "B", "x", "y", "T2", "ALB_Y"),
    ])
    try:
        assert f(path, 0, 2) == [("T1", "ALB_X"), ("T2", "ALB_Y")]
    finally:
        os.remove(path)


# ---- Empty / missing cells ----------------------------------------------

def test_empty_asin_cells_become_empty_strings():
    path = _write_xlsx([
        _full_row(1, "A", "x", "y", "T1", "A1"),
        _full_row(2, "B", "x", "y", "", ""),
        _full_row(3, "C", "x", "y", "T3", "A1"),
    ])
    try:
        assert f(path, 0, 3) == [
            ("T1", "A1"), ("", ""), ("T3", "A1"),
        ]
    finally:
        os.remove(path)


def test_none_cells_become_empty_strings():
    # Build via raw row with None values to make sure the path tolerates
    # cells that openpyxl reads back as None rather than "".
    path = _write_xlsx([
        [1, "A", "x", "y", "", "", "", None, None],
    ])
    try:
        assert f(path, 0, 1) == [("", "")]
    finally:
        os.remove(path)


def test_whitespace_is_stripped():
    path = _write_xlsx([
        _full_row(1, "A", "x", "y", "  T1  ", "  A1  "),
    ])
    try:
        assert f(path, 0, 1) == [("T1", "A1")]
    finally:
        os.remove(path)


# ---- Count / slice bounds ------------------------------------------------

def test_count_zero_returns_empty():
    path = _write_xlsx([_full_row(1, "A", "x", "y", "T1", "A1")])
    try:
        assert f(path, 0, 0) == []
    finally:
        os.remove(path)


def test_negative_count_returns_empty():
    path = _write_xlsx([_full_row(1, "A", "x", "y", "T1", "A1")])
    try:
        assert f(path, 0, -5) == []
    finally:
        os.remove(path)


def test_count_past_end_truncates():
    # If start_row+count > len(rows), the helper returns what's available
    # rather than padding or erroring. (Shouldn't happen in production —
    # track_count comes from the same xlsx — but defensive.)
    path = _write_xlsx([
        _full_row(1, "A", "x", "y", "T1", "A1"),
        _full_row(2, "B", "x", "y", "T2", "A1"),
    ])
    try:
        assert f(path, 0, 10) == [("T1", "A1"), ("T2", "A1")]
    finally:
        os.remove(path)


def test_start_row_past_end_returns_empty():
    path = _write_xlsx([
        _full_row(1, "A", "x", "y", "T1", "A1"),
    ])
    try:
        assert f(path, 5, 2) == []
    finally:
        os.remove(path)


# ---- Missing columns -----------------------------------------------------

def test_missing_track_asin_column_returns_empty():
    # Pre-backfill xlsx (or any file written before 2026-06-02) has no
    # Track ASIN / Album ASIN columns. Helper returns [] so the caller
    # can skip those perfs entirely.
    headers = ["#", "Song Name", "Artist", "Album", "Release Date",
               "Genre", "Duration"]
    path = _write_xlsx(
        [[1, "A", "x", "y", "", "", ""]], headers=headers,
    )
    try:
        assert f(path, 0, 1) == []
    finally:
        os.remove(path)


def test_missing_album_asin_column_returns_empty():
    headers = ["#", "Song Name", "Artist", "Album", "Release Date",
               "Genre", "Duration", "Track ASIN"]
    path = _write_xlsx(
        [[1, "A", "x", "y", "", "", "", "T1"]], headers=headers,
    )
    try:
        assert f(path, 0, 1) == []
    finally:
        os.remove(path)


def test_headers_are_case_insensitive():
    # _pick_header_index normalises to lowercase, so lowercase headers
    # work the same as the canonical title-cased ones.
    headers = ["#", "song name", "artist", "album", "release date",
               "genre", "duration", "track asin", "album asin"]
    path = _write_xlsx(
        [[1, "A", "x", "y", "", "", "", "T1", "A1"]], headers=headers,
    )
    try:
        assert f(path, 0, 1) == [("T1", "A1")]
    finally:
        os.remove(path)


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
