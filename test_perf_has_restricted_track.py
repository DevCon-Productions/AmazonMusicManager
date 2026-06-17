"""Unit tests for ``_perf_has_restricted_track`` — the pool-builder helper
that decides whether to drop a Performance because one of its tracks is in
the web-restricted log.

Originally the pool only checked the first track of each perf (sufficient
for live-mode click-time substitutions, which always derail at the playlist-
row click). The 2026-06-12 menu-hidden symptom flags individual mid-perf
ASINs, so the check now matches any track in the perf.

Run standalone:
    python test_perf_has_restricted_track.py

Or with pytest:
    pytest test_perf_has_restricted_track.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from gui import Performance, _perf_has_restricted_track


def _perf(playlist_url, artist, titles):
    """Build a minimal Performance for the filter check. Only the fields the
    helper inspects are populated."""
    return Performance(
        playlist_url=playlist_url,
        playlist_name="",
        artist=artist,
        album="",
        track_count=len(titles),
        start_row=0,
        titles=tuple(titles),
    )


def _entry(title, artist="", playlist_url="https://music.amazon.com/p/x"):
    return {
        "playlist_url": playlist_url,
        "title": title,
        "artist": artist,
    }


URL = "https://music.amazon.com/p/x"


def test_empty_restricted_returns_false():
    perf = _perf(URL, "Berliner Philharmoniker", ["sym 8: i", "sym 8: ii"])
    assert _perf_has_restricted_track(perf, {}) is False


def test_other_playlist_url_does_not_match():
    perf = _perf(URL, "Berliner Philharmoniker", ["sym 8: iii"])
    by_url = {
        "https://music.amazon.com/p/y": [
            _entry("sym 8: iii", "Berliner Philharmoniker"),
        ],
    }
    assert _perf_has_restricted_track(perf, by_url) is False


def test_first_track_match_returns_true():
    """Legacy behaviour: live-mode click-time substitutions recorded the
    first track's title. Filter must still catch them."""
    perf = _perf(URL, "Berliner Philharmoniker",
                 ["sym 9: i. allegro", "sym 9: ii", "sym 9: iii", "sym 9: iv"])
    by_url = {URL: [_entry("sym 9: i. allegro", "Berliner Philharmoniker")]}
    assert _perf_has_restricted_track(perf, by_url) is True


def test_mid_perf_track_match_returns_true():
    """2026-06-12 menu-hidden symptom: Beethoven 8 mvt III is restricted
    for Stockholm and Philadelphia. The grouper produced a 4-track perf
    for each. The old first-track-only filter would let it through and
    the playlist generator would fail again. With the broader check the
    whole perf is skipped instead."""
    perf = _perf(URL, "Royal Stockholm Philharmonic",
                 ["sym 8: i. allegro vivace",
                  "sym 8: ii. allegretto scherzando",
                  "sym 8: iii. tempo di menuetto",
                  "sym 8: iv. allegro vivace"])
    by_url = {URL: [_entry(
        "sym 8: iii. tempo di menuetto", "Royal Stockholm Philharmonic"
    )]}
    assert _perf_has_restricted_track(perf, by_url) is True


def test_title_match_artist_mismatch_returns_false():
    """Different recording of the same piece — title matches but artist
    differs. Don't false-positive Karajan's Sym 9 because Bernstein's
    Sym 9 mvt I is in the restricted log."""
    perf = _perf(URL, "Berliner Philharmoniker",
                 ["sym 9: i", "sym 9: ii", "sym 9: iii", "sym 9: iv"])
    by_url = {URL: [_entry("sym 9: i", "New York Philharmonic")]}
    assert _perf_has_restricted_track(perf, by_url) is False


def test_title_match_empty_artist_in_entry_is_permissive():
    """An older / hand-edited entry without an artist field should still
    flag the perf — the title equality plus playlist URL is enough signal."""
    perf = _perf(URL, "Berliner Philharmoniker", ["sym 8: iii"])
    by_url = {URL: [_entry("sym 8: iii", "")]}
    assert _perf_has_restricted_track(perf, by_url) is True


def test_title_match_empty_artist_in_perf_is_permissive():
    """Defensive: perf with no artist (shouldn't happen for classical, but
    the field is defaulted) should still match by title alone."""
    perf = _perf(URL, "", ["sym 8: iii"])
    by_url = {URL: [_entry("sym 8: iii", "Royal Stockholm Philharmonic")]}
    assert _perf_has_restricted_track(perf, by_url) is True


def test_title_mismatch_returns_false():
    perf = _perf(URL, "Berliner Philharmoniker",
                 ["sym 5: i", "sym 5: ii"])
    by_url = {URL: [_entry("sym 8: iii", "Berliner Philharmoniker")]}
    assert _perf_has_restricted_track(perf, by_url) is False


def test_case_insensitive_match():
    perf = _perf(URL, "Berliner Philharmoniker", ["Sym 8: III"])
    by_url = {URL: [_entry("SYM 8: iii", "berliner philharmoniker")]}
    assert _perf_has_restricted_track(perf, by_url) is True


def test_whitespace_stripped_on_both_sides():
    perf = _perf(URL, " Berliner Philharmoniker ", ["  sym 8: iii  "])
    by_url = {URL: [_entry("sym 8: iii", "Berliner Philharmoniker")]}
    assert _perf_has_restricted_track(perf, by_url) is True


def test_empty_titles_returns_false():
    perf = _perf(URL, "X", [])
    by_url = {URL: [_entry("anything", "X")]}
    assert _perf_has_restricted_track(perf, by_url) is False


def test_blank_title_in_perf_is_ignored():
    """Defensive: a perf with blank title entries shouldn't accidentally
    match a blank-title restricted entry."""
    perf = _perf(URL, "X", ["", "  ", "sym 8: iii"])
    by_url = {URL: [_entry("sym 8: iii", "X")]}
    assert _perf_has_restricted_track(perf, by_url) is True


def test_blank_title_entry_does_not_match():
    perf = _perf(URL, "X", ["sym 8: i", "sym 8: ii"])
    by_url = {URL: [_entry("", "X")]}
    assert _perf_has_restricted_track(perf, by_url) is False


def test_multiple_entries_one_matches():
    perf = _perf(URL, "BPO", ["sym 8: i", "sym 8: ii", "sym 8: iii"])
    by_url = {URL: [
        _entry("totally unrelated", "BPO"),
        _entry("sym 8: iii", "BPO"),
        _entry("another miss", "BPO"),
    ]}
    assert _perf_has_restricted_track(perf, by_url) is True


def test_realistic_two_recordings_only_one_restricted():
    """Pool-builder shape: two Beethoven 8 perfs from the same source
    playlist (Stockholm and Philadelphia). Only Stockholm's mvt III is in
    the restricted log. Stockholm gets filtered; Philadelphia stays.
    (When the second recording also gets flagged at next run-time, it'll
    join the log and be filtered too.)
    """
    stockholm = _perf(URL, "Royal Stockholm Philharmonic Orchestra",
                      ["sym 8: i", "sym 8: ii",
                       "sym 8: iii. tempo di menuetto", "sym 8: iv"])
    philadelphia = _perf(URL, "Philadelphia Orchestra",
                         ["sym 8: i", "sym 8: ii",
                          "sym 8: iii. tempo di menuetto", "sym 8: iv"])
    by_url = {URL: [_entry(
        "sym 8: iii. tempo di menuetto",
        "Royal Stockholm Philharmonic Orchestra",
    )]}
    assert _perf_has_restricted_track(stockholm, by_url) is True
    assert _perf_has_restricted_track(philadelphia, by_url) is False


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
