"""Diagnostic: inspect the music-image-row at index 4 of the Beethoven PC1 playlist.
Lists all music-button descendants (light + shadow), their icon-name and aria-label,
so we can see where the "play" action lives and update play_track_at_index accordingly.
"""
import asyncio
import json

from auth import ensure_logged_in
from browser import BrowserManager

PLAYLIST_URL = "https://music.amazon.com/my/playlists/PLAYLIST-ID-HERE"
TARGET_IDX_1BASED = 4  # the Brendel "Allegro con brio"

INSPECT_JS = r"""
    (targetIdx) => {
        const rows = document.querySelectorAll('music-image-row[primary-text]');
        const vh = window.innerHeight || document.documentElement.clientHeight;
        for (const row of rows) {
            const rect = row.getBoundingClientRect();
            if (rect.bottom <= 0 || rect.top >= vh) continue;
            const idxEl = row.querySelector('span.index')
                || (row.shadowRoot && row.shadowRoot.querySelector('span.index'));
            if (!idxEl) continue;
            const n = parseInt((idxEl.textContent || '').trim(), 10);
            if (n !== targetIdx) continue;

            // Collect all descendants from light DOM and shadow DOM
            const findAll = (root, results = [], visited = new Set()) => {
                if (!root || visited.has(root)) return results;
                visited.add(root);
                const children = root.querySelectorAll ? root.querySelectorAll('*') : [];
                for (const el of children) {
                    const tag = (el.tagName || '').toLowerCase();
                    const icon = el.getAttribute && el.getAttribute('icon-name');
                    const aria = el.getAttribute && el.getAttribute('aria-label');
                    const role = el.getAttribute && el.getAttribute('role');
                    if (tag === 'music-button' || tag === 'button' || icon || (aria && aria.length)) {
                        results.push({
                            tag, icon: icon || '', aria: aria || '', role: role || '',
                            classes: (el.className || '').toString().slice(0, 80),
                        });
                    }
                    if (el.shadowRoot) findAll(el.shadowRoot, results, visited);
                }
                return results;
            };
            const buttons = findAll(row);
            const tag = row.tagName.toLowerCase();
            return {
                rowTag: tag,
                primaryText: row.getAttribute('primary-text') || '',
                secondaryText: row.getAttribute('secondary-text-1') || '',
                rect: { top: rect.top, left: rect.left, w: rect.width, h: rect.height },
                buttonCount: buttons.length,
                buttons: buttons.slice(0, 40),
            };
        }
        return null;
    }
"""


async def run():
    async with BrowserManager(headless=False, slow_mo=100) as bm:
        await ensure_logged_in(bm.page)
        await bm.page.goto(PLAYLIST_URL, wait_until="domcontentloaded", timeout=30000)
        await bm.page.wait_for_selector('music-image-row[primary-text]', timeout=30000)
        await bm.page.wait_for_timeout(2500)

        # First pass: no hover.
        info_no_hover = await bm.page.evaluate(INSPECT_JS, TARGET_IDX_1BASED)
        print("\n=== BEFORE HOVER ===")
        print(json.dumps(info_no_hover, indent=2))

        if not info_no_hover:
            print("Could not find target row.")
            return

        # Hover the row's center.
        rect = info_no_hover["rect"]
        cx = rect["left"] + rect["w"] / 2
        cy = rect["top"] + rect["h"] / 2
        await bm.page.mouse.move(cx, cy)
        await bm.page.wait_for_timeout(1200)

        info_hover = await bm.page.evaluate(INSPECT_JS, TARGET_IDX_1BASED)
        print("\n=== AFTER HOVER ===")
        print(json.dumps(info_hover, indent=2))


if __name__ == "__main__":
    asyncio.run(run())
