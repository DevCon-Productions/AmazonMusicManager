"""Hover-state probe (Investigation task 1 / 4).

Targets row 11 of "Classical Beethoven Symphony 9" — the multi-performance
playlist where music-playback-button click misbehaves (plays row 1, not the
clicked row). Captures the row's clickable enumeration BEFORE and AFTER
hovering the row, so we can spot any play-overlay that only renders on hover.

Run with VPN OFF and Amazon Music logged in (login session persists in
browser_data/).
"""
import asyncio
import json

from auth import ensure_logged_in
from browser import BrowserManager
from scraper import _PROBE_VISIBLE_INDEX_JS

PLAYLIST_URL = "https://music.amazon.com/my/playlists/PLAYLIST-ID-HERE"
TARGET_IDX_1BASED = 11

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
                        const cs = el.getBoundingClientRect ? el.getBoundingClientRect() : null;
                        results.push({
                            tag,
                            icon: icon || '',
                            aria: aria || '',
                            role: role || '',
                            classes: (el.className || '').toString().slice(0, 80),
                            visible: cs ? (cs.width > 0 && cs.height > 0) : false,
                            rect: cs ? { top: Math.round(cs.top), left: Math.round(cs.left), w: Math.round(cs.width), h: Math.round(cs.height) } : null,
                        });
                    }
                    if (el.shadowRoot) findAll(el.shadowRoot, results, visited);
                }
                return results;
            };
            const buttons = findAll(row);
            return {
                primaryText: row.getAttribute('primary-text') || '',
                secondaryText1: row.getAttribute('secondary-text-1') || '',
                secondaryText2: row.getAttribute('secondary-text-2') || '',
                rect: { top: rect.top, left: rect.left, w: rect.width, h: rect.height },
                indexSpanText: (idxEl.textContent || '').trim(),
                buttonCount: buttons.length,
                buttons,
            };
        }
        return null;
    }
"""


async def scroll_row_into_view(page, target_idx_1based, max_scrolls=80):
    """Mirror of scraper.scroll_and_click_row_at_index's scroll phase only.
    Stops once span.index == target is visible.
    """
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
                    print(f"Scroll stalled at row {v_max}; target {target_idx_1based} unreachable.")
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

        info_no_hover = await bm.page.evaluate(INSPECT_JS, TARGET_IDX_1BASED)
        print("\n=== BEFORE HOVER ===")
        print(json.dumps(info_no_hover, indent=2))

        if not info_no_hover:
            print("Could not find target row after scroll. Aborting.")
            return

        rect = info_no_hover["rect"]
        cx = rect["left"] + rect["w"] / 2
        cy = rect["top"] + rect["h"] / 2
        print(f"\nHovering row at ({cx:.0f}, {cy:.0f})...")
        await bm.page.mouse.move(cx, cy)
        await bm.page.wait_for_timeout(1200)

        info_hover = await bm.page.evaluate(INSPECT_JS, TARGET_IDX_1BASED)
        print("\n=== AFTER HOVER ===")
        print(json.dumps(info_hover, indent=2))

        # Diff summary — what buttons appeared, what changed.
        before_keys = {(b["tag"], b["icon"], b["aria"]) for b in (info_no_hover["buttons"] if info_no_hover else [])}
        after_keys = {(b["tag"], b["icon"], b["aria"]) for b in (info_hover["buttons"] if info_hover else [])}
        appeared = after_keys - before_keys
        disappeared = before_keys - after_keys
        print("\n=== DIFF ===")
        print(f"Appeared on hover ({len(appeared)}):")
        for k in sorted(appeared):
            print(f"  + tag={k[0]:<22} icon={k[1]:<22} aria={k[2]}")
        print(f"Disappeared on hover ({len(disappeared)}):")
        for k in sorted(disappeared):
            print(f"  - tag={k[0]:<22} icon={k[1]:<22} aria={k[2]}")

        # Hover-visible buttons whose rect overlaps the row's track-number column
        # (left ~30-80px of row). That's where a hover-only play icon would
        # appear if Amazon follows the common streaming-app pattern.
        if info_hover and info_hover.get("buttons"):
            row_left = info_hover["rect"]["left"]
            print("\n=== CANDIDATES IN TRACK-NUMBER COLUMN (after hover) ===")
            for b in info_hover["buttons"]:
                if not b.get("visible") or not b.get("rect"):
                    continue
                br = b["rect"]
                # roughly first 100px of the row from its left edge
                if br["left"] < row_left + 100 and br["w"] > 0:
                    print(f"  tag={b['tag']:<22} icon={b['icon']:<22} aria={b['aria']:<40} rect={br}")

        print("\nKeeping browser open 10s so you can manually verify...")
        await bm.page.wait_for_timeout(10000)


if __name__ == "__main__":
    asyncio.run(run())
