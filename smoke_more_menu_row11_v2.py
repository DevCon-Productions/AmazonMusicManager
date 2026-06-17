"""Row-menu probe v2 — extract actual labels from each music-list-item.

v1 found 10 menuitems but their textContent was empty because labels live in
the shadow DOM via slots. v2 walks each menuitem's full subtree (light + shadow)
to extract whatever text exists.
"""
import asyncio
import json

from auth import ensure_logged_in
from browser import BrowserManager
from scraper import _PROBE_VISIBLE_INDEX_JS

PLAYLIST_URL = "https://music.amazon.com/my/playlists/PLAYLIST-ID-HERE"
TARGET_IDX_1BASED = 11


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
                btnRect: { top: br.top, left: br.left, w: br.width, h: br.height },
                rowRect: { top: rect.top, left: rect.left, w: rect.width, h: rect.height },
            };
        }
        return null;
    }
"""


# Deep extraction: for each music-list-item[role=menuitem], dump all attributes
# AND all text from its subtree (light + shadow + slotted).
ENUMERATE_MENU_DEEP_JS = r"""
    () => {
        const items = [];
        const visited = new Set();

        const collectText = (root, acc, seen) => {
            if (!root || seen.has(root)) return;
            seen.add(root);
            // Direct text nodes
            const tw = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
            let n;
            while ((n = tw.nextNode())) {
                const t = (n.textContent || '').trim();
                if (t) acc.push(t);
            }
            // Recurse into shadow roots
            const all = root.querySelectorAll ? root.querySelectorAll('*') : [];
            for (const el of all) {
                if (el.shadowRoot) collectText(el.shadowRoot, acc, seen);
                // Capture <slot> assigned nodes
                if ((el.tagName || '').toLowerCase() === 'slot') {
                    try {
                        const assigned = el.assignedNodes({ flatten: true }) || [];
                        for (const a of assigned) {
                            if (a.nodeType === Node.TEXT_NODE) {
                                const t = (a.textContent || '').trim();
                                if (t) acc.push(t);
                            } else if (a.nodeType === Node.ELEMENT_NODE) {
                                collectText(a, acc, seen);
                            }
                        }
                    } catch (e) {}
                }
            }
        };

        const collectAttrs = (el) => {
            const out = {};
            if (!el.attributes) return out;
            for (const a of el.attributes) out[a.name] = a.value;
            return out;
        };

        const walk = (root) => {
            if (!root || visited.has(root)) return;
            visited.add(root);
            const all = root.querySelectorAll ? root.querySelectorAll('*') : [];
            for (const el of all) {
                const tag = (el.tagName || '').toLowerCase();
                const role = el.getAttribute && el.getAttribute('role');
                if (tag === 'music-list-item' && role === 'menuitem') {
                    const cs = el.getBoundingClientRect();
                    if (cs.width <= 0 || cs.height <= 0) continue;
                    const acc = [];
                    collectText(el, acc, new Set());
                    items.push({
                        attrs: collectAttrs(el),
                        text: acc.join(' | '),
                        rect: { top: Math.round(cs.top), left: Math.round(cs.left), w: Math.round(cs.width), h: Math.round(cs.height) },
                        innerHTML: (el.innerHTML || '').slice(0, 400),
                    });
                }
                if (el.shadowRoot) walk(el.shadowRoot);
            }
        };
        walk(document);
        return items;
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

        if not await scroll_row_into_view(bm.page, TARGET_IDX_1BASED):
            print("Could not scroll target row into view.")
            return
        await bm.page.wait_for_timeout(800)

        loc = await bm.page.evaluate(LOCATE_MORE_BUTTON_JS, TARGET_IDX_1BASED)
        if not loc:
            print("Could not find row 11 or its more-button.")
            return

        # Scroll up so menu has room
        if loc["rowRect"]["top"] < 50:
            await bm.page.mouse.wheel(0, -200)
            await bm.page.wait_for_timeout(800)
            loc = await bm.page.evaluate(LOCATE_MORE_BUTTON_JS, TARGET_IDX_1BASED)
            if not loc:
                print("Lost row 11 after adjustment.")
                return

        bx = loc["btnRect"]["left"] + loc["btnRect"]["w"] / 2
        by = loc["btnRect"]["top"] + loc["btnRect"]["h"] / 2
        print(f"Clicking 'more' button at ({bx:.0f}, {by:.0f})...")
        await bm.page.mouse.click(bx, by)
        await bm.page.wait_for_timeout(1500)

        items = await bm.page.evaluate(ENUMERATE_MENU_DEEP_JS)
        print(f"\n=== {len(items)} menuitems extracted ===\n")
        for i, it in enumerate(items):
            print(f"--- Item {i + 1} ---")
            print(f"  text:  {it['text']!r}")
            print(f"  attrs: {it['attrs']}")
            print(f"  rect:  {it['rect']}")
            print(f"  innerHTML[:400]: {it['innerHTML']!r}")
            print()

        await bm.page.keyboard.press("Escape")
        await bm.page.wait_for_timeout(500)
        print("Keeping browser open 10s...")
        await bm.page.wait_for_timeout(10000)


if __name__ == "__main__":
    asyncio.run(run())
