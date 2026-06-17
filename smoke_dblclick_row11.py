"""Double-click probe (Investigation task 3).

Dispatches dblclick on row 11's body. If Amazon's row-handler responds to
dblclick with "play from here," we win cheaply. Tests both:
  A) Synthetic JS row.dispatchEvent(new MouseEvent('dblclick'))
  B) Real page.mouse.dblclick() on the row body (trusted event)

Run in sequence with pause between attempts so we can attribute outcomes.
"""
import asyncio

from auth import ensure_logged_in
from browser import BrowserManager
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
                rect: { top: rect.top, left: rect.left, w: rect.width, h: rect.height },
                primary: row.getAttribute('primary-text') || '',
                secondary1: row.getAttribute('secondary-text-1') || '',
            };
        }
        return null;
    }
"""


# Synthetic dblclick via JS on the matched row.
SYNTH_DBLCLICK_JS = r"""
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
            try {
                row.dispatchEvent(new MouseEvent('dblclick', {
                    bubbles: true, cancelable: true, view: window,
                }));
                return 'dispatched';
            } catch (e) { return 'error: ' + e.message; }
        }
        return 'no-row-found';
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


async def center_row(page, target_idx_1based, target_y=300):
    for _ in range(8):
        loc = await page.evaluate(LOCATE_ROW_JS, target_idx_1based)
        if not loc:
            await page.wait_for_timeout(400)
            continue
        row_top = loc["rect"]["top"]
        delta = row_top - target_y
        if abs(delta) < 30:
            return loc
        await page.mouse.wheel(0, int(delta))
        await page.wait_for_timeout(700)
    return await page.evaluate(LOCATE_ROW_JS, target_idx_1based)


async def report_np(page, label):
    np = await get_now_playing(page)
    if not np:
        print(f"  [{label}] None")
        return None
    print(f"  [{label}] title={np.get('title', '')[:70]!r} artist={np.get('artist', '')[:50]!r}")
    return np


def evaluate(label, np):
    if not np:
        return "no track info"
    title = (np.get("title", "") or "").lower()
    artist = (np.get("artist", "") or "").lower()
    if EXPECTED_TITLE_FRAGMENT.lower() in title and EXPECTED_ARTIST_FRAGMENT.lower() in artist:
        return f"{label}: MATCH — row 11 plays!"
    if WRONG_ARTIST_FRAGMENT.lower() in artist:
        return f"{label}: MISS — Karajan (row 1)"
    return f"{label}: unclear — title={title[:60]!r} artist={artist[:50]!r}"


async def run():
    async with BrowserManager(headless=False, slow_mo=100) as bm:
        await ensure_logged_in(bm.page)
        await bm.page.goto(PLAYLIST_URL, wait_until="domcontentloaded", timeout=30000)
        await bm.page.wait_for_selector('music-image-row[primary-text]', timeout=30000)
        await bm.page.wait_for_timeout(2500)

        print("=== Setup ===")
        await report_np(bm.page, "before")
        print(f"Pause: {await bm.page.evaluate(PAUSE_ONLY_JS)}")
        await bm.page.wait_for_timeout(1200)

        if not await scroll_row_into_view(bm.page, TARGET_IDX_1BASED):
            print("scroll failed")
            return
        loc = await center_row(bm.page, TARGET_IDX_1BASED, target_y=300)
        if not loc:
            print("center failed")
            return
        print(f"Row centered at top={loc['rect']['top']:.0f}, title={loc['primary'][:60]!r}")

        # --- Test A: synthetic dblclick via JS ---
        print("\n=== Test A: synthetic JS dblclick ===")
        result_a = await bm.page.evaluate(SYNTH_DBLCLICK_JS, TARGET_IDX_1BASED)
        print(f"Dispatch result: {result_a}")
        await bm.page.wait_for_timeout(4000)
        np_a = await report_np(bm.page, "after-A")
        verdict_a = evaluate("Test A (synthetic dblclick)", np_a)
        print(f"  => {verdict_a}")

        # Pause/reset between tests
        await bm.page.wait_for_timeout(800)
        print(f"\nPause-reset: {await bm.page.evaluate(PAUSE_ONLY_JS)}")
        await bm.page.wait_for_timeout(1500)
        # Re-center in case scroll position drifted (paused state shouldn't move it).
        loc = await center_row(bm.page, TARGET_IDX_1BASED, target_y=300)
        if not loc:
            print("re-center failed")
            return

        # --- Test B: real page.mouse.dblclick on row body ---
        # Click in the MIDDLE of the row (x=600) to land on the title area,
        # NOT on the play button (left=96, w=56 → x≤152).
        cx = loc["rect"]["left"] + 600
        cy = loc["rect"]["top"] + loc["rect"]["h"] / 2
        print(f"\n=== Test B: real page.mouse.dblclick at ({cx:.0f}, {cy:.0f}) ===")
        await bm.page.mouse.move(cx, cy)
        await bm.page.wait_for_timeout(300)
        await bm.page.mouse.dblclick(cx, cy)
        await bm.page.wait_for_timeout(4000)
        np_b = await report_np(bm.page, "after-B")
        verdict_b = evaluate("Test B (real dblclick)", np_b)
        print(f"  => {verdict_b}")

        print("\n=== Summary ===")
        print(verdict_a)
        print(verdict_b)
        print("\nKeeping browser open 10s...")
        await bm.page.wait_for_timeout(10000)


if __name__ == "__main__":
    asyncio.run(run())
