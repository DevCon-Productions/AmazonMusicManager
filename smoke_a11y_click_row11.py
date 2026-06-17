"""A11y wrapper click probe (final hypothesis test).

Every music-image-row has a `div[role="button"]` with class "a11y" whose
aria-label concatenates title+artist+album. That's the canonical
screen-reader "activate this row" target. If clicking it plays row 11
(Bernstein), we've found the row-start path.

This IS destructive — it triggers playback. Run with VPN off and
Amazon Music logged in.
"""
import asyncio

from auth import ensure_logged_in
from browser import BrowserManager
from scraper import _PROBE_VISIBLE_INDEX_JS, get_now_playing

PLAYLIST_URL = "https://music.amazon.com/my/playlists/PLAYLIST-ID-HERE"
TARGET_IDX_1BASED = 11
EXPECTED_TITLE_FRAGMENT = "Allegro ma non troppo"  # Bernstein's mvt 1
EXPECTED_ARTIST_FRAGMENT = "Bernstein"
WRONG_TITLE_FRAGMENT = "Karajan"  # if we hear this, we hit row 1 again


# Clicks the persistent-player pause button ONLY when it's visible (i.e. only
# when something is currently playing). No-op if already paused. Cribbed from
# gui.py _PLAYER_ACTION_JS pause_only mode.
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


# Find the row, click its div[role=button].a11y, return what we did.
CLICK_A11Y_JS = r"""
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

            const findA11y = (root, visited = new Set()) => {
                if (!root || visited.has(root)) return null;
                visited.add(root);
                const all = root.querySelectorAll ? root.querySelectorAll('*') : [];
                for (const el of all) {
                    const tag = (el.tagName || '').toLowerCase();
                    const role = el.getAttribute && el.getAttribute('role');
                    const cls = ((el.className || '') + '').toString();
                    if (tag === 'div' && role === 'button' && /\ba11y\b/.test(cls)) return el;
                    if (el.shadowRoot) {
                        const f = findA11y(el.shadowRoot, visited);
                        if (f) return f;
                    }
                }
                return null;
            };
            const a11y = findA11y(row);
            if (!a11y) return { found: false };
            const ar = a11y.getBoundingClientRect();
            const aria = a11y.getAttribute('aria-label') || '';
            // Try a direct .click()
            try { a11y.click(); } catch (e) {}
            return {
                found: true,
                aria: aria,
                rect: { top: ar.top, left: ar.left, w: ar.width, h: ar.height },
                rowPrimary: row.getAttribute('primary-text') || '',
                rowSecondary1: row.getAttribute('secondary-text-1') || '',
                rowSecondary2: row.getAttribute('secondary-text-2') || '',
            };
        }
        return { found: false };
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


async def report_now_playing(page, label):
    np = await get_now_playing(page)
    if not np:
        print(f"  [{label}] get_now_playing: None")
        return None
    print(f"  [{label}] title:  {np.get('title', '')!r}")
    print(f"  [{label}] artist: {np.get('artist', '')!r}")
    print(f"  [{label}] source: {np.get('source', '')!r}")
    return np


async def run():
    async with BrowserManager(headless=False, slow_mo=100) as bm:
        await ensure_logged_in(bm.page)
        await bm.page.goto(PLAYLIST_URL, wait_until="domcontentloaded", timeout=30000)
        await bm.page.wait_for_selector('music-image-row[primary-text]', timeout=30000)
        await bm.page.wait_for_timeout(2500)

        print("Pre-click now-playing state:")
        await report_now_playing(bm.page, "before")

        # Pause any currently-playing track so the a11y test is unambiguous.
        pause_result = await bm.page.evaluate(PAUSE_ONLY_JS)
        print(f"\nPause attempt: {pause_result}")
        await bm.page.wait_for_timeout(1500)

        print(f"\nScrolling row {TARGET_IDX_1BASED} into view...")
        if not await scroll_row_into_view(bm.page, TARGET_IDX_1BASED):
            print("Could not scroll target row into view.")
            return
        await bm.page.wait_for_timeout(800)

        result = await bm.page.evaluate(CLICK_A11Y_JS, TARGET_IDX_1BASED)
        if not result.get("found"):
            print("Could not find div[role=button].a11y on row 11.")
            return
        print(f"\nFound a11y wrapper on row 11:")
        print(f"  rowPrimary:    {result['rowPrimary'][:90]}...")
        print(f"  rowSecondary1: {result['rowSecondary1']}")
        print(f"  rowSecondary2: {result['rowSecondary2']}")
        print(f"  aria-label:    {result['aria'][:120]}...")
        print(f"  rect:          {result['rect']}")
        print("  -> .click() dispatched")

        # Poll now-playing a few times since Amazon takes ~2s to update the
        # player widget.
        for delay_s in (2, 4, 7):
            await bm.page.wait_for_timeout(delay_s * 1000 - (delay_s - 1) * 1000)
            print(f"\n  After ~{delay_s}s:")
            np = await report_now_playing(bm.page, f"+{delay_s}s")
            if np and np.get("title"):
                title = (np.get("title", "") or "").lower()
                artist = (np.get("artist", "") or "").lower()
                if EXPECTED_TITLE_FRAGMENT.lower() in title and EXPECTED_ARTIST_FRAGMENT.lower() in artist:
                    print(f"\n  MATCH: row 11 (Bernstein) IS playing. a11y click works!")
                    break
                if WRONG_TITLE_FRAGMENT.lower() in artist or "karajan" in title:
                    print(f"\n  MISS: Still playing Karajan/row-1. a11y click DID NOT play the clicked row.")
                    break

        print("\nLeaving browser open 10s for manual verification...")
        await bm.page.wait_for_timeout(10000)


if __name__ == "__main__":
    asyncio.run(run())
