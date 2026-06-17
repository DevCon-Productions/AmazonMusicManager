"""End-to-end smoke test for `importer.add_track_by_asin`.

Adds a single ASIN-identified track to a target playlist via the
Amazon-Music deep-link + 3-dot menu flow.

Default invocation uses the known-good Dvořák ASIN (verified during
Step 2.5: `/albums/B09S6SS3F5?trackAsin=B09S6Q9S9M` is the Berliner
Phil mvt IV from *Deep Dive – Dvořák*). The target playlist must
already exist in your Amazon Music library — the smoke test does NOT
create it, so we don't litter the library with empty playlists.

Usage:
    # Pre-step: in Amazon Music, create an empty playlist named exactly:
    #   "Smoke Test - add_track_by_asin"
    # (or pass --playlist with the name of any existing playlist).

    python smoke_add_track_by_asin.py
    python smoke_add_track_by_asin.py --playlist "My Test Playlist"
    python smoke_add_track_by_asin.py --album B0... --track B0... --playlist "..."

After the script reports success, verify by opening that playlist in
Amazon Music and confirming the Dvořák track was added.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

# UTF-8 stdout for diacritics on Windows consoles.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

from auth import ensure_logged_in
from browser import BrowserManager
from importer import add_track_by_asin
from utils import logger, setup_logging


# Known-good Dvořák ASINs from Step 2.5 verification.
DEFAULT_ALBUM_ASIN = "B09S6SS3F5"
DEFAULT_TRACK_ASIN = "B09S6Q9S9M"
DEFAULT_PLAYLIST = "Smoke Test - add_track_by_asin"


async def _run(args: argparse.Namespace) -> int:
    setup_logging()

    print(f"album_asin    = {args.album}")
    print(f"track_asin    = {args.track}")
    print(f"playlist_name = {args.playlist!r}")
    print(f"deep-link     = https://music.amazon.com/albums/{args.album}?trackAsin={args.track}")
    print()

    async with BrowserManager(headless=args.headless, slow_mo=args.slow_mo) as bm:
        await ensure_logged_in(bm.page)
        print("[smoke] logged in — calling add_track_by_asin...")
        ok = await add_track_by_asin(
            bm.page,
            album_asin=args.album,
            track_asin=args.track,
            playlist_name=args.playlist,
        )

    print()
    if ok:
        print("[smoke] RESULT: success ✓")
        print(f"        Verify by opening '{args.playlist}' in Amazon Music")
        print(f"        and confirming the track is present.")
        return 0
    else:
        print("[smoke] RESULT: FAILED — see amazonmusic.log for details.")
        print("        Common causes:")
        print("        - target playlist doesn't exist (create it first)")
        print("        - Amazon DOM changed (music-image-row[primary-text] selector?)")
        print("        - 'Add to playlist' label changed in context menu")
        print("        - playlist picker UI changed")
        return 1


def main() -> int:
    p = argparse.ArgumentParser(description="Smoke test add_track_by_asin end-to-end.")
    p.add_argument("--album", default=DEFAULT_ALBUM_ASIN,
                   help=f"Album ASIN (default: {DEFAULT_ALBUM_ASIN} — Deep Dive – Dvořák)")
    p.add_argument("--track", default=DEFAULT_TRACK_ASIN,
                   help=f"Track ASIN (default: {DEFAULT_TRACK_ASIN} — Berliner Phil Sym 9 mvt IV)")
    p.add_argument("--playlist", default=DEFAULT_PLAYLIST,
                   help=f"Target playlist name (must already exist; default: {DEFAULT_PLAYLIST!r})")
    p.add_argument("--headless", action="store_true",
                   help="Run Chrome headlessly (not recommended).")
    p.add_argument("--slow-mo", type=int, default=100,
                   help="Playwright slow_mo in ms (default: 100).")
    args = p.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
