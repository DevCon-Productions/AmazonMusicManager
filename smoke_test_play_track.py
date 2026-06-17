"""Smoke test for scraper.play_track_at_index.

Plays track at index 3 (0-based) of the Beethoven Piano Concerto 1 playlist.
Expected: Brendel/Levine "Allegro con brio" (the 4th track) starts playing.
"""
import asyncio

from auth import ensure_logged_in
from browser import BrowserManager
from scraper import play_track_at_index, get_now_playing
from utils import setup_logging

setup_logging()

PLAYLIST_URL = "https://music.amazon.com/my/playlists/PLAYLIST-ID-HERE"
PLAYLIST_NAME = "Classical Beethoven Piano Concerto 1"
TARGET_INDEX = 3  # 0-based; expected: "1. Allegro con brio" by Alfred Brendel et al.


async def run():
    async with BrowserManager(headless=False, slow_mo=100) as bm:
        await ensure_logged_in(bm.page)

        ok = await play_track_at_index(bm.page, PLAYLIST_URL, TARGET_INDEX)
        print(f"\n  play_track_at_index returned: {ok}")
        if not ok:
            print("  Smoke test FAILED — could not click the row.")
            return

        # Give Amazon a moment to start streaming the track.
        await bm.page.wait_for_timeout(4000)

        np = await get_now_playing(bm.page)
        print(f"\n  get_now_playing: {np}")
        if np:
            print(f"\n  Now playing: '{np.get('title', '')}' by '{np.get('artist', '')}'")
            print(f"  Expected   : 'Beethoven: Piano Concerto No. 1 ... Allegro con brio'")
            print(f"               'Alfred Brendel, ... James Levine'")
        else:
            print("  No track detected in player bar — playback may not have started.")

        # Hold the browser open a bit so the user can hear/see playback,
        # then close so this script can exit non-interactively.
        print("\n  Holding playback for 6s before closing browser...")
        await bm.page.wait_for_timeout(6000)
        np2 = await get_now_playing(bm.page)
        print(f"  get_now_playing (after 6s): {np2}")


if __name__ == "__main__":
    asyncio.run(run())
