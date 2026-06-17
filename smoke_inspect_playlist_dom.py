"""Diagnostic: what rows exist in the playlist DOM right now and at what positions?

Helps explain the scroll oscillation observed in smoke_compare_rows_9_11.py
(visMin=1, visMax=5 alternating with visMin=11, visMax=15, never seeing rows
6-10). Possibilities to test: rows 6-10 don't exist; rows 6-10 are off-screen
in a specific direction; rows 6-10 are present but with weird rects.
"""
import asyncio

from auth import ensure_logged_in
from browser import BrowserManager

PLAYLIST_URL = "https://music.amazon.com/my/playlists/PLAYLIST-ID-HERE"


DUMP_ALL_ROWS_JS = r"""
    () => {
        const rows = document.querySelectorAll('music-image-row[primary-text]');
        const vh = window.innerHeight || document.documentElement.clientHeight;
        const out = [];
        for (const row of rows) {
            const rect = row.getBoundingClientRect();
            const idxEl = row.querySelector('span.index')
                || (row.shadowRoot && row.shadowRoot.querySelector('span.index'));
            const idxText = idxEl ? (idxEl.textContent || '').trim() : null;
            out.push({
                idxText,
                primaryText: (row.getAttribute('primary-text') || '').slice(0, 60),
                secondaryText1: (row.getAttribute('secondary-text-1') || '').slice(0, 40),
                rect: { top: Math.round(rect.top), h: Math.round(rect.height), w: Math.round(rect.width) },
                inViewport: rect.bottom > 0 && rect.top < vh && rect.width > 0 && rect.height > 0,
            });
        }
        return { vh, totalRows: rows.length, rows: out };
    }
"""


async def run():
    async with BrowserManager(headless=False, slow_mo=100) as bm:
        await ensure_logged_in(bm.page)
        await bm.page.goto(PLAYLIST_URL, wait_until="domcontentloaded", timeout=30000)
        await bm.page.wait_for_selector('music-image-row[primary-text]', timeout=30000)
        await bm.page.wait_for_timeout(2500)

        for tag, sleep_ms in [("initial", 0), ("after 3s", 3000)]:
            if sleep_ms:
                await bm.page.wait_for_timeout(sleep_ms)
            info = await bm.page.evaluate(DUMP_ALL_ROWS_JS)
            print(f"\n=== {tag} state ===  vh={info['vh']}  totalRowsInDOM={info['totalRows']}")
            print(f"{'idx':>5} {'top':>6} {'h':>4} {'w':>4} {'inVP':<5}  primaryText[:60]")
            for r in info["rows"]:
                print(f"{r['idxText']!s:>5} {r['rect']['top']:>6} {r['rect']['h']:>4} {r['rect']['w']:>4} {str(r['inViewport']):<5}  {r['primaryText']}")

        print("\nKeeping browser open 10s for manual inspection...")
        await bm.page.wait_for_timeout(10000)


if __name__ == "__main__":
    asyncio.run(run())
