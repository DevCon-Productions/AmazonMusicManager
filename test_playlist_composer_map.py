"""Unit tests for `gui._build_playlist_composer_map`.

Builds {playlist_url: {composer, ...}} from an org tree. Single pass,
multi-filed playlists union their composers. Step 6 of the playlist
generator (see [[project_classical_shuffle_playlist_gen_plan]]) uses
this to filter sampled performances by the dialog's composer checkboxes.

Run standalone:
    python test_playlist_composer_map.py

Or with pytest:
    pytest test_playlist_composer_map.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from gui import _build_playlist_composer_map as f


def _make_org(root: dict) -> dict:
    return {"version": 1, "root": root}


def _folder(folders: dict | None = None, items: list | None = None) -> dict:
    return {"folders": folders or {}, "items": items or []}


def _playlist(url: str, name: str = "") -> dict:
    return {"kind": "playlist", "url": url, "name": name or url}


# ---- Happy path ----------------------------------------------------------

def test_single_classical_playlist_gets_single_composer():
    org = _make_org(_folder({
        "Classical": _folder({
            "Beethoven": _folder({
                "Piano Sonatas": _folder({
                    "Piano Sonata 28": _folder(items=[_playlist("u1")]),
                }),
            }),
        }),
    }))
    assert f(org) == {"u1": {"Beethoven"}}


def test_multiple_distinct_playlists():
    org = _make_org(_folder({
        "Classical": _folder({
            "Beethoven": _folder(items=[_playlist("u1")]),
            "Mahler": _folder(items=[_playlist("u2")]),
            "Bach": _folder({
                "Cantatas": _folder(items=[_playlist("u3")]),
            }),
        }),
    }))
    assert f(org) == {
        "u1": {"Beethoven"},
        "u2": {"Mahler"},
        "u3": {"Bach"},
    }


def test_playlist_directly_under_composer():
    # A playlist filed directly at Classical/Mozart with no piece subtree.
    org = _make_org(_folder({
        "Classical": _folder({
            "Mozart": _folder(items=[_playlist("u1")]),
        }),
    }))
    assert f(org) == {"u1": {"Mozart"}}


# ---- Multi-filing (the load-bearing case) -------------------------------

def test_multi_filed_playlist_unions_composers():
    # Same playlist URL filed under both Beethoven and Cello — Step 6
    # filter should match either composer selection.
    org = _make_org(_folder({
        "Classical": _folder({
            "Beethoven": _folder({
                "Concertos": _folder(items=[_playlist("u1")]),
            }),
            "Cello": _folder({
                "Concertos": _folder(items=[_playlist("u1")]),
            }),
        }),
    }))
    assert f(org) == {"u1": {"Beethoven", "Cello"}}


def test_triple_filing_unions_all_three():
    org = _make_org(_folder({
        "Classical": _folder({
            "Beethoven": _folder(items=[_playlist("u1")]),
            "Mahler": _folder(items=[_playlist("u1")]),
            "Bach": _folder(items=[_playlist("u1")]),
        }),
    }))
    assert f(org) == {"u1": {"Beethoven", "Mahler", "Bach"}}


# ---- Edge cases ---------------------------------------------------------

def test_empty_org():
    assert f(_make_org(_folder())) == {}


def test_org_with_no_classical_folder():
    # Non-Classical playlists are still in the map but with empty composer
    # sets — Step 6 filters them out via empty-set intersection.
    org = _make_org(_folder({
        "Rock": _folder({
            "Limp Bizkit": _folder(items=[_playlist("u1")]),
        }),
    }))
    assert f(org) == {"u1": set()}


def test_classical_root_playlist_has_no_composer():
    # A playlist filed directly at /Classical (above any composer folder)
    # contributes no composer tag — _composer_from_org_path returns None
    # for ["Classical"].
    org = _make_org(_folder({
        "Classical": _folder(items=[_playlist("u1")]),
    }))
    assert f(org) == {"u1": set()}


def test_playlist_in_both_classical_and_rock_gets_just_classical():
    # Multi-filed across a Classical path and a non-Classical path —
    # only the Classical filing contributes a composer.
    org = _make_org(_folder({
        "Classical": _folder({
            "Beethoven": _folder(items=[_playlist("u1")]),
        }),
        "Rock": _folder({
            "Misc": _folder(items=[_playlist("u1")]),
        }),
    }))
    assert f(org) == {"u1": {"Beethoven"}}


def test_spreadsheet_items_are_ignored():
    # Only playlist items contribute; spreadsheets are silently ignored.
    org = _make_org(_folder({
        "Classical": _folder({
            "Beethoven": _folder(items=[
                _playlist("u1"),
                {"kind": "spreadsheet", "path": "Beethoven.xlsx",
                 "name": "Beethoven"},
            ]),
        }),
    }))
    assert f(org) == {"u1": {"Beethoven"}}


def test_playlist_with_empty_url_is_skipped():
    # Defensive: URL "" / missing wouldn't survive the lookup-by-URL flow
    # anyway, so silently dropping them keeps the map tidy.
    org = _make_org(_folder({
        "Classical": _folder({
            "Beethoven": _folder(items=[
                _playlist("u1"),
                _playlist(""),
                {"kind": "playlist", "name": "no-url"},  # no url key
            ]),
        }),
    }))
    assert f(org) == {"u1": {"Beethoven"}}


def test_deeply_nested_path_still_tags_top_composer():
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
    assert f(org) == {"u1": {"Bach"}}


def test_classical_nested_under_parent_folder():
    # Org layouts can vary; _composer_from_org_path walks past the FIRST
    # "Classical" segment so a nested Classical folder still works.
    org = _make_org(_folder({
        "Music": _folder({
            "Classical": _folder({
                "Beethoven": _folder(items=[_playlist("u1")]),
            }),
        }),
    }))
    assert f(org) == {"u1": {"Beethoven"}}


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
