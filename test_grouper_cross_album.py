"""Unit tests for the cross-album merge rule in `_group_into_performances`.

The grouper used to require same-album for merging. Real-world source playlists
mix a compilation album (e.g. *Mahler: The Symphonies* for mvt I) with the
per-symphony album (*Mahler: Symphony No.1* for mvts II-IV) even though it's
the same Chicago Symphony recording — so the old rule split the recording into
a 1-track perf and a 3-track perf. The sampler could then draw only the
1-track perf, surfacing a single movement in the generated playlist.

The cross-album rule merges adjacent rows from different albums when BOTH the
artist tokens overlap AND the piece prefix matches. Both signals are required
so we don't over-glue:
  - different recordings of the same piece (same prefix, different performers)
    must still split
  - the same performers playing two different pieces (same tokens, different
    prefix) must still split

Run standalone:
    python test_grouper_cross_album.py

Or with pytest:
    pytest test_grouper_cross_album.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from gui import (
    _group_into_performances, _piece_prefix, _prefix_match_key, _work_signature,
)


def _rows(*entries):
    """Shorthand: each entry is (title, artist, album[, duration[, album_asin]])."""
    out = []
    for e in entries:
        title = e[0]
        artist = e[1]
        album = e[2]
        dur = e[3] if len(e) >= 4 else 0
        album_asin = e[4] if len(e) >= 5 else ""
        out.append((title, artist, album, dur, album_asin))
    return out


def test_mahler1_cross_album_same_recording_merges():
    """Mahler 1 / Chicago Symphony, real shape from
    ``Classical Mahler Symphony 1.xlsx``: mvt I lives on the compilation album
    *Mahler: The Symphonies*; mvts II-IV live on *Mahler: Symphony No.1*. Same
    recording — should land as a single 4-track perf, not 1+3.
    """
    rows = _rows(
        ("Mahler: Symphony No. 1 in D Major: I. Langsam, schleppend",
         "Chicago Symphony Orchestra", "Mahler: The Symphonies"),
        ("Mahler: Symphony No. 1 in D Major: II. Kraftig bewegt",
         "Chicago Symphony Orchestra", "Mahler: Symphony No.1"),
        ("Mahler: Symphony No. 1 in D Major: III. Feierlich und gemessen",
         "Chicago Symphony Orchestra", "Mahler: Symphony No.1"),
        ("Mahler: Symphony No. 1 in D Major: IV. Sturmisch bewegt",
         "Chicago Symphony Orchestra", "Mahler: Symphony No.1"),
    )
    perfs = _group_into_performances(rows, "u", "p")
    assert len(perfs) == 1, [p.track_count for p in perfs]
    assert perfs[0].track_count == 4


def test_cross_album_different_performers_same_piece_splits():
    """Mahler 1 Chicago Symphony (compilation) immediately followed by Mahler 1
    Boston Symphony (single-symphony album). Same piece prefix, but different
    artists — must NOT merge even though albums also differ.
    """
    rows = _rows(
        ("Mahler: Symphony No. 1 in D Major: I. Langsam",
         "Chicago Symphony Orchestra", "Mahler: The Symphonies"),
        ("Mahler: Symphony No. 1 in D Major: I. Langsam",
         "Boston Symphony Orchestra", "Mahler: Symphony No. 1 in D"),
    )
    perfs = _group_into_performances(rows, "u", "p")
    assert len(perfs) == 2
    assert all(p.track_count == 1 for p in perfs)


def test_cross_album_same_performers_different_piece_splits():
    """A single orchestra plays a Mahler 1 movement, then jumps to a Mahler 5
    movement, with each on its own album. Same performers but different piece
    prefix — must NOT merge.
    """
    rows = _rows(
        ("Mahler: Symphony No. 1 in D Major: IV. Sturmisch bewegt",
         "Chicago Symphony Orchestra", "Mahler: Symphony No.1"),
        ("Mahler: Symphony No. 5: I. Trauermarsch",
         "Chicago Symphony Orchestra", "Mahler: Symphony No. 5"),
    )
    perfs = _group_into_performances(rows, "u", "p")
    assert len(perfs) == 2
    assert all(p.track_count == 1 for p in perfs)


def test_same_album_artist_only_overlap_still_merges():
    """Regression: same-album rows whose artist tokens overlap but whose
    titles share no piece prefix must still group. This preserves the
    pre-2026-06-10 same-album OR behavior — without it the soprano-finale
    fallback would tighten unintentionally.
    """
    rows = _rows(
        ("Beethoven: Symphony No. 9 'Choral': I. Allegro",
         "Berliner Philharmoniker", "Beethoven: Sym. 9"),
        ("Beethoven: Symphony No. 9 'Choral': II. Molto vivace",
         "Berliner Philharmoniker", "Beethoven: Sym. 9"),
    )
    perfs = _group_into_performances(rows, "u", "p")
    assert len(perfs) == 1
    assert perfs[0].track_count == 2


def test_same_album_piece_only_match_still_merges():
    """Regression / soprano-finale: same album, NO artist overlap between
    consecutive rows, but the piece prefix matches — old rule merges via the
    piece-match fallback. New rule must keep that path.
    """
    rows = _rows(
        ("Beethoven: Symphony No. 9 'Choral': IV. Presto",
         "Berliner Philharmoniker", "Beethoven: Sym. 9"),
        ("Beethoven: Symphony No. 9 'Choral': IV. 'O Freunde'",
         "Gwyneth Jones", "Beethoven: Sym. 9"),
    )
    perfs = _group_into_performances(rows, "u", "p")
    assert len(perfs) == 1
    assert perfs[0].track_count == 2


def test_full_mahler1_xlsx_shape_three_recordings():
    """End-to-end shape: first four rows = Chicago (split album), next four =
    Boston (same album), next four = Bernstein (same album, different piece
    prefix because of the *Titan* subtitle). Expected: 3 separate perfs, the
    first one fully reunited at 4 tracks.
    """
    rows = _rows(
        # Chicago — mvt I on compilation, mvts II-IV on single album
        ("Mahler: Symphony No. 1 in D Major: I. Langsam",
         "Chicago Symphony Orchestra", "Mahler: The Symphonies"),
        ("Mahler: Symphony No. 1 in D Major: II. Kraftig bewegt",
         "Chicago Symphony Orchestra", "Mahler: Symphony No.1"),
        ("Mahler: Symphony No. 1 in D Major: III. Feierlich",
         "Chicago Symphony Orchestra", "Mahler: Symphony No.1"),
        ("Mahler: Symphony No. 1 in D Major: IV. Sturmisch",
         "Chicago Symphony Orchestra", "Mahler: Symphony No.1"),
        # Boston — all four on the same album
        ("Mahler: Symphony No. 1 in D Major: I. Langsam",
         "Boston Symphony Orchestra", "Mahler: Symphony No. 1 in D"),
        ("Mahler: Symphony No. 1 in D Major: II. Kraftig bewegt",
         "Boston Symphony Orchestra", "Mahler: Symphony No. 1 in D"),
        ("Mahler: Symphony No. 1 in D Major: III. Feierlich",
         "Boston Symphony Orchestra", "Mahler: Symphony No. 1 in D"),
        ("Mahler: Symphony No. 1 in D Major: IV. Sturmisch",
         "Boston Symphony Orchestra", "Mahler: Symphony No. 1 in D"),
        # Bernstein — same album, different piece prefix (Titan subtitle)
        ("Symphony No. 1 in D Major 'Titan': Ia. Langsam",
         "Leonard Bernstein", "Mahler: Sym 1 Titan"),
        ("Symphony No. 1 in D Major 'Titan': Ib. Immer sehr gemachlich",
         "Leonard Bernstein", "Mahler: Sym 1 Titan"),
    )
    perfs = _group_into_performances(rows, "u", "p")
    counts = [p.track_count for p in perfs]
    assert counts == [4, 4, 2], counts


def test_orchestra_only_artist_same_album_name_different_asin_splits():
    """The Karajan-vs-Abbado Mahler 5 bug from the 2026-06-11 real-run:
    Amazon's playlist row shows the artist as just "Berliner Philharmoniker"
    (no conductor) and the album as "Mahler: Symphony No. 5" for BOTH
    recordings. The artist phrase alone can't distinguish them, but the
    album ASINs differ — and same display-name across different ASINs is the
    distinct-recordings pattern (compilation+per-symphony, which the
    cross-album rule targets, always carries DIFFERENT album names). The
    grouper must split.
    """
    rows = _rows(
        ("Mahler: Symphony No. 5: I. Trauermarsch",
         "Berliner Philharmoniker", "Mahler: Symphony No. 5", 0, "ABBADO_ALBUM"),
        ("Mahler: Symphony No. 5: II. Sturmisch",
         "Berliner Philharmoniker", "Mahler: Symphony No. 5", 0, "ABBADO_ALBUM"),
        ("Mahler: Symphony No. 5: III. Scherzo",
         "Berliner Philharmoniker", "Mahler: Symphony No. 5", 0, "KARAJAN_ALBUM"),
        ("Mahler: Symphony No. 5: IV. Adagietto",
         "Berliner Philharmoniker", "Mahler: Symphony No. 5", 0, "KARAJAN_ALBUM"),
    )
    perfs = _group_into_performances(rows, "u", "p")
    counts = [p.track_count for p in perfs]
    assert counts == [2, 2], counts


def test_mahler1_cross_album_with_asins_still_merges():
    """Chicago Mahler 1 compilation + per-symphony case but with album ASINs
    populated. Album NAMES differ ("The Symphonies" vs "Symphony No.1"),
    which is the marker of a legitimate compilation/per-symphony split — so
    even though ASINs differ, the cross-album merge must still fire.
    """
    rows = _rows(
        ("Mahler: Symphony No. 1 in D Major: I. Langsam, schleppend",
         "Chicago Symphony Orchestra", "Mahler: The Symphonies",
         0, "COMPILATION_ASIN"),
        ("Mahler: Symphony No. 1 in D Major: II. Kraftig bewegt",
         "Chicago Symphony Orchestra", "Mahler: Symphony No.1",
         0, "SINGLE_SYMPHONY_ASIN"),
        ("Mahler: Symphony No. 1 in D Major: III. Feierlich und gemessen",
         "Chicago Symphony Orchestra", "Mahler: Symphony No.1",
         0, "SINGLE_SYMPHONY_ASIN"),
        ("Mahler: Symphony No. 1 in D Major: IV. Sturmisch bewegt",
         "Chicago Symphony Orchestra", "Mahler: Symphony No.1",
         0, "SINGLE_SYMPHONY_ASIN"),
    )
    perfs = _group_into_performances(rows, "u", "p")
    assert len(perfs) == 1, [p.track_count for p in perfs]
    assert perfs[0].track_count == 4


def test_cross_album_orchestra_only_overlap_with_different_conductor_splits():
    """Karajan-vs-Abbado Mahler 5 with DIFFERENT album display names AND the
    conductor present in the artist credit. Subset-style overlap would catch
    the orchestra phrase and merge under the cross-album rule; equality-style
    overlap (the post-fix behavior) keeps them split because the conductor
    phrase differs between sides.
    """
    rows = _rows(
        ("Mahler: Symphony No. 5: I. Trauermarsch",
         "Berliner Philharmoniker, Claudio Abbado", "Mahler 5 (Abbado)",
         0, "ABBADO_ALBUM"),
        ("Mahler: Symphony No. 5: II. Sturmisch",
         "Berliner Philharmoniker, Claudio Abbado", "Mahler 5 (Abbado)",
         0, "ABBADO_ALBUM"),
        ("Mahler: Symphony No. 5: III. Scherzo",
         "Berliner Philharmoniker, Herbert von Karajan", "Mahler 5 (Karajan)",
         0, "KARAJAN_ALBUM"),
        ("Mahler: Symphony No. 5: V. Rondo-Finale",
         "Berliner Philharmoniker, Herbert von Karajan", "Mahler 5 (Karajan)",
         0, "KARAJAN_ALBUM"),
    )
    perfs = _group_into_performances(rows, "u", "p")
    counts = [p.track_count for p in perfs]
    assert counts == [2, 2], counts


def test_same_asin_different_display_names_still_merges():
    """Defensive: when a single album somehow renders with two different
    display-name strings across consecutive rows (Amazon catalog quirks happen)
    but the ASIN is identical, the grouper should still treat them as one
    album.
    """
    rows = _rows(
        ("Beethoven: Symphony No. 9 'Choral': I. Allegro",
         "Berliner Philharmoniker", "Beethoven: Sym. 9", 0, "SYM9_ALBUM"),
        ("Beethoven: Symphony No. 9 'Choral': II. Molto vivace",
         "Berliner Philharmoniker", "Beethoven: Symphony No. 9", 0, "SYM9_ALBUM"),
    )
    perfs = _group_into_performances(rows, "u", "p")
    assert len(perfs) == 1
    assert perfs[0].track_count == 2


def test_brahms_bpo_two_recordings_split_on_movement_reset():
    """The 2026-06-11 evening bug from ``Classical Brahms Symphony 1.xlsx``:
    Berliner Philharmoniker's Brahms 1 on *Brahms: The Complete Symphonies*
    (mvts I-IV) was being glued to BPO's separate Brahms 1 on
    *Brahms: Symphony No.1 / Schumann: Symphony No.1* (mvts I-IV), because
    both rows credit only the orchestra (no conductor), the piece prefix
    matches across the boundary, and the album display names differ — so
    none of the prior guards fired. The movement-number gate catches it:
    boundary mvt IV → mvt I is a reset, not an advance, so the cross-album
    merge is blocked. Expected split: (4, 4), not (5, 3).
    """
    rows = _rows(
        ("Brahms: Symphony No. 1 in C Minor, Op. 68: I. Un poco sostenuto",
         "Berliner Philharmoniker", "Brahms: The Complete Symphonies",
         0, "B000VHIWD4"),
        ("Brahms: Symphony No. 1 in C Minor, Op. 68: II. Andante sostenuto",
         "Berliner Philharmoniker", "Brahms: The Complete Symphonies",
         0, "B000VHIWD4"),
        ("Brahms: Symphony No. 1 in C Minor, Op. 68: III. Un poco allegretto",
         "Berliner Philharmoniker", "Brahms: The Complete Symphonies",
         0, "B000VHIWD4"),
        ("Brahms: Symphony No. 1 in C Minor, Op. 68: IV. Adagio",
         "Berliner Philharmoniker", "Brahms: The Complete Symphonies",
         0, "B000VHIWD4"),
        ("Brahms: Symphony No. 1 in C Minor, Op. 68: I. Un poco sostenuto",
         "Berliner Philharmoniker", "Brahms: Symphony No.1 / Schumann: Symphony No.1",
         0, "B000V6S95G"),
        # mvt II credits the concertmaster soloist instead of the orchestra,
        # matching the real Amazon row — must still join the second perf via
        # the same-album + piece-prefix fallback.
        ("Brahms: Symphony No. 1 in C Minor, Op. 68: II. Andante sostenuto",
         "Michel Schwalbé", "Brahms: Symphony No.1 / Schumann: Symphony No.1",
         0, "B000V6S95G"),
        ("Brahms: Symphony No. 1 in C Minor, Op. 68: III. Un poco allegretto",
         "Berliner Philharmoniker", "Brahms: Symphony No.1 / Schumann: Symphony No.1",
         0, "B000V6S95G"),
        ("Brahms: Symphony No. 1 in C Minor, Op. 68: IV. Adagio",
         "Berliner Philharmoniker", "Brahms: Symphony No.1 / Schumann: Symphony No.1",
         0, "B000V6S95G"),
    )
    perfs = _group_into_performances(rows, "u", "p")
    counts = [p.track_count for p in perfs]
    assert counts == [4, 4], counts
    # Sanity: the first perf must NOT span albums.
    assert perfs[0].album == "Brahms: The Complete Symphonies"
    assert perfs[1].album == "Brahms: Symphony No.1 / Schumann: Symphony No.1"


def test_cross_album_movement_advance_still_merges_when_artists_equal():
    """The legitimate Mahler 1 compilation/per-symphony case the cross-album
    rule was built for. Boundary mvt I → mvt II ADVANCES, so the
    movement-number gate must still allow the merge. This is essentially
    ``test_mahler1_cross_album_same_recording_merges`` but with ASINs
    populated, to cover the post-2026-06-02-backfill xlsx shape end-to-end.
    """
    rows = _rows(
        ("Mahler: Symphony No. 1 in D Major: I. Langsam, schleppend",
         "Chicago Symphony Orchestra", "Mahler: The Symphonies",
         0, "COMPILATION_ASIN"),
        ("Mahler: Symphony No. 1 in D Major: II. Kraftig bewegt",
         "Chicago Symphony Orchestra", "Mahler: Symphony No.1",
         0, "SINGLE_SYMPHONY_ASIN"),
        ("Mahler: Symphony No. 1 in D Major: III. Feierlich",
         "Chicago Symphony Orchestra", "Mahler: Symphony No.1",
         0, "SINGLE_SYMPHONY_ASIN"),
        ("Mahler: Symphony No. 1 in D Major: IV. Sturmisch",
         "Chicago Symphony Orchestra", "Mahler: Symphony No.1",
         0, "SINGLE_SYMPHONY_ASIN"),
    )
    perfs = _group_into_performances(rows, "u", "p")
    assert len(perfs) == 1, [p.track_count for p in perfs]
    assert perfs[0].track_count == 4


def test_cross_album_no_movement_numbers_falls_back_permissive():
    """When neither title carries a parseable movement number (overtures,
    single-mvt works), the movement-number gate must NOT block. The artist-
    equality + piece-prefix signals are all we have, and the rule should
    fall back to its pre-gate behavior — otherwise we'd over-split.

    Stretches the rule a bit: two consecutive "Coriolan Overture" tracks
    on different albums by the same orchestra. Real-world rare but worth
    keeping permissive — the gate exists to BLOCK overconfident merges,
    not to require movement numbers where they don't exist.
    """
    rows = _rows(
        ("Beethoven: Coriolan Overture",
         "Berliner Philharmoniker", "Beethoven: Overtures",
         0, "OVERTURES_ALBUM"),
        ("Beethoven: Coriolan Overture",
         "Berliner Philharmoniker", "Beethoven: The Complete Edition",
         0, "COMPLETE_EDITION_ALBUM"),
    )
    perfs = _group_into_performances(rows, "u", "p")
    # Permissive fallback merges them into one. This isn't necessarily
    # musically correct (two different recordings of the same overture)
    # but matches pre-gate behavior — the user accepted that tradeoff
    # when they decided "missing signal shouldn't block."
    assert len(perfs) == 1
    assert perfs[0].track_count == 2


def test_cross_album_movement_equal_blocks_merge():
    """Same as the Brahms case but with a single-row repeat at the
    boundary: previous group ended on mvt I, new album also starts mvt I.
    Strict-greater (not ≥) is the rule for cross-album, so equal mvt
    numbers also block. Mvt I → mvt I across albums is the "different
    recordings, both starting from the top" pattern — must split.
    """
    rows = _rows(
        ("Mahler: Symphony No. 5: I. Trauermarsch",
         "Berliner Philharmoniker", "Mahler 5 (Recording A)",
         0, "RECORDING_A_ASIN"),
        ("Mahler: Symphony No. 5: I. Trauermarsch",
         "Berliner Philharmoniker", "Mahler 5 (Recording B)",
         0, "RECORDING_B_ASIN"),
    )
    perfs = _group_into_performances(rows, "u", "p")
    counts = [p.track_count for p in perfs]
    assert counts == [1, 1], counts


def test_arrau_grieg_schumann_coupling_album_splits_on_piece_change():
    """The 2026-06-12 15:49-run bug from
    ``Classical Grieg Piano Concerto in A Minor.xlsx`` rows 7-12. Claudio
    Arrau plays Grieg PC Op. 16 (mvts I-III) followed by Schumann PC Op. 54
    (mvts I-III), all on the single coupling album
    *Grieg: Piano Concerto in A Minor - Schumann: Piano Concerto in A Minor*
    (B0040IDPO6). Both pieces are in A minor, so Amazon renders both as
    "Piano Concerto in A Minor, Op. NN: <mvt>".

    The pre-2026-06-13 same-album+artist_overlap rule glued all 6 mvts into
    one perf, the sampler drew it as "Arrau Grieg PC", and the user heard
    Grieg mvts I-III followed by Schumann mvts I-III — calling it "Grieg
    twice in a row". The piece_changes guard catches the prefix change
    ("piano concerto in a minor, op. 16" → "...op. 54") and splits.
    Expected: (3, 3).
    """
    rows = _rows(
        ("Piano Concerto in A Minor, Op. 16: I. Allegro molto moderato",
         "Claudio Arrau", "Grieg: Piano Concerto - Schumann: Piano Concerto",
         0, "B0040IDPO6"),
        ("Piano Concerto in A Minor, Op. 16: II. Adagio",
         "Claudio Arrau", "Grieg: Piano Concerto - Schumann: Piano Concerto",
         0, "B0040IDPO6"),
        ("Piano Concerto in A Minor, Op. 16: III. Allegro moderato molto e marcato",
         "Claudio Arrau", "Grieg: Piano Concerto - Schumann: Piano Concerto",
         0, "B0040IDPO6"),
        ("Piano Concerto in A Minor, Op. 54: I. Allegro affettuoso",
         "Claudio Arrau", "Grieg: Piano Concerto - Schumann: Piano Concerto",
         0, "B0040IDPO6"),
        ("Piano Concerto in A Minor, Op. 54: II. Intermezzo",
         "Claudio Arrau", "Grieg: Piano Concerto - Schumann: Piano Concerto",
         0, "B0040IDPO6"),
        ("Piano Concerto in A Minor, Op. 54: III. Allegro vivace",
         "Claudio Arrau", "Grieg: Piano Concerto - Schumann: Piano Concerto",
         0, "B0040IDPO6"),
    )
    perfs = _group_into_performances(rows, "u", "p")
    counts = [p.track_count for p in perfs]
    assert counts == [3, 3], counts


def test_same_album_overture_then_symphony_same_orchestra_splits():
    """Generalization of the Grieg/Schumann case to a different coupling
    shape: an overture and a symphony on the same album by the same
    orchestra. Piece prefix changes from "beethoven" (overture, only the
    composer name precedes the final colon) to "beethoven: symphony no. 5
    in c minor, op. 67" (symphony) — the guard fires and splits.
    """
    rows = _rows(
        ("Beethoven: Coriolan Overture, Op. 62",
         "Berliner Philharmoniker", "Beethoven: Overtures and Symphony No. 5",
         0, "OVERTURES_AND_SYM5"),
        ("Beethoven: Symphony No. 5 in C Minor, Op. 67: I. Allegro con brio",
         "Berliner Philharmoniker", "Beethoven: Overtures and Symphony No. 5",
         0, "OVERTURES_AND_SYM5"),
    )
    perfs = _group_into_performances(rows, "u", "p")
    counts = [p.track_count for p in perfs]
    assert counts == [1, 1], counts


def test_same_album_choral_finale_still_merges_under_piece_changes_guard():
    """Beethoven 9 choral finale on the same album: mvts I-III credit the
    orchestra, mvt IV gains a soprano credit (different artist tokens).
    The artist-overlap branch would not fire AND the piece prefix is
    constant ("beethoven: symphony no. 9 'choral'") across all four rows,
    so piece_match merges them. The new piece_changes guard does NOT gate
    piece_match, so this regression target stays a single 4-track perf.
    """
    rows = _rows(
        ("Beethoven: Symphony No. 9 'Choral': I. Allegro",
         "Berliner Philharmoniker", "Beethoven: Sym. 9", 0, "SYM9"),
        ("Beethoven: Symphony No. 9 'Choral': II. Molto vivace",
         "Berliner Philharmoniker", "Beethoven: Sym. 9", 0, "SYM9"),
        ("Beethoven: Symphony No. 9 'Choral': III. Adagio molto",
         "Berliner Philharmoniker", "Beethoven: Sym. 9", 0, "SYM9"),
        ("Beethoven: Symphony No. 9 'Choral': IV. Presto 'O Freunde'",
         "Gwyneth Jones", "Beethoven: Sym. 9", 0, "SYM9"),
    )
    perfs = _group_into_performances(rows, "u", "p")
    assert len(perfs) == 1
    assert perfs[0].track_count == 4


# ---------------------------------------------------------------------------
# 2026-06-15: _piece_prefix movement-boundary fix. The old rsplit(":", 1)[0]
# took everything before the LAST colon, so movement titles containing a
# second colon (subdivided finales, tempo tails, cadenza notes) produced a
# per-movement-varying prefix that made piece_changes fire and shattered whole
# single-album recordings into lone-movement fragments the sampler then drew.
# ---------------------------------------------------------------------------

def test_subdivided_finale_stays_grouped():
    """Mahler 2 / NY Phil, real shape from ``Classical Mahler Symphony 2.xlsx``:
    a single album where the Scherzo and Finale are subdivided
    ('III. Scherzo: a.', 'V. Finale: a.'). The extra colon must NOT split the
    recording — all movements are one performance.
    """
    rows = _rows(
        ('Mahler: Symphony No. 2 "Resurrection": I. Allegro maestoso',
         "New York Philharmonic", "Mahler 2", 0, "M2"),
        ('Mahler: Symphony No. 2 "Resurrection": II. Andante moderato',
         "New York Philharmonic", "Mahler 2", 0, "M2"),
        ('Mahler: Symphony No. 2 "Resurrection": III. Scherzo: a. In ruhig',
         "New York Philharmonic", "Mahler 2", 0, "M2"),
        ('Mahler: Symphony No. 2 "Resurrection": III. Scherzo: b. Sehr',
         "New York Philharmonic", "Mahler 2", 0, "M2"),
        ('Mahler: Symphony No. 2 "Resurrection": IV. Urlicht. O Roschen rot',
         "New York Philharmonic", "Mahler 2", 0, "M2"),
        ('Mahler: Symphony No. 2 "Resurrection": V. Finale: a. Im Tempo',
         "New York Philharmonic", "Mahler 2", 0, "M2"),
        ('Mahler: Symphony No. 2 "Resurrection": V. Finale: b. Langsam',
         "New York Philharmonic", "Mahler 2", 0, "M2"),
    )
    perfs = _group_into_performances(rows, "u", "p")
    assert len(perfs) == 1, [p.track_count for p in perfs]
    assert perfs[0].track_count == 7


def test_tempo_tail_colon_stays_grouped():
    """Eroica: mvt II is 'II. Marcia funebre: Adagio assai' — the tempo tail's
    colon must not split mvt II (or mvt I) off. Same album, one performance.
    """
    rows = _rows(
        ('Symphony No. 3 "Eroica": I. Allegro con brio',
         "London Symphony Orchestra", "Eroica", 0, "E1"),
        ('Symphony No. 3 "Eroica": II. Marcia funebre: Adagio assai',
         "London Symphony Orchestra", "Eroica", 0, "E1"),
        ('Symphony No. 3 "Eroica": III. Scherzo: Allegro vivace',
         "London Symphony Orchestra", "Eroica", 0, "E1"),
        ('Symphony No. 3 "Eroica": IV. Finale: Allegro molto',
         "London Symphony Orchestra", "Eroica", 0, "E1"),
    )
    perfs = _group_into_performances(rows, "u", "p")
    assert len(perfs) == 1, [p.track_count for p in perfs]
    assert perfs[0].track_count == 4


def test_cadenza_note_colon_stays_grouped():
    """Beethoven PC4: mvt I carries '(Cadenza: Kempff)'. The parenthetical
    colon must not split mvt I from mvts II/III on the same album.
    """
    rows = _rows(
        ("Beethoven: Piano Concerto No. 4 in G Major, Op. 58: "
         "I. Allegro moderato (Cadenza: Kempff)",
         "Wilhelm Kempff", "PC4", 0, "PC4"),
        ("Beethoven: Piano Concerto No. 4 in G Major, Op. 58: II. Andante con moto",
         "Wilhelm Kempff", "PC4", 0, "PC4"),
        ("Beethoven: Piano Concerto No. 4 in G Major, Op. 58: III. Rondo. Vivace",
         "Wilhelm Kempff", "PC4", 0, "PC4"),
    )
    perfs = _group_into_performances(rows, "u", "p")
    assert len(perfs) == 1, [p.track_count for p in perfs]
    assert perfs[0].track_count == 3


def test_text_incipit_finale_no_numeral_stays_grouped():
    """Beethoven 9 choral finale rendered as a bare text incipit with NO
    numeral ('"Choral": "O Freunde, nicht diese Tone!"'). The no-numbered-marker
    fallback must keep the same piece prefix as mvts I-IV so it groups. This is
    the regression the 2026-06-15 fix had to preserve.
    """
    rows = _rows(
        ('Symphony No. 9 in D Minor, Op. 125 "Choral": I. Allegro ma non troppo',
         "Berliner Philharmoniker", "Sym 9", 0, "S9"),
        ('Symphony No. 9 in D Minor, Op. 125 "Choral": II. Molto vivace',
         "Berliner Philharmoniker", "Sym 9", 0, "S9"),
        ('Symphony No. 9 in D Minor, Op. 125 "Choral": III. Adagio molto e cantabile',
         "Berliner Philharmoniker", "Sym 9", 0, "S9"),
        ('Symphony No. 9 in D Minor, Op. 125 "Choral": IV. Presto',
         "Berliner Philharmoniker", "Sym 9", 0, "S9"),
        ('Symphony No. 9 in D Minor, Op. 125 "Choral": "O Freunde, nicht diese Tone!"',
         "Anna Tomowa-Sintow", "Sym 9", 0, "S9"),
    )
    perfs = _group_into_performances(rows, "u", "p")
    assert len(perfs) == 1, [p.track_count for p in perfs]
    assert perfs[0].track_count == 5


def test_dash_separated_movements_stay_grouped():
    """Karajan's Beethoven 5 on album B0015HK92Q uses a colon-less, numeral-less
    format: 'Symphony No. 5 in C minor (Fate), Op. 67 - Andante con moto' (and
    one row with no space after the dash, '... Op. 67 -Allegro con brio'). All
    four movements share the same piece and album — must be one performance.
    """
    rows = _rows(
        ("Symphony No. 5 in C minor (Fate), Op. 67 -Allegro con brio",
         "Berliner Philharmoniker", "Beethoven 5", 0, "B5K"),
        ("Symphony No. 5 in C minor (Fate), Op. 67 - Andante con moto",
         "Berliner Philharmoniker", "Beethoven 5", 0, "B5K"),
        ("Symphony No. 5 in C minor (Fate), Op. 67 - Allegro. Scherzo",
         "Berliner Philharmoniker", "Beethoven 5", 0, "B5K"),
        ("Symphony No. 5 in C minor (Fate), Op. 67 - Allegro. Finale",
         "Berliner Philharmoniker", "Beethoven 5", 0, "B5K"),
    )
    perfs = _group_into_performances(rows, "u", "p")
    assert len(perfs) == 1, [p.track_count for p in perfs]
    assert perfs[0].track_count == 4


def test_variation_set_with_subtitle_colons_stays_grouped():
    """Elgar Enigma (album B0031MNDH6): variations use 'Var.N - <tempo>', and a
    few carry a SECOND colon in their subtitle ('Var.10 - Intermezzo: …',
    'Var.14 - Finale: E.D.U.'). The 'Var.N' marker must anchor the split so
    those variations keep the same piece prefix as the rest — one performance,
    not a shattered set the sampler draws in pieces.
    """
    rows = _rows(
        ("Enigma Variations, Op.36: Theme - Andante",
         "Academy of St Martin", "Enigma", 0, "EN"),
        ("Enigma Variations, Op.36: Var.9 - Adagio",
         "Academy of St Martin", "Enigma", 0, "EN"),
        ("Enigma Variations, Op.36: Var.10 - Intermezzo: Allegretto",
         "Academy of St Martin", "Enigma", 0, "EN"),
        ("Enigma Variations, Op.36: Var.11 - Allegro di molto",
         "Academy of St Martin", "Enigma", 0, "EN"),
        ("Enigma Variations, Op.36: Var.14 - Finale: E.D.U. Allegro",
         "Academy of St Martin", "Enigma", 0, "EN"),
    )
    perfs = _group_into_performances(rows, "u", "p")
    assert len(perfs) == 1, [p.track_count for p in perfs]
    assert perfs[0].track_count == 5


def test_whitespace_only_prefix_difference_stays_grouped():
    """Tchaikovsky VC on album B0042EFUX2: Amazon spells the catalogue number
    inconsistently across movements of the SAME recording — 'Op. 35, TH.59'
    (no space) on mvt I vs 'Op. 35, TH. 59' (space) on mvts II/III. That
    one-space difference must not split the recording.
    """
    rows = _rows(
        ("Tchaikovsky: Violin Concerto in D Major, Op. 35, TH.59: I. Allegro moderato",
         "David Oistrakh", "Tch VC", 0, "TVC"),
        ("Tchaikovsky: Violin Concerto in D Major, Op. 35, TH. 59: II. Canzonetta",
         "David Oistrakh", "Tch VC", 0, "TVC"),
        ("Tchaikovsky: Violin Concerto in D Major, Op. 35, TH. 59: III. Allegro vivacissimo",
         "David Oistrakh", "Tch VC", 0, "TVC"),
    )
    perfs = _group_into_performances(rows, "u", "p")
    assert len(perfs) == 1, [p.track_count for p in perfs]
    assert perfs[0].track_count == 3


def test_prefix_match_key_whitespace_insensitive():
    """_prefix_match_key collapses whitespace so spacing-only metadata
    differences compare equal, but genuinely different works do not."""
    k = _prefix_match_key
    assert k("op. 35, th.59") == k("op. 35, th. 59")
    assert k("op. 16") == k("op.16")
    # Different opus numbers must NOT collide.
    assert k("piano concerto in a minor, op. 16") != k("piano concerto in a minor, op. 54")


def test_cross_album_catalogue_signature_merges():
    """Perlman's Tchaikovsky VC is catalogued under two album entries with
    inconsistent titles — 'Violin Concerto, Op.35 (1985 - Remaster): I.' on
    one album, 'Violin Concerto in D Major, Op. 35: II./III.' on another.
    Same artist, same catalogue number (Op. 35), movements advance I->II->III,
    album names differ. The piece prefixes don't match, but the catalogue
    signature does — must merge into one recording.
    """
    art = "Itzhak Perlman/Philadelphia Orchestra/Eugene Ormandy"
    rows = _rows(
        ("Violin Concerto, Op.35 (1985 - Remaster): I. Allegro moderato",
         art, "Violin Concerto / Serenade - Tchaikovsky", 0, "ALB1"),
        ("Violin Concerto in D Major, Op. 35: II. Canzonetta. Andante",
         art, "Tchaikovsky: Violin Concerto & Serenade", 0, "ALB2"),
        ("Violin Concerto in D Major, Op. 35: III. Finale. Allegro vivacissimo",
         art, "Tchaikovsky: Violin Concerto & Serenade", 0, "ALB2"),
    )
    perfs = _group_into_performances(rows, "u", "p")
    assert len(perfs) == 1, [p.track_count for p in perfs]
    assert perfs[0].track_count == 3


def test_cross_album_same_catalogue_movement_reset_splits():
    """Two different recordings of the same work by the same artist on
    different albums (movement IV -> I reset at the boundary) must NOT merge,
    even though catalogue signatures match — the reset signals a new recording.
    """
    art = "Berliner Philharmoniker"
    rows = _rows(
        ("Symphony No. 1 in C Minor, Op. 68: III. Un poco allegretto",
         art, "Complete Symphonies", 0, "A"),
        ("Symphony No. 1 in C Minor, Op. 68: IV. Adagio - Allegro",
         art, "Complete Symphonies", 0, "A"),
        ("Symphony No. 1 in C Minor, Op. 68: I. Un poco sostenuto",
         art, "Brahms Symphony No. 1", 0, "B"),
        ("Symphony No. 1 in C Minor, Op. 68: II. Andante sostenuto",
         art, "Brahms Symphony No. 1", 0, "B"),
    )
    perfs = _group_into_performances(rows, "u", "p")
    assert [p.track_count for p in perfs] == [2, 2], [p.track_count for p in perfs]


def test_inconsistent_title_same_catalogue_stays_grouped():
    """Schubert Piano Trio 2 on album B007GN06WQ: mvt I's title carries
    'No. 2' but mvts II-IV drop it — only the surrounding text differs, the
    catalogue numbers (Op. 100, D. 929) are identical. Same recording, must be
    one performance. The whitespace key can't bridge the extra 'No. 2' token;
    the catalogue-signature path does.
    """
    rows = _rows(
        ("Trio for Piano, Violin & Violoncello No. 2 in E-Flat Major, Op. 100, D. 929: I. Allegro",
         "Beaux Arts Trio", "Schubert Trios", 0, "TR"),
        ("Trio for Piano, Violin & Violoncello in E-Flat Major, Op. 100, D. 929: II. Andante con moto",
         "Beaux Arts Trio", "Schubert Trios", 0, "TR"),
        ("Trio for Piano, Violin & Violoncello in E-Flat Major, Op. 100, D. 929: III. Scherzo",
         "Beaux Arts Trio", "Schubert Trios", 0, "TR"),
        ("Trio for Piano, Violin & Violoncello in E-Flat Major, Op. 100, D. 929: IV. Allegro moderato",
         "Beaux Arts Trio", "Schubert Trios", 0, "TR"),
    )
    perfs = _group_into_performances(rows, "u", "p")
    assert len(perfs) == 1, [p.track_count for p in perfs]
    assert perfs[0].track_count == 4


def test_catalogue_match_does_not_glue_coupling_album():
    """Same album + same artist + DIFFERENT catalogue numbers (a coupling
    album) must still split — the catalogue-signature path requires equal
    signatures, so Grieg Op. 16 and Schumann Op. 54 stay separate.
    """
    rows = _rows(
        ("Piano Concerto in A Minor, Op. 16: I. Allegro molto moderato",
         "Claudio Arrau", "Grieg / Schumann", 0, "CPL"),
        ("Piano Concerto in A Minor, Op. 16: II. Adagio",
         "Claudio Arrau", "Grieg / Schumann", 0, "CPL"),
        ("Piano Concerto in A Minor, Op. 54: I. Allegro affettuoso",
         "Claudio Arrau", "Grieg / Schumann", 0, "CPL"),
        ("Piano Concerto in A Minor, Op. 54: II. Intermezzo",
         "Claudio Arrau", "Grieg / Schumann", 0, "CPL"),
    )
    perfs = _group_into_performances(rows, "u", "p")
    assert [p.track_count for p in perfs] == [2, 2], [p.track_count for p in perfs]


def test_work_signature_extraction():
    """_work_signature pulls catalogue tokens; equal => same work."""
    assert _work_signature("piano trio no. 2 in e-flat, op. 100, d. 929") == frozenset({"op100", "d929"})
    # Period-inconsistent renderings of the same number collapse together.
    assert _work_signature("symphony no. 8, d 759") == _work_signature("symphony no. 8, d. 759")
    assert (_work_signature("trio for piano, violin & violoncello no. 2 in e-flat major, op. 100, d. 929")
            == _work_signature("trio for piano, violin & violoncello in e-flat major, op. 100, d. 929"))
    assert _work_signature("piano concerto in a minor, op. 16") != _work_signature("piano concerto in a minor, op. 54")
    # A key signature ('in D major') must NOT be read as a D. catalogue number.
    assert _work_signature("symphony no. 9 in d major") == frozenset()
    # No catalogue number at all (Mahler) => empty, callers fall back to prefix.
    assert _work_signature('mahler: symphony no. 2 "resurrection"') == frozenset()


def test_piece_prefix_movement_boundary():
    """Direct unit checks on _piece_prefix's movement-boundary split."""
    # Subdivided finale — second colon must not corrupt the prefix.
    assert (_piece_prefix('Mahler: Symphony No. 2 "Resurrection": V. Finale: a. Im Tempo')
            == 'mahler: symphony no. 2 "resurrection"')
    # Tempo tail.
    assert (_piece_prefix('Symphony No. 3 "Eroica": II. Marcia funebre: Adagio assai')
            == 'symphony no. 3 "eroica"')
    # Cadenza parenthetical.
    assert (_piece_prefix("Beethoven: Piano Concerto No. 4 in G Major, Op. 58: "
                          "I. Allegro moderato (Cadenza: Kempff)")
            == "beethoven: piano concerto no. 4 in g major, op. 58")
    # Bare movement, no piece name → empty prefix (grouper falls back to album/artist).
    assert _piece_prefix("II. Larghetto") == ""
    # Piece name opening with a count is NOT a movement marker (no attached period).
    assert (_piece_prefix("Webern: 5 Pieces for Orchestra, Op. 10: I. Sehr ruhig")
            == "webern: 5 pieces for orchestra, op. 10")
    # Text-incipit finale with no numeral → fallback to before-last-colon.
    assert (_piece_prefix('Symphony No. 9 "Choral": "O Freunde, nicht diese Tone!"')
            == 'symphony no. 9 "choral"')
    # Colon-less title with no dash stays whole.
    assert _piece_prefix("Etudes Symphoniques") == "etudes symphoniques"
    # Colon-less " - <tempo>" movement format (Karajan B5): strip at the dash.
    assert (_piece_prefix("Symphony No. 5 in C minor (Fate), Op. 67 - Andante con moto")
            == "symphony no. 5 in c minor (fate), op. 67")
    assert (_piece_prefix("Symphony No. 5 in C minor (Fate), Op. 67 -Allegro con brio")
            == "symphony no. 5 in c minor (fate), op. 67")
    # Hyphenated key name must NOT be split (no space before the dash).
    assert (_piece_prefix("Symphony No. 3 in E-Flat Major, Op. 55: I. Allegro")
            == "symphony no. 3 in e-flat major, op. 55")
    # Variation marker 'Var.N', incl. a subtitle colon that must not corrupt it.
    assert (_piece_prefix("Enigma Variations, Op.36: Var.11 - Allegro di molto")
            == "enigma variations, op.36")
    assert (_piece_prefix("Enigma Variations, Op.36: Var.10 - Intermezzo: Allegretto")
            == "enigma variations, op.36")
    assert (_piece_prefix("Enigma Variations, Op.36: Var.14 - Finale: E.D.U. Allegro")
            == "enigma variations, op.36")
    assert (_piece_prefix("Elgar: Enigma Variations, Op. 36: Var. 1. L'istesso tempo")
            == "elgar: enigma variations, op. 36")
    # 'Theme' as a section is a marker...
    assert (_piece_prefix("Enigma Variations, Op.36: Theme - Andante")
            == "enigma variations, op.36")
    # ...but a piece literally named 'Theme and Variations' is NOT a marker.
    assert (_piece_prefix("Theme and Variations, Op. 73: Var. 1. Allegretto")
            == "theme and variations, op. 73")
    # Quoted text-incipit movement is a marker (Beethoven 9 Ode to Joy).
    assert (_piece_prefix('Symphony No. 9 "Choral": "O Freunde, nicht diese Tone!"')
            == 'symphony no. 9 "choral"')
    # Arabic-numeral movements (Prokofiev uses '1.', '2.').
    assert (_piece_prefix("Prokofiev: Piano Concerto No. 2 in G Minor, Op. 16: 1. Andantino")
            == "prokofiev: piano concerto no. 2 in g minor, op. 16")


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
