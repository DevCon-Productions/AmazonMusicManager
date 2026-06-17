"""Persistent log of tracks that Amazon's web player silently misroutes.

Background — see feedback_amazon_music_dom.md Trap 4. Some ASINs are licensed
to play on Amazon's desktop apps but NOT on the web player. When the web client
clicks such a row, Amazon's backend silently substitutes a different ASIN
(typically the playlist's first track) and the click "plays the wrong song."
The exact same row plays correctly in the Windows desktop app.

This module owns ./output/web_restricted_tracks.json — the persistent record
of every substitution event we've observed. Classical Shuffle writes here when
its post-click check detects the substitution, and the GUI reads here to
(a) flag rows in the spreadsheet detail view, and (b) populate the
"Review web-restricted tracks" dialog.

Schema for one entry:

    {
        "playlist_url": "https://music.amazon.com/my/playlists/...",
        "playlist_name": "Classical Dvorak Symphony 9",
        "row_index": 12,                      # 1-based, matching xlsx '#' column
        "title": "Dvořák: Symphony No. 9 ...",
        "artist": "Berliner Philharmoniker",
        "album": "Deep Dive - Dvořák",
        "requested_asin": "B09S6Q9S9M",       # what the client asked Amazon to play
        "substituted_asin": "B00138AOPO",     # what Amazon's backend actually returned
        "detected_at": "2026-05-22T18:42:45"  # ISO-8601, local time
    }

Dedup key is (playlist_url, requested_asin) — re-clicking the same restricted
row just refreshes detected_at, never grows the file.

Failures (missing file, decode errors, write errors) degrade gracefully:
- Reads return an empty list and log a warning.
- Writes do nothing and log a warning. Calling code should not raise on its
  own when these helpers can't persist — the playback experience continues
  unchanged.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

# Default storage path — kept under ./output so it travels with the user's
# spreadsheets if they back up that directory.
DEFAULT_STORE_PATH = "./output/web_restricted_tracks.json"

# File access lock — read/write are infrequent (once per click bailout) but
# Classical Shuffle and the GUI thread could theoretically both touch it.
_LOCK = threading.Lock()


def _now_iso() -> str:
    """ISO timestamp without fractional seconds — easier to read in the GUI."""
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def load(path: str = DEFAULT_STORE_PATH) -> list[dict[str, Any]]:
    """Return the list of restricted-track entries from *path*.

    Returns [] on any read error — never raises. Caller can treat missing file
    and unreadable file the same way.
    """
    with _LOCK:
        if not os.path.isfile(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("web_restricted: could not read %s (%s)", path, e)
            return []
    if not isinstance(data, dict):
        logger.warning("web_restricted: unexpected schema in %s (top-level not dict)", path)
        return []
    tracks = data.get("tracks")
    if not isinstance(tracks, list):
        return []
    # Filter out entries with no minimum identity — defensive against
    # hand-edits or older schema versions.
    out: list[dict[str, Any]] = []
    for entry in tracks:
        if not isinstance(entry, dict):
            continue
        if not entry.get("playlist_url"):
            continue
        out.append(entry)
    return out


def _save_locked(entries: list[dict[str, Any]], path: str) -> None:
    """Write *entries* — caller must already hold _LOCK."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    payload = {
        "schema": 1,
        "tracks": entries,
    }
    # Atomic-ish write: tmp file then rename. JSON is small (~KB) so this is
    # cheap even with hundreds of entries.
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as e:
        logger.warning("web_restricted: could not write %s (%s)", path, e)
        try:
            os.remove(tmp)
        except OSError:
            pass


def save(entries: list[dict[str, Any]], path: str = DEFAULT_STORE_PATH) -> None:
    """Write *entries* to *path*. Use sparingly — prefer add/remove helpers."""
    with _LOCK:
        _save_locked(entries, path)


def _key(entry: dict[str, Any]) -> tuple[str, str]:
    """Dedup key. ASIN is the durable identity; row_index can drift after
    re-export but the same restricted ASIN stays restricted regardless of
    where it sits in the playlist."""
    return (
        (entry.get("playlist_url") or "").strip(),
        (entry.get("requested_asin") or "").strip().upper(),
    )


def add(
    *,
    playlist_url: str,
    playlist_name: str = "",
    row_index: int | None = None,
    title: str = "",
    artist: str = "",
    album: str = "",
    requested_asin: str = "",
    substituted_asin: str = "",
    path: str = DEFAULT_STORE_PATH,
) -> bool:
    """Record a substitution event. Idempotent — re-recording an existing
    (playlist_url, requested_asin) just refreshes detected_at.

    Returns True if a NEW entry was added, False if an existing one was
    refreshed. Both cases are success.
    """
    if not playlist_url or not requested_asin:
        # Without these two we can't dedup — refuse to store junk.
        return False
    new_entry = {
        "playlist_url": playlist_url,
        "playlist_name": playlist_name,
        "row_index": row_index,
        "title": title,
        "artist": artist,
        "album": album,
        "requested_asin": requested_asin.upper(),
        "substituted_asin": (substituted_asin or "").upper(),
        "detected_at": _now_iso(),
    }
    with _LOCK:
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                entries = data.get("tracks") if isinstance(data, dict) else None
                if not isinstance(entries, list):
                    entries = []
            except (OSError, json.JSONDecodeError):
                entries = []
        else:
            entries = []
        existing_key = _key(new_entry)
        was_new = True
        out: list[dict[str, Any]] = []
        for e in entries:
            if isinstance(e, dict) and _key(e) == existing_key:
                # Refresh: keep the older detected_at? No — we want recency.
                # But preserve fields the caller didn't pass (defensive).
                refreshed = dict(e)
                refreshed.update({k: v for k, v in new_entry.items() if v not in ("", None)})
                refreshed["detected_at"] = new_entry["detected_at"]
                out.append(refreshed)
                was_new = False
            else:
                out.append(e)
        if was_new:
            out.append(new_entry)
        _save_locked(out, path)
        return was_new


def remove(
    *,
    playlist_url: str,
    requested_asin: str,
    path: str = DEFAULT_STORE_PATH,
) -> bool:
    """Delete the entry with this (playlist_url, requested_asin). Returns
    True if something was removed."""
    target_key = (
        (playlist_url or "").strip(),
        (requested_asin or "").strip().upper(),
    )
    if not target_key[0] or not target_key[1]:
        return False
    with _LOCK:
        if not os.path.isfile(path):
            return False
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return False
        entries = data.get("tracks") if isinstance(data, dict) else None
        if not isinstance(entries, list):
            return False
        kept = [e for e in entries if not (isinstance(e, dict) and _key(e) == target_key)]
        removed = len(kept) != len(entries)
        if removed:
            _save_locked(kept, path)
        return removed


def get_for_playlist(
    playlist_url: str, path: str = DEFAULT_STORE_PATH,
) -> list[dict[str, Any]]:
    """All entries whose playlist_url matches *playlist_url*."""
    url = (playlist_url or "").strip()
    if not url:
        return []
    return [e for e in load(path) if (e.get("playlist_url") or "").strip() == url]


def is_restricted(
    *, playlist_url: str, title: str = "", artist: str = "",
    row_index: int | None = None, path: str = DEFAULT_STORE_PATH,
) -> bool:
    """Cheap check for the spreadsheet visual flag.

    Matches an entry to a row by:
      - playlist_url exact match, AND
      - (title + artist case-insensitive match)  OR  (row_index match)

    We try title+artist first since row_index drifts after re-export (insertions
    or deletions in Amazon's playlist push the indices around).
    """
    if not playlist_url:
        return False
    entries = get_for_playlist(playlist_url, path)
    if not entries:
        return False
    t = (title or "").strip().lower()
    a = (artist or "").strip().lower()
    for e in entries:
        et = (e.get("title") or "").strip().lower()
        ea = (e.get("artist") or "").strip().lower()
        if t and a and t == et and a == ea:
            return True
        if row_index is not None and e.get("row_index") == row_index:
            return True
    return False
