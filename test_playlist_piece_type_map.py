"""Unit tests for `gui._build_playlist_piece_type_map`.

Mirror of test_playlist_composer_map.py one level deeper in the
Classical hierarchy. Builds {playlist_url: {piece_type, ...}} from an
org tree. Single pass, multi-filed playlists union their piece types.
Step 6 of the playlist generator uses this for piece-type filtering
alongside the existing composer filter.

Run standalone:
    python test_playlist_piece_type_map.py

Or with pytest:
    pytest test_playlist_piece_type_map.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from gui import _build_playlist_piece_type_map as f


def _make_org(root: dict) -> dict:
    return {"version": 1, "root": root}


def _folder(folders: dict | None = None, items: list | None = None) -> dict:
    return {"folders": folders or {}, "items": items or []}


def _playlist(url: str, name: str = "") -> dict:
    return {"kind": "playlist", "url": url, "name": name or url}


# ---- Happy path ----------------------------------------------------------

def test_single_playlist_gets_single_piece_type():
    org = _make_org(_folder({
        "Classical": _folder({
            "Beethoven": _folder({
                "Piano Sonatas": _folder({
                    "Piano Sonata 28": _folder(items=[_playlist("u1")]),
                }),
            }),
        }),
    }))
    assert f(org) == {"u1": {"Piano Sonatas"}}


def test_multiple_distinct_playlists():
    org = _make_org(_folder({
        "Classical": _folder({
            "Beethoven": _folder({
                "Symphonies": _folder(items=[_playlist("u1")]),
                "Piano Sonatas": _folder(items=[_playlist("u2")]),
            }),
            "Bach": _folder({
                "Cantatas": _folder(items=[_playlist("u3")]),
            }),
        }),
    }))
    assert f(org) == {
        "u1": {"Symphonies"},
        "u2": {"Piano Sonatas"},
        "u3": {"Cantatas"},
    }


def test_deeply_nested_path_tags_with_top_piece_type():
    # Piece type is the segment two below "Classical" — deeper nesting
    # (e.g., "Cantatas / Late Cantatas / BWV 140") does not change which
    # piece-type label applies to the playlist.
    org = _make_org(_folder({
        "Classical": _folder({
            "Bach": _folder({
                "Cantatas": _folder({
                    "Late Cantatas": _folder({
                        "BWV 140": _folder(items=[_playlist("u1")]),
                    }),
                }),
            }),
        }),
    }))
    assert f(org) == {"u1": {"Cantatas"}}


# ---- Multi-filing (the load-bearing case) -------------------------------

def test_multi_filed_playlist_unions_piece_types():
    # Same playlist filed under "Symphonies" AND "Choral Works" (e.g.,
    # Beethoven 9). Step 6 filter should match either type selection.
    org = _make_org(_folder({
        "Classical": _folder({
            "Beethoven": _folder({
                "Symphonies": _folder(items=[_playlist("u1")]),
                "Choral Works": _folder(items=[_playlist("u1")]),
            }),
        }),
    }))
    assert f(org) == {"u1": {"Symphonies", "Choral Works"}}


def test_cross_composer_multi_filing_unions_types():
    # Brahms Violin Concerto could be filed under both Brahms/Violin
    # Concertos and a hypothetical Violinists/Heifetz path — only the
    # Classical-tagged filing contributes a piece type.
    org = _make_org(_folder({
        "Classical": _folder({
            "Brahms": _folder({
                "Violin Concertos": _folder(items=[_playlist("u1")]),
            }),
            "Tchaikovsky": _folder({
                "Violin Concertos": _folder(items=[_playlist("u1")]),
            }),
        }),
    }))
    assert f(org) == {"u1": {"Violin Concertos"}}


# ---- Edge cases ---------------------------------------------------------

def test_empty_org():
    assert f(_make_org(_folder())) == {}


def test_org_with_no_classical_folder():
    org = _make_org(_folder({
        "Rock": _folder({
            "Limp Bizkit": _folder(items=[_playlist("u1")]),
        }),
    }))
    assert f(org) == {"u1": set()}


def test_classical_root_playlist_has_no_piece_type():
    # Filed directly at /Classical — no composer, no piece type.
    org = _make_org(_folder({
        "Classical": _folder(items=[_playlist("u1")]),
    }))
    assert f(org) == {"u1": set()}


def test_classical_composer_only_playlist_has_no_piece_type():
    # Filed at /Classical/Beethoven directly — composer is known but
    # no piece-type folder above the playlist, so empty set here.
    org = _make_org(_folder({
        "Classical": _folder({
            "Mozart": _folder(items=[_playlist("u1")]),
        }),
    }))
    assert f(org) == {"u1": set()}


def test_playlist_in_classical_and_rock_gets_just_classical_piece_type():
    org = _make_org(_folder({
        "Classical": _folder({
            "Beethoven": _folder({
                "Symphonies": _folder(items=[_playlist("u1")]),
            }),
        }),
        "Rock": _folder({
            "Misc": _folder(items=[_playlist("u1")]),
        }),
    }))
    assert f(org) == {"u1": {"Symphonies"}}


def test_spreadsheet_items_are_ignored():
    org = _make_org(_folder({
        "Classical": _folder({
            "Beethoven": _folder({
                "Symphonies": _folder(items=[
                    _playlist("u1"),
                    {"kind": "spreadsheet", "path": "Beethoven.xlsx",
                     "name": "Beethoven"},
                ]),
            }),
        }),
    }))
    assert f(org) == {"u1": {"Symphonies"}}


def test_playlist_with_empty_url_is_skipped():
    org = _make_org(_folder({
        "Classical": _folder({
            "Beethoven": _folder({
                "Symphonies": _folder(items=[
                    _playlist("u1"),
                    _playlist(""),
                    {"kind": "playlist", "name": "no-url"},
                ]),
            }),
        }),
    }))
    assert f(org) == {"u1": {"Symphonies"}}


def test_classical_nested_under_parent_folder():
    org = _make_org(_folder({
        "Music": _folder({
            "Classical": _folder({
                "Beethoven": _folder({
                    "Symphonies": _folder(items=[_playlist("u1")]),
                }),
            }),
        }),
    }))
    assert f(org) == {"u1": {"Symphonies"}}


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
