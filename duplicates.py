"""Duplicate detection for playlist spreadsheets.

Uses fuzzy matching on normalized title + artist to find potential duplicates.
Dismissed groups are saved so they don't come up again on future runs.
"""

import json
import os
from difflib import SequenceMatcher
from openpyxl import load_workbook

from utils import normalize, logger
from config import OUTPUT_DIR

# File to persist dismissed duplicate groups
_DISMISSED_FILE = os.path.join(OUTPUT_DIR, "dismissed_duplicates.json")


def _load_dismissed() -> set[str]:
    """Load previously dismissed duplicate group keys."""
    if not os.path.exists(_DISMISSED_FILE):
        return set()
    try:
        with open(_DISMISSED_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except (json.JSONDecodeError, IOError):
        return set()


def _save_dismissed(dismissed: set[str]):
    """Save dismissed duplicate group keys."""
    os.makedirs(os.path.dirname(_DISMISSED_FILE), exist_ok=True)
    with open(_DISMISSED_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(dismissed), f, indent=2)


def _group_key(titles_artists: list[tuple[str, str]]) -> str:
    """Create a stable key for a group of duplicates (order-independent)."""
    normalized = sorted(
        f"{normalize(t)}|{normalize(a)}" for t, a in titles_artists
    )
    return "::".join(normalized)


def _similarity(a: str, b: str) -> float:
    """Return similarity ratio between two strings (0.0 to 1.0)."""
    return SequenceMatcher(None, a, b).ratio()


def find_duplicates(xlsx_path: str, threshold: float = 0.85) -> list[list[dict]]:
    """Find groups of potential duplicate songs in a spreadsheet.

    Args:
        xlsx_path: Path to the .xlsx file.
        threshold: Similarity threshold (0.0-1.0) for fuzzy matching.

    Returns:
        List of duplicate groups. Each group is a list of dicts with
        row, title, artist, album, norm_title, norm_artist.
    """
    wb = load_workbook(xlsx_path)
    ws = wb.active

    # Read all songs with their row numbers
    songs = []
    for row in ws.iter_rows(min_row=2, values_only=False):
        title = row[1].value or ""
        artist = row[2].value or ""
        album = row[3].value or ""
        if not title:
            continue
        songs.append({
            "row": row[0].row,
            "title": str(title),
            "artist": str(artist),
            "album": str(album),
            "norm_title": normalize(str(title)),
            "norm_artist": normalize(str(artist)),
        })

    # Compare all pairs — O(n²) but fine for playlists (< 2000 songs)
    groups = []
    used = set()

    for i, a in enumerate(songs):
        if i in used:
            continue

        group = [a]
        for j, b in enumerate(songs[i + 1:], start=i + 1):
            if j in used:
                continue

            # Check title similarity
            title_sim = _similarity(a["norm_title"], b["norm_title"])
            if title_sim < threshold:
                continue

            # Check artist similarity
            artist_sim = _similarity(a["norm_artist"], b["norm_artist"])
            if artist_sim < 0.7:
                continue

            group.append(b)
            used.add(j)

        if len(group) > 1:
            used.add(i)
            groups.append(group)

    return groups


def interactive_dedup(xlsx_path: str, threshold: float = 0.85):
    """Interactively find and resolve duplicates in a spreadsheet.

    Prompts the user for each duplicate group. Dismissed groups are
    saved and won't appear on future runs.
    """
    groups = find_duplicates(xlsx_path, threshold)
    dismissed = _load_dismissed()

    if not groups:
        print("\nNo potential duplicates found.")
        return

    # Filter out previously dismissed groups
    new_groups = []
    for group in groups:
        key = _group_key([(s["title"], s["artist"]) for s in group])
        if key not in dismissed:
            new_groups.append((key, group))

    if not new_groups:
        print(f"\nFound {len(groups)} duplicate group(s), but all were previously dismissed.")
        print("Use --reset to clear dismissed duplicates and scan again.")
        return

    print(f"\nFound {len(new_groups)} potential duplicate group(s) to review:\n")

    wb = load_workbook(xlsx_path)
    ws = wb.active
    rows_to_delete = []
    modified = False

    for idx, (key, group) in enumerate(new_groups, 1):
        print(f"--- Group {idx}/{len(new_groups)} ---")
        for i, song in enumerate(group, 1):
            print(f"  [{i}] Row {song['row']}: {song['title']}")
            print(f"      Artist: {song['artist']}")
            print(f"      Album:  {song['album']}")
        print()

        print("  Options:")
        print("    Enter number(s) to DELETE (e.g., '2' or '1,3')")
        print("    'skip'  — not duplicates, don't ask again")
        print("    'later' — skip for now, ask again next time")
        print()

        while True:
            choice = input("  Your choice: ").strip().lower()

            if choice == "later":
                break
            elif choice == "skip":
                dismissed.add(key)
                _save_dismissed(dismissed)
                print("  Marked as not duplicates — won't ask again.\n")
                break
            else:
                # Parse numbers
                try:
                    nums = [int(x.strip()) for x in choice.split(",")]
                    valid = all(1 <= n <= len(group) for n in nums)
                    if not valid:
                        print(f"  Invalid number(s). Enter 1-{len(group)}, 'skip', or 'later'.")
                        continue

                    # Confirm deletion
                    to_delete = [group[n - 1] for n in nums]
                    titles = ", ".join(f"'{s['title']}' (row {s['row']})" for s in to_delete)
                    confirm = input(f"  Delete {titles}? (y/n): ").strip().lower()
                    if confirm == "y":
                        for s in to_delete:
                            rows_to_delete.append(s["row"])
                        dismissed.add(key)
                        _save_dismissed(dismissed)
                        modified = True
                        print("  Deleted.\n")
                    else:
                        print("  Cancelled.\n")
                    break
                except ValueError:
                    print(f"  Invalid input. Enter number(s), 'skip', or 'later'.")
                    continue

    if rows_to_delete:
        # Delete rows from bottom to top so indices don't shift
        rows_to_delete.sort(reverse=True)
        for row_num in rows_to_delete:
            ws.delete_rows(row_num)

        # Renumber the # column
        for i, row in enumerate(ws.iter_rows(min_row=2, max_col=1), start=1):
            row[0].value = i

        wb.save(xlsx_path)
        print(f"\nRemoved {len(rows_to_delete)} duplicate(s) from {xlsx_path}")
    else:
        print("\nNo changes made.")
