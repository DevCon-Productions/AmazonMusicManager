"""Row-menu probe (Investigation task 2, alt path).

Right-click on the row produced no menu (Amazon Music suppresses it). The
hover probe revealed each row has a `music-button[icon-name="more"]` ("...")
at the right edge with inner `button[aria-label="Opens menu"]`. Click THAT
and enumerate the resulting menu — that's almost certainly where the
"Play this song" / "Play from here" entry lives.

Non-destructive: never triggers playback. Dismisses with ESC.
"""
import asyncio
import json

from auth import ensure_logged_in
from browser import BrowserManager
from scraper import _PROBE_VISIBLE_INDEX_JS

PLAYLIST_URL = "https://music.amazon.com/my/playlists/PLAYLIST-ID-HERE"
TARGET_IDX_1BASED = 11


# Locate the "more" (...) button for the target row. Returns its center
# coords so we can click it via the mouse API.
LOCATE_MORE_BUTTON_JS = r"""
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

            // Search row light + shadow DOM for music-button with icon-name=more
            const findMore = (root, visited = new Set()) => {
                if (!root || visited.has(root)) return null;
                visited.add(root);
                const all = root.querySelectorAll ? root.querySelectorAll('*') : [];
                for (const el of all) {
                    const tag = (el.tagName || '').toLowerCase();
                    const icon = el.getAttribute && el.getAttribute('icon-name');
                    if (tag === 'music-button' && icon === 'more') return el;
                    if (el.shadowRoot) {
                        const found = findMore(el.shadowRoot, visited);
                        if (found) return found;
                    }
                }
                return null;
            };
            const moreBtn = findMore(row);
            if (!moreBtn) return null;
            const br = moreBtn.getBoundingClientRect();
            return {
                rowPrimary: row.getAttribute('primary-text') || '',
                btnRect: { top: br.top, left: br.left, w: br.width, h: br.height },
                rowRect: { top: rect.top, left: rect.left, w: rect.width, h: rect.height },
            };
        }
        return null;
    }
"""


# Same wide-net menu enumeration as smoke_rightclick_row11.py.
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
                const text = (el.textContent || '').trim().slice(0, 200);
                const cs = el.getBoundingClientRect ? el.getBoundingClientRect() : null;
                const visible = cs ? (cs.width > 0 && cs.height > 0) : false;

                const looksLikeMenuContainer = (
                    tag.includes('context-menu') ||
                    tag.includes('popover') ||
                    tag.includes('dropdown') ||
                    role === 'menu' ||
                    (el.className && /context|menu|popover|popup|dropdown/i.test(el.className.toString()))
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

        loc = await bm.page.evaluate(LOCATE_MORE_BUTTON_JS, TARGET_IDX_1BASED)
        if not loc:
            print("Could not find row 11 or its 'more' button.")
            return
        print(f"Row 11 found: '{loc['rowPrimary'][:80]}...'")
        print(f"  more-button rect: {loc['btnRect']}")
        print(f"  row rect:         {loc['rowRect']}")

        # The "more" button is at the right edge of the row. If row is
        # partially scrolled, button rect may also be partially off-screen.
        # Scroll the row into the *middle* of the viewport first so the
        # menu (which may render below the button) has space.
        if loc["rowRect"]["top"] < 50:
            print("Row near top of viewport — scrolling up a bit so menu has space below the button...")
            await bm.page.mouse.wheel(0, -200)
            await bm.page.wait_for_timeout(800)
            loc = await bm.page.evaluate(LOCATE_MORE_BUTTON_JS, TARGET_IDX_1BASED)
            if not loc:
                print("Lost row 11 after adjustment scroll.")
                return
            print(f"  re-located more-button rect: {loc['btnRect']}")
            print(f"  re-located row rect:         {loc['rowRect']}")

        bx = loc["btnRect"]["left"] + loc["btnRect"]["w"] / 2
        by = loc["btnRect"]["top"] + loc["btnRect"]["h"] / 2
        print(f"Clicking 'more' button at ({bx:.0f}, {by:.0f})...")
        await bm.page.mouse.move(bx, by)
        await bm.page.wait_for_timeout(300)
        await bm.page.mouse.click(bx, by)
        await bm.page.wait_for_timeout(1500)

        hits = await bm.page.evaluate(ENUMERATE_MENU_JS)
        print(f"\n=== MENU ELEMENTS FOUND: {len(hits)} ===")

        containers = [h for h in hits if h["isContainer"] and not h["isItem"]]
        items = [h for h in hits if h["isItem"]]

        print(f"\n--- Containers ({len(containers)}) ---")
        for c in containers:
            print(f"  tag={c['tag']:<32} role={c['role']:<10} classes={c['classes'][:60]}")
            print(f"    rect={c['rect']}")

        print(f"\n--- Items ({len(items)}) ---")
        for it in items:
            print(f"  tag={it['tag']:<32} role={it['role']:<10} rect={it['rect']}")
            print(f"    text={it['text'][:140]!r}")
            if it["aria"]:
                print(f"    aria={it['aria'][:140]!r}")

        # Dump raw if something interesting is hiding in text/classes.
        print("\n=== RAW JSON (first 10000 chars) ===")
        print(json.dumps(hits, indent=2)[:10000])

        await bm.page.keyboard.press("Escape")
        await bm.page.wait_for_timeout(500)

        print("\nKeeping browser open 12s so you can manually verify...")
        await bm.page.wait_for_timeout(12000)


if __name__ == "__main__":
    asyncio.run(run())
