"""Cache for the user's playlist list.

Stores playlists in a JSON file so we don't have to open a browser
every time we need to refer to them.
"""

import json
import os
from datetime import datetime

from utils import logger
from config import OUTPUT_DIR

_CACHE_FILE = os.path.join(OUTPUT_DIR, "playlists.json")


def load_cached_playlists() -> list[dict] | None:
    """Load cached playlists. Returns None if no cache exists."""
    if not os.path.exists(_CACHE_FILE):
        return None
    try:
        with open(_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        playlists = data.get("playlists", [])
        updated = data.get("updated", "unknown")
        logger.info("Loaded %d cached playlists (last updated: %s)", len(playlists), updated)
        return playlists
    except (json.JSONDecodeError, IOError) as e:
        logger.warning("Failed to read playlist cache: %s", e)
        return None


def save_cached_playlists(playlists: list[dict]):
    """Save playlists to the cache file."""
    os.makedirs(os.path.dirname(_CACHE_FILE) or ".", exist_ok=True)
    data = {
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "playlists": playlists,
    }
    with open(_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    logger.info("Saved %d playlists to cache", len(playlists))


def get_playlist_url(name: str) -> str | None:
    """Look up a playlist URL by name from the cache. Returns None if not found."""
    playlists = load_cached_playlists()
    if not playlists:
        return None
    name_lower = name.lower().strip()
    for pl in playlists:
        if pl["name"].lower().strip() == name_lower:
            return pl["url"]
    return None


def print_cached_playlists():
    """Print the cached playlist list."""
    playlists = load_cached_playlists()
    if not playlists:
        print("\n  No cached playlists. Use 'Refresh playlist list' to fetch them.\n")
        return
    print(f"\n  {len(playlists)} playlists (cached):\n")
    for i, pl in enumerate(playlists, 1):
        print(f"    {i}. {pl['name']}")
    print()
