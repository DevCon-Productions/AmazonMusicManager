"""Unit tests for `gui._classical_perf_match`.

The matcher decides whether a now-playing track belongs to a picked Classical
Shuffle Performance. It's a pure function over dicts — no DOM, no async —
so we test it directly with realistic strings from production logs.

Run standalone:
    python test_classical_perf_match.py

Or with pytest (no extra config needed):
    pytest test_classical_perf_match.py

Each case below corresponds to a real situation we've observed in
amazonmusic.log; comments name the date / perf where applicable.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from gui import _classical_perf_match, Performance


def _perf(
    artist: str,
    titles: tuple[str, ...],
    album: str = "X",
    playlist_url: str = "u",
    playlist_name: str = "p",
    track_count: int | None = None,
    start_row: int = 0,
) -> Performance:
    return Performance(
        playlist_url=playlist_url,
        playlist_name=playlist_name,
        artist=artist,
        album=album,
        track_count=track_count if track_count is not None else len(titles),
        start_row=start_row,
        titles=titles,
    )


# ---------------------------------------------------------------------------
# Artist-credit asymmetry — both directions must pass.
# ---------------------------------------------------------------------------

def test_track_credits_drop_album_collaborator():
    """Faust/Melnikov, 2026-05-20 log.

    Album-level artist credits the pianist (Melnikov) because the album also
    contains piano works. The Larghetto movement of the violin concerto
    naturally credits only the violinist + orchestra. Old code rejected this;
    new bidirectional check accepts because np_words is a subset of perf_words.
    """
    perf = _perf(
        artist=(
            "Isabelle Faust, Alexander Melnikov, Jiri Belohlavek & "
            "The Prague Philharmonia"
        ),
        album="Beethoven: Violin Concerto",
        titles=(
            "ii. larghetto",
            "iii. rondo (allegro)",
            "iii. finale - presto",
        ),
    )
    np = {
        "title": "II. Larghetto",
        "artist": (
            "Isabelle Faust, Jiri Belohlavek & The Prague Philharmonia"
        ),
    }
    assert _classical_perf_match(np, perf) is True


def test_player_adds_composer_to_track_credits():
    """Karajan / Symphony 9, observed in pre-2026-05-20 runs.

    Album credit is two artists; player widget adds "Ludwig van Beethoven"
    to the track-level credit. perf_words is a subset of np_words.
    """
    perf = _perf(
        artist="Berliner Philharmoniker & Herbert von Karajan",
        album="Beethoven: Symphony No. 9",
        titles=(
            'beethoven: symphony no. 9 in d minor, op. 125 "choral": '
            "i. allegro ma non troppo, un poco maestoso",
        ),
    )
    np = {
        "title": (
            'Beethoven: Symphony No. 9 in D Minor, Op. 125 "Choral": '
            "I. Allegro ma non troppo, un poco maestoso"
        ),
        "artist": (
            "Berliner Philharmoniker, Herbert von Karajan & "
            "Ludwig van Beethoven"
        ),
    }
    assert _classical_perf_match(np, perf) is True


def test_reordered_artist_names_match():
    """Amazon reorders multi-artist credits — sets ignore order.

    perf_words == np_words after splitting on commas + ampersands.
    """
    perf = _perf(
        artist="A, B, C & D",
        titles=("mvt",),
    )
    np = {"title": "mvt", "artist": "A, C, D & B"}
    assert _classical_perf_match(np, perf) is True


def test_xlsx_sparse_player_adds_conductor_and_composer():
    """Karajan Sym 9, observed in 2026-05-21 17:15 log.

    The xlsx artist field is just 'Berliner Philharmoniker' (no conductor).
    The merged perf — via the grouper's piece-prefix fallback — also picks
    up the mvt IVc-j soloist ('Gundula Janowitz'), so its artist_tokens
    union is {'berliner philharmoniker', 'gundula janowitz'}. The player
    reports 'Berliner Philharmoniker, Herbert von Karajan & Ludwig van
    Beethoven'. Bidirectional subset rejects (Janowitz in perf only,
    Karajan+Beethoven in np only). Phrase-level intersection accepts on
    the shared 'berliner philharmoniker' phrase.
    """
    perf = _perf(
        artist="Berliner Philharmoniker",  # longest seen in run
        album="Beethoven: The 9 Symphonies",
        titles=(
            'beethoven: symphony no. 9 in d minor, op. 125 "choral": '
            "i. allegro ma non troppo, un poco maestoso",
        ),
    )
    perf = perf._replace(artist_tokens=frozenset({
        "berliner philharmoniker", "gundula janowitz",
    }))
    np = {
        "title": (
            'Beethoven: Symphony No. 9 in D Minor, Op. 125 "Choral": '
            "I. Allegro ma non troppo, un poco maestoso"
        ),
        "artist": (
            "Berliner Philharmoniker, Herbert von Karajan & "
            "Ludwig van Beethoven"
        ),
    }
    assert _classical_perf_match(np, perf) is True


# ---------------------------------------------------------------------------
# Rejection cases — wrong perf must NOT match.
# ---------------------------------------------------------------------------

def test_completely_different_artists_reject():
    perf = _perf(artist="Some Pianist", titles=("movement 1",))
    np = {"title": "movement 1", "artist": "Different Pianist"}
    assert _classical_perf_match(np, perf) is False


def test_partial_overlap_neither_subset_rejects():
    """{Alice, Brown} vs {Alice, Carter} — neither is a subset of the other.

    The bidirectional check must still reject this; otherwise it would
    accept any pair sharing one name token (e.g., a common first name).
    """
    perf = _perf(artist="Alice Brown", titles=("mvt",))
    np = {"title": "mvt", "artist": "Alice Carter"}
    assert _classical_perf_match(np, perf) is False


def test_title_not_in_perf_titles_rejects():
    perf = _perf(artist="X", titles=("movement 1",))
    np = {"title": "completely other thing", "artist": "X"}
    assert _classical_perf_match(np, perf) is False


# ---------------------------------------------------------------------------
# Transient states — return None so the caller doesn't flip state.
# ---------------------------------------------------------------------------

def test_empty_np_is_transient():
    perf = _perf(artist="X", titles=("mvt",))
    assert _classical_perf_match(None, perf) is None


def test_empty_title_is_transient():
    perf = _perf(artist="X", titles=("mvt",))
    np = {"title": "", "artist": "X"}
    assert _classical_perf_match(np, perf) is None


def test_perf_artist_but_empty_np_artist_is_transient():
    """Right after a row-click, the player widget may not yet have updated
    the artist field even though title is set. Return None so we keep
    waiting instead of bailing out.
    """
    perf = _perf(artist="X Y Z", titles=("mvt",))
    np = {"title": "mvt", "artist": ""}
    assert _classical_perf_match(np, perf) is None


# ---------------------------------------------------------------------------
# Edge: perf has no titles (xlsx had no title column).
# ---------------------------------------------------------------------------

def test_no_titles_falls_back_to_artist_only():
    """When the pool builder couldn't read titles (no title column in xlsx),
    we still want artist-only matching to work.
    """
    perf = _perf(artist="Solo Artist", titles=())
    np = {"title": "anything goes", "artist": "Solo Artist"}
    assert _classical_perf_match(np, perf) is True


# ---------------------------------------------------------------------------
# Standalone runner — also makes the file usable as `python test_*.py`.
# ---------------------------------------------------------------------------

def _run_all():
    tests = [
        (name, obj) for name, obj in globals().items()
        if name.startswith("test_") and callable(obj)
    ]
    failed = []
    for name, fn in tests:
        try:
            fn()
        except AssertionError as e:
            failed.append((name, f"AssertionError: {e}"))
            print(f"FAIL  {name}")
        except Exception as e:
            failed.append((name, f"{type(e).__name__}: {e}"))
            print(f"ERROR {name}: {type(e).__name__}: {e}")
        else:
            print(f"PASS  {name}")
    print()
    print(f"{len(tests) - len(failed)} passed, {len(failed)} failed "
          f"({len(tests)} total)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
