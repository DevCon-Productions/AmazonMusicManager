"""Fresh-profile experiment.

Hypothesis: our automation's stale browser_data/ pins us to an A/B-test
bucket (or an outdated client build) in which the row's play button plays
row 1 instead of the clicked row. User's personal Chrome with a different
profile correctly plays the clicked row.

This script launches our automation against a TEMPORARY fresh user-data-dir
(no cookies, no localStorage). You'll have to log in to Amazon Music
manually when the browser opens; the script's stdin prompt will wait.

Then it scrolls row 11 of Symphony 9 into view and clicks the
music-playback-button via real page.mouse — same logic as our production
scraper. If row 11 plays here but not in browser_data/, we know the bug is
stale-state and can fix it by clearing browser_data/.

Run with VPN off.
"""
import asyncio
import os
import tempfile

import browser  # so we can patch USER_DATA_DIR before BrowserManager runs
from scraper import _PROBE_VISIBLE_INDEX_JS, get_now_playing


PLAYLIST_URL = "https://music.amazon.com/my/playlists/PLAYLIST-ID-HERE"
TARGET_IDX_1BASED = 11
EXPECTED_TITLE_FRAGMENT = "Allegro ma non troppo"
EXPECTED_ARTIST_FRAGMENT = "Bernstein"
WRONG_ARTIST_FRAGMENT = "Karajan"


PAUSE_ONLY_JS = r"""
    () => {
        const PLAYER_SEL = 'music-player, music-transport, music-horizontal-player, [id*="TransportBar"], [class*="TransportBar"], [class*="transportBar"]';
        const tokenize = (s) => (s || '').split(/[^a-z0-9]+/).filter(Boolean);
        const containsWord = (text, w) => tokenize(text).includes(w);
        const ariaOf = (el) => (el.getAttribute && el.getAttribute('aria-label') || '').toLowerCase();
        const iconOf = (el) => (el.getAttribute && el.getAttribute('icon-name') || '').toLowerCase();
        const isPauseBtn = (el) => containsWord(ariaOf(el), 'pause') || containsWord(iconOf(el), 'pause');
        const walked = new Set();
        const collect = (root, out) => {
            if (!root || walked.has(root)) return;
            walked.add(root);
            const all = root.querySelectorAll ? root.querySelectorAll('*') : [];
            for (const el of all) {
                const tag = (el.tagName || '').toLowerCase();
                if (tag === 'music-button' || tag === 'button') out.push(el);
                if (el.shadowRoot) collect(el.shadowRoot, out);
            }
        };
        const findPlayers = (root, out) => {
            if (!root) return;
            try { for (const p of (root.querySelectorAll ? root.querySelectorAll(PLAYER_SEL) : [])) out.push(p); } catch(e){}
            const all = root.querySelectorAll ? root.querySelectorAll('*') : [];
            for (const el of all) if (el.shadowRoot) findPlayers(el.shadowRoot, out);
        };
        const players = [];
        findPlayers(document, players);
        const candidates = [];
        if (players.length) for (const p of players) collect(p, candidates);
        else collect(document, candidates);
        for (const el of candidates) {
            if (isPauseBtn(el)) {
                try { el.click(); return 'paused'; } catch(e){}
            }
        }
        return 'no-pause-button-found';
    }
"""


LOCATE_PLAYBACK_BTN_JS = r"""
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
            const findBtn = (root, visited = new Set()) => {
                if (!root || visited.has(root)) return null;
                visited.add(root);
                const all = root.querySelectorAll ? root.querySelectorAll('*') : [];
                for (const el of all) {
                    const tag = (el.tagName || '').toLowerCase();
                    if (tag === 'music-playback-button') return el;
                    if (el.shadowRoot) {
                        const f = findBtn(el.shadowRoot, visited);
                        if (f) return f;
                    }
                }
                return null;
            };
            const btn = findBtn(row);
            if (!btn) return null;
            const br = btn.getBoundingClientRect();
            return {
                rowRect: { top: rect.top, left: rect.left, w: rect.width, h: rect.height },
                btnRect: { top: br.top, left: br.left, w: br.width, h: br.height },
                rowPrimary: row.getAttribute('primary-text') || '',
                rowSecondary1: row.getAttribute('secondary-text-1') || '',
            };
        }
        return null;
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


async def center_row_in_viewport(page, target_idx_1based, target_y=300):
    for _ in range(8):
        loc = await page.evaluate(LOCATE_PLAYBACK_BTN_JS, target_idx_1based)
        if not loc:
            await page.wait_for_timeout(400)
            continue
        row_top = loc["rowRect"]["top"]
        delta = row_top - target_y
        if abs(delta) < 30:
            return loc
        await page.mouse.wheel(0, int(delta))
        await page.wait_for_timeout(700)
    return await page.evaluate(LOCATE_PLAYBACK_BTN_JS, target_idx_1based)


async def report_now_playing(page, label):
    np = await get_now_playing(page)
    if not np:
        print(f"  [{label}] None")
        return None
    print(f"  [{label}] title={np.get('title', '')[:70]!r}")
    print(f"  [{label}] artist={np.get('artist', '')[:50]!r}")
    return np


async def run():
    # Allocate a fresh tempdir for the profile. This bypasses our stale
    # browser_data/ entirely.
    fresh_dir = tempfile.mkdtemp(prefix="amzmusic_fresh_")
    print(f"Using fresh user-data-dir: {fresh_dir}")
    browser.USER_DATA_DIR = fresh_dir

    # Import after the patch so any module-level captures pick it up.
    from browser import BrowserManager
    from auth import ensure_logged_in

    async with BrowserManager(headless=False, slow_mo=100) as bm:
        # Print UA for comparison.
        ua = await bm.page.evaluate("navigator.userAgent")
        print(f"\nUser-Agent: {ua}")

        await ensure_logged_in(bm.page)

        await bm.page.goto(PLAYLIST_URL, wait_until="domcontentloaded", timeout=60000)
        await bm.page.wait_for_selector('music-image-row[primary-text]', timeout=60000)
        await bm.page.wait_for_timeout(2500)

        print("\nPre-test now-playing state:")
        await report_now_playing(bm.page, "before")
        print(f"Pause attempt: {await bm.page.evaluate(PAUSE_ONLY_JS)}")
        await bm.page.wait_for_timeout(1500)

        print(f"\nScrolling row {TARGET_IDX_1BASED} into view...")
        if not await scroll_row_into_view(bm.page, TARGET_IDX_1BASED):
            print("scroll failed")
            return
        loc = await center_row_in_viewport(bm.page, TARGET_IDX_1BASED, target_y=300)
        if not loc:
            print("locate failed")
            return
        print(f"  row title: {loc['rowPrimary'][:70]}...")
        print(f"  row artist (should be Bernstein): {loc['rowSecondary1']}")

        cx = loc["btnRect"]["left"] + loc["btnRect"]["w"] / 2
        cy = loc["btnRect"]["top"] + loc["btnRect"]["h"] / 2
        print(f"\nClicking music-playback-button at ({cx:.0f}, {cy:.0f}) via real mouse...")
        await bm.page.mouse.move(cx, cy)
        await bm.page.wait_for_timeout(300)
        await bm.page.mouse.click(cx, cy)

        for t in (2, 4, 6, 8):
            await bm.page.wait_for_timeout(2000)
            print(f"\n  After ~{t}s:")
            np = await report_now_playing(bm.page, f"+{t}s")
            if not np:
                continue
            title = (np.get("title", "") or "").lower()
            artist = (np.get("artist", "") or "").lower()
            if EXPECTED_TITLE_FRAGMENT.lower() in title and EXPECTED_ARTIST_FRAGMENT.lower() in artist:
                print(f"\n  ===> FRESH PROFILE WORKS! Row 11 (Bernstein) is playing.")
                print(f"       Stale browser_data/ was the cause. Action: clear browser_data/ in production.")
                break
            if WRONG_ARTIST_FRAGMENT.lower() in artist:
                print(f"\n  ===> FRESH PROFILE ALSO BROKEN. Karajan/row-1 is playing.")
                print(f"       Stale state is NOT the cause. Need to look elsewhere (UA, account tier, etc.).")
                break

        print(f"\nFresh profile dir kept for inspection: {fresh_dir}")
        print("Browser stays open 15s for manual verification...")
        await bm.page.wait_for_timeout(15000)


if __name__ == "__main__":
    asyncio.run(run())
