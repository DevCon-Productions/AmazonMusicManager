"""Real-mouse click probe.

Tests whether using page.mouse.click(x, y) (routed via CDP Input.dispatchMouseEvent,
which produces event.isTrusted=true) plays row 11 vs the synthetic-JS .click()
we've been using (which sets isTrusted=false). Some Amazon shadow-DOM handlers
may gate on isTrusted.

Procedure:
  1. Navigate to Classical Beethoven Symphony 9.
  2. Pause anything currently playing.
  3. Scroll row 11 so the row's TOP is at y~300 — center of viewport, comfortable
     click target.
  4. Read exact screen coords of row 11's music-playback-button.
  5. page.mouse.click(cx, cy) — real trusted click.
  6. Poll get_now_playing for 8 seconds, report what plays.
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


# Find the screen coords of row 11's music-playback-button (deep through
# shadow DOM). Returns the rect or null.
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
    """Scroll until span.index == target is in viewport."""
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
    """After scroll_row_into_view, the row may be near the edges of the
    viewport. Scroll a bit more so the row's top is near target_y."""
    for _ in range(8):
        loc = await page.evaluate(LOCATE_PLAYBACK_BTN_JS, target_idx_1based)
        if not loc:
            await page.wait_for_timeout(400)
            continue
        row_top = loc["rowRect"]["top"]
        delta = row_top - target_y
        if abs(delta) < 30:
            return loc
        # delta > 0 → row is below target → scroll down (positive wheel)
        # delta < 0 → row is above target → scroll up (negative wheel)
        await page.mouse.wheel(0, int(delta))
        await page.wait_for_timeout(700)
    return await page.evaluate(LOCATE_PLAYBACK_BTN_JS, target_idx_1based)


async def report_now_playing(page, label):
    np = await get_now_playing(page)
    if not np:
        print(f"  [{label}] get_now_playing: None")
        return None
    print(f"  [{label}] title:  {np.get('title', '')!r}")
    print(f"  [{label}] artist: {np.get('artist', '')!r}")
    return np


async def run():
    async with BrowserManager(headless=False, slow_mo=100) as bm:
        await ensure_logged_in(bm.page)
        await bm.page.goto(PLAYLIST_URL, wait_until="domcontentloaded", timeout=30000)
        await bm.page.wait_for_selector('music-image-row[primary-text]', timeout=30000)
        await bm.page.wait_for_timeout(2500)

        print("Pre-test now-playing state:")
        await report_now_playing(bm.page, "before")
        pause_result = await bm.page.evaluate(PAUSE_ONLY_JS)
        print(f"Pause attempt: {pause_result}")
        await bm.page.wait_for_timeout(1500)

        print(f"\nScrolling row {TARGET_IDX_1BASED} into view...")
        if not await scroll_row_into_view(bm.page, TARGET_IDX_1BASED):
            print("Could not scroll into view.")
            return
        await bm.page.wait_for_timeout(800)

        print(f"Centering row {TARGET_IDX_1BASED} at y~300...")
        loc = await center_row_in_viewport(bm.page, TARGET_IDX_1BASED, target_y=300)
        if not loc:
            print("Couldn't locate button after centering.")
            return
        print(f"  row rect:    {loc['rowRect']}")
        print(f"  button rect: {loc['btnRect']}")
        print(f"  row title:   {loc['rowPrimary'][:80]}...")
        print(f"  row artist:  {loc['rowSecondary1']}")

        cx = loc["btnRect"]["left"] + loc["btnRect"]["w"] / 2
        cy = loc["btnRect"]["top"] + loc["btnRect"]["h"] / 2
        print(f"\nDispatching REAL page.mouse.click at ({cx:.0f}, {cy:.0f})...")
        # Move first so any hover state settles, then click.
        await bm.page.mouse.move(cx, cy)
        await bm.page.wait_for_timeout(300)
        await bm.page.mouse.click(cx, cy)

        for t in (2, 4, 6, 8):
            await bm.page.wait_for_timeout(2000)
            print(f"\n  After ~{t}s:")
            np = await report_now_playing(bm.page, f"+{t}s")
            if np:
                title = (np.get("title", "") or "").lower()
                artist = (np.get("artist", "") or "").lower()
                if EXPECTED_TITLE_FRAGMENT.lower() in title and EXPECTED_ARTIST_FRAGMENT.lower() in artist:
                    print(f"\n  ===> MATCH: row 11 (Bernstein) IS playing. Real mouse click works!")
                    return
                if WRONG_ARTIST_FRAGMENT.lower() in artist:
                    print(f"\n  ===> MISS: Karajan (row 1) is playing instead of row 11.")
                    return

        print("\nNo conclusive outcome after 8s. Leaving browser open 10s for manual check.")
        await bm.page.wait_for_timeout(10000)


if __name__ == "__main__":
    asyncio.run(run())
