"""Right-click context menu probe (Investigation task 2 / 4).

Right-clicks row 11 of Classical Beethoven Symphony 9 and enumerates the
resulting context menu. We're looking for Amazon's vocabulary around
"Play this song" / "Play from here" so we can find the canonical row-start
selector.

Non-destructive: never triggers playback. Dismisses the menu with ESC.
"""
import asyncio
import json

from auth import ensure_logged_in
from browser import BrowserManager
from scraper import _PROBE_VISIBLE_INDEX_JS

PLAYLIST_URL = "https://music.amazon.com/my/playlists/PLAYLIST-ID-HERE"
TARGET_IDX_1BASED = 11


# Locate row 11's center coords for right-clicking.
LOCATE_ROW_JS = r"""
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
            return {
                top: rect.top, left: rect.left,
                w: rect.width, h: rect.height,
                primaryText: row.getAttribute('primary-text') || '',
            };
        }
        return null;
    }
"""


# Walks the entire document (incl. shadow DOMs) looking for menu-ish
# elements that appeared post-right-click. We cast a wide net since Amazon
# may use a custom element name.
ENUMERATE_MENU_JS = r"""
    () => {
        const hits = [];
        const visited = new Set();

        const walk = (root) => {
            if (!root || visited.has(root)) return;
            visited.add(root);
            const all = root.querySelectorAll ? root.querySelectorAll('*') : [];
            for (const el of all) {
                const tag = (el.tagName || '').toLowerCase();
                const role = el.getAttribute && el.getAttribute('role');
                const aria = el.getAttribute && el.getAttribute('aria-label');
                const text = (el.textContent || '').trim().slice(0, 120);
                const cs = el.getBoundingClientRect ? el.getBoundingClientRect() : null;
                const visible = cs ? (cs.width > 0 && cs.height > 0) : false;

                const looksLikeMenuContainer = (
                    tag.includes('context-menu') ||
                    tag.includes('menu') ||
                    role === 'menu' ||
                    (el.className && /context|menu|popover|popup/i.test(el.className.toString()))
                );
                const looksLikeMenuItem = (
                    role === 'menuitem' ||
                    tag === 'music-context-menu-item' ||
                    tag === 'music-list-item' ||
                    tag === 'music-context-menu-group'
                );

                if (visible && (looksLikeMenuContainer || looksLikeMenuItem)) {
                    hits.push({
                        tag, role: role || '',
                        aria: aria || '',
                        text,
                        classes: (el.className || '').toString().slice(0, 100),
                        rect: cs ? { top: Math.round(cs.top), left: Math.round(cs.left), w: Math.round(cs.width), h: Math.round(cs.height) } : null,
                        isContainer: looksLikeMenuContainer,
                        isItem: looksLikeMenuItem,
                    });
                }
                if (el.shadowRoot) walk(el.shadowRoot);
            }
        };
        walk(document);
        return hits;
    }
"""


async def scroll_row_into_view(page, target_idx_1based, max_scrolls=80):
    last_v_max = -1
    stuck_count = 0
    for _ in range(max_scrolls):
        info = await page.evaluate(_PROBE_VISIBLE_INDEX_JS, target_idx_1based)
        if info["found"]:
            return True
        v_min, v_max = info["visMin"], info["visMax"]
        if v_min is None:
            await page.wait_for_timeout(500)
            continue
        if target_idx_1based > v_max:
            if v_max == last_v_max:
                stuck_count += 1
                if stuck_count >= 3:
                    return False
            else:
                stuck_count = 0
                last_v_max = v_max
            await page.mouse.wheel(0, 1500)
        elif target_idx_1based < v_min:
            await page.mouse.wheel(0, -1500)
        else:
            await page.wait_for_timeout(500)
        await page.wait_for_timeout(700)
    return False


async def run():
    async with BrowserManager(headless=False, slow_mo=100) as bm:
        await ensure_logged_in(bm.page)
        await bm.page.goto(PLAYLIST_URL, wait_until="domcontentloaded", timeout=30000)
        await bm.page.wait_for_selector('music-image-row[primary-text]', timeout=30000)
        await bm.page.wait_for_timeout(2500)

        print(f"Scrolling row {TARGET_IDX_1BASED} into view...")
        if not await scroll_row_into_view(bm.page, TARGET_IDX_1BASED):
            print("Could not scroll target row into view.")
            return
        await bm.page.wait_for_timeout(800)

        loc = await bm.page.evaluate(LOCATE_ROW_JS, TARGET_IDX_1BASED)
        if not loc:
            print("Target row not findable after scroll.")
            return
        print(f"Row 11 found: '{loc['primaryText'][:80]}...'")
        print(f"  rect: top={loc['top']:.0f}, left={loc['left']:.0f}, w={loc['w']:.0f}, h={loc['h']:.0f}")

        # If row's top is negative the click point might be off-screen — clamp
        # the y to a positive in-row coordinate. Aim for ~30% into row height
        # to land on the title area, away from the play-button column.
        cx = loc["left"] + loc["w"] * 0.5
        cy_raw = loc["top"] + loc["h"] * 0.7
        cy = max(loc["top"] + loc["h"] * 0.5, 10) if loc["top"] < 0 else cy_raw
        if cy < 10:
            cy = max(10, loc["top"] + loc["h"] - 5)
        print(f"Right-clicking at ({cx:.0f}, {cy:.0f})...")

        await bm.page.mouse.move(cx, cy)
        await bm.page.wait_for_timeout(300)
        await bm.page.mouse.click(cx, cy, button="right")
        await bm.page.wait_for_timeout(1500)

        hits = await bm.page.evaluate(ENUMERATE_MENU_JS)
        print(f"\n=== MENU ELEMENTS FOUND: {len(hits)} ===")
        # Print containers first, then items
        containers = [h for h in hits if h["isContainer"] and not h["isItem"]]
        items = [h for h in hits if h["isItem"]]
        others = [h for h in hits if not h["isContainer"] and not h["isItem"]]

        print(f"\n--- Containers ({len(containers)}) ---")
        for c in containers:
            print(f"  tag={c['tag']:<28} role={c['role']:<10} classes={c['classes']:<40}")
            print(f"    rect={c['rect']}")
            if c["aria"]:
                print(f"    aria={c['aria'][:100]}")

        print(f"\n--- Items ({len(items)}) ---")
        for it in items:
            print(f"  tag={it['tag']:<28} role={it['role']:<10}")
            print(f"    text={it['text'][:100]!r}")
            if it["aria"]:
                print(f"    aria={it['aria'][:100]!r}")

        # Also dump the raw JSON in case we missed structure.
        print("\n=== RAW JSON ===")
        print(json.dumps(hits, indent=2)[:8000])

        # Dismiss the menu cleanly.
        await bm.page.keyboard.press("Escape")
        await bm.page.wait_for_timeout(500)

        print("\nKeeping browser open 10s so you can manually verify...")
        await bm.page.wait_for_timeout(10000)


if __name__ == "__main__":
    asyncio.run(run())
