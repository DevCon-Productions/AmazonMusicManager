import asyncio
import json
import re
from collections import Counter

from playwright.async_api import Page

from config import BASE_URL, LONG_TIMEOUT, SCROLL_PAUSE, SELECTORS
from models import Song, Playlist
from utils import logger


# /albums/<albumAsin>?trackAsin=<trackAsin> — captures the per-row deep link
# that Amazon's track UI uses; both ASINs are needed for ASIN-direct add.
_ASIN_HREF_RE = re.compile(r"/albums/([A-Z0-9]+)\?trackAsin=([A-Z0-9]+)")


def _parse_asins_from_href(href: str) -> tuple[str, str]:
    """Return (track_asin, album_asin) parsed from a row primary-href, or ("","")."""
    if not href:
        return "", ""
    m = _ASIN_HREF_RE.search(href)
    if not m:
        return "", ""
    return m.group(2), m.group(1)


async def list_playlists(page: Page, debug: bool = False) -> list[dict]:
    """Return a list of dicts with 'name' and 'url' for each user playlist.

    Scrolls down to load all playlists (Amazon Music lazy-loads them).
    """
    logger.info("Listing user playlists")
    await page.goto(f"{BASE_URL}/my/playlists/all", wait_until="domcontentloaded", timeout=LONG_TIMEOUT)
    # Wait for playlist items to render (they load asynchronously)
    try:
        await page.wait_for_selector('music-vertical-item[primary-href], music-vertical-item[href]', state="attached", timeout=LONG_TIMEOUT)
        logger.info("Playlist items detected in DOM")
    except Exception:
        logger.warning("Timed out waiting for music-vertical-item elements")
    await page.wait_for_timeout(2000)  # Extra settle time

    if debug:
        print("\n  DEBUG: Browser paused. Use the inspector to find playlist selectors.")
        print("  Close the inspector or press Resume to continue.\n")
        await page.pause()

    # Collect playlists while scrolling — the grid may be virtualized
    # (items leave the DOM as they scroll out of view), so we collect
    # at each scroll position and merge results.
    playlists = []
    seen_urls = set()

    async def _collect_visible():
        """Scrape all playlist items currently in the DOM."""
        results = await page.evaluate("""() => {
            const playlists = [];
            const seen = new Set();
            const els = document.querySelectorAll(
                '[href*="playlist"], [primary-href*="playlist"]'
            );
            for (const el of els) {
                const href = el.getAttribute('href') || el.getAttribute('primary-href') || '';
                if (!href) continue;
                if (href === '/my/playlists/all' || href === '/my/playlists'
                    || href === '/my/playlists/') continue;
                const idx = href.indexOf('/playlists/');
                if (idx === -1) continue;
                const after = href.substring(idx + 11);
                if (!after || after.length < 2) continue;
                if (seen.has(href)) continue;
                seen.add(href);
                const text = (el.getAttribute('primary-text') || el.textContent || '').trim();
                if (text) {
                    playlists.push({ name: text, url: href });
                }
            }
            return playlists;
        }""")
        added = 0
        for item in results:
            url = item["url"]
            if not url.startswith("http"):
                url = f"{BASE_URL}{url}"
            if url not in seen_urls:
                seen_urls.add(url)
                playlists.append({"name": item["name"], "url": url})
                added += 1
        return added

    # First collection before scrolling
    await _collect_visible()
    logger.info("Initial collection: %d playlists", len(playlists))

    # Scroll down and collect at each position
    stale_rounds = 0
    for scroll_round in range(50):
        # Scroll down
        await page.keyboard.press("End")
        await page.mouse.wheel(0, 3000)
        await page.wait_for_timeout(1500)

        added = await _collect_visible()
        if added > 0:
            stale_rounds = 0
            logger.info("Scroll round %d: +%d new, %d total playlists",
                        scroll_round + 1, added, len(playlists))
        else:
            stale_rounds += 1
            if stale_rounds >= 3:
                break

    # If the fast scroll missed some (e.g. virtualized grid unloaded top items),
    # scroll back to top and do one slow pass
    count_after_fast = len(playlists)
    await page.keyboard.press("Home")
    await page.wait_for_timeout(1500)
    added = await _collect_visible()
    if added > 0:
        logger.info("Top re-scan: +%d playlists recovered, %d total", added, len(playlists))
        # Slow scroll to pick up anything in between
        stale_rounds = 0
        for scroll_round in range(50):
            await page.mouse.wheel(0, 800)
            await page.wait_for_timeout(800)
            added = await _collect_visible()
            if added > 0:
                stale_rounds = 0
                logger.info("Slow scroll round %d: +%d new, %d total",
                            scroll_round + 1, added, len(playlists))
            else:
                stale_rounds += 1
                if stale_rounds >= 5:
                    break

    logger.info("Final playlist count: %d", len(playlists))

    if not playlists:
        logger.info("No playlists found via standard selectors. Current URL: %s", page.url)
        # Dump page structure for debugging
        debug_info = await page.evaluate("""() => {
            const tags = new Set();
            document.querySelectorAll('*').forEach(el => tags.add(el.tagName.toLowerCase()));
            const musicTags = [...tags].filter(t => t.startsWith('music-'));

            // Look for any clickable elements that might be playlist cards
            const candidates = [];

            // Check music-horizontal-item, music-list-item, music-shoveler items
            for (const tag of ['music-horizontal-item', 'music-list-item', 'music-image-row', 'music-container']) {
                const els = document.querySelectorAll(tag);
                if (els.length > 0) {
                    const sample = els[0];
                    const attrs = {};
                    for (const a of sample.attributes) attrs[a.name] = a.value.substring(0, 100);
                    candidates.push({ tag, count: els.length, sampleAttrs: attrs });
                }
            }

            // Check for any href containing 'playlist'
            const playlistLinks = [];
            document.querySelectorAll('[href*="playlist"], [primary-href*="playlist"]').forEach(el => {
                playlistLinks.push({
                    tag: el.tagName.toLowerCase(),
                    href: el.getAttribute('href') || el.getAttribute('primary-href') || '',
                    text: (el.getAttribute('primary-text') || el.textContent || '').trim().substring(0, 80),
                });
            });

            return {
                musicTags,
                url: window.location.href,
                candidates,
                playlistLinks: playlistLinks.slice(0, 20),
                bodyText: document.body?.innerText?.substring(0, 500),
            };
        }""")
        logger.info("Music web components found: %s", debug_info.get("musicTags"))
        logger.info("Candidate elements: %s", debug_info.get("candidates"))
        logger.info("Playlist links found: %s", debug_info.get("playlistLinks"))
        logger.info("Page body preview: %s", debug_info.get("bodyText", "")[:500])

    logger.info("Found %d playlists", len(playlists))

    # Save to cache for future use without browser
    if playlists:
        from playlist_cache import save_cached_playlists
        save_cached_playlists(playlists)

    return playlists


async def scrape_playlist(page: Page, playlist_url: str | None = None, playlist_name: str | None = None) -> Playlist:
    """Scrape all tracks from a playlist.

    Provide either *playlist_url* (direct link) or *playlist_name* (will
    navigate to the user's library and find it).
    """
    if playlist_url:
        logger.info("Opening playlist URL: %s", playlist_url)
        await page.goto(playlist_url, wait_until="domcontentloaded", timeout=LONG_TIMEOUT)
    elif playlist_name:
        await _navigate_to_playlist_by_name(page, playlist_name)
    else:
        raise ValueError("Provide either playlist_url or playlist_name")

    # Wait for the playlist page to fully load. Since the Nov 2026 redesign
    # (React Native Web) track rows are `div[class*=selectionRow]`; the
    # playlist data itself lives in React props (see _read_playlist_data).
    logger.info("Waiting for playlist page to fully load...")
    try:
        await page.wait_for_selector(_ROW_SELECTOR, state="attached", timeout=LONG_TIMEOUT)
        logger.info("Track rows detected")
    except Exception:
        logger.warning("Timed out waiting for track rows (url=%s)", page.url)
    await page.wait_for_timeout(1500)

    data = await _read_playlist_data(page)
    name = (data or {}).get("title") or playlist_name or "Unnamed Playlist"

    songs = await _collect_all_tracks(page)
    logger.info("Scraped %d tracks from '%s'", len(songs), name)

    return Playlist(name=name, songs=songs)


async def _navigate_to_playlist_by_name(page: Page, name: str):
    """Find and open a playlist by name from the user's library.

    Checks the cached playlist list first to avoid opening the listing page.
    Falls back to browsing if the cache doesn't have it.
    """
    # Check cache first
    from playlist_cache import get_playlist_url
    cached_url = get_playlist_url(name)
    if cached_url:
        logger.info("Found playlist '%s' in cache: %s", name, cached_url)
        await page.goto(cached_url, wait_until="domcontentloaded", timeout=LONG_TIMEOUT)
        return

    logger.info("Playlist '%s' not in cache, searching library", name)
    await page.goto(f"{BASE_URL}/my/playlists/all", wait_until="domcontentloaded", timeout=LONG_TIMEOUT)
    # Wait for playlist items to load
    try:
        await page.wait_for_selector('music-vertical-item[primary-href], music-vertical-item[href]', state="attached", timeout=LONG_TIMEOUT)
    except Exception:
        logger.warning("Timed out waiting for playlist items to load")
    await page.wait_for_timeout(2000)

    # Find the matching playlist URL, then navigate directly
    found_url = await page.evaluate("""(targetName) => {
        const lower = targetName.toLowerCase();

        const els = document.querySelectorAll('[href*="playlist"], [primary-href*="playlist"]');
        for (let i = 0; i < els.length; i++) {
            const el = els[i];
            const text = (el.getAttribute('primary-text') || el.textContent || '').trim();
            if (text.toLowerCase() === lower) {
                return el.getAttribute('href') || el.getAttribute('primary-href') || '';
            }
        }
        return '';
    }""", name)

    if not found_url:
        raise ValueError(f"Playlist '{name}' not found in your library")

    # Navigate directly to the playlist URL
    full_url = found_url if found_url.startswith("http") else f"{BASE_URL}{found_url}"
    logger.info("Found playlist '%s' at %s", name, full_url)
    await page.goto(full_url, wait_until="domcontentloaded", timeout=LONG_TIMEOUT)


# ---------------------------------------------------------------------------
# Playlist track collection (redesigned site, Nov 2026)
#
# The web player is now React Native Web: no music-* components, no ASINs in
# the DOM. But every track row's React fiber carries the page's GraphQL
# `playlistRef` object, whose `tracks.edges[i].node` has the track ASIN,
# full title, duration (seconds), album {id, shortTitle} and
# contributingArtists. Edges accumulate as the list scrolls (100 per page,
# `pageInfo.hasNextPage`), so we scroll until edges == totalCount.
# ---------------------------------------------------------------------------
_ROW_SELECTOR = '[class*="selectionRow"]'

_READ_PLAYLIST_JS = r"""() => {
    const rows = document.querySelectorAll('[class*="selectionRow"]');
    let pr = null;
    for (const r of rows) {
        const fk = Object.keys(r).find(k => k.startsWith('__reactFiber'));
        let f = fk ? r[fk] : null;
        for (let i = 0; i < 30 && f; i++, f = f.return) {
            const p = f.memoizedProps;
            if (p && p.playlistRef && p.playlistRef.tracks) { pr = p.playlistRef; break; }
        }
        if (pr) break;
    }
    if (!pr) return null;
    const t = pr.tracks;
    const tracks = (t.edges || []).map(e => {
        const n = (e && e.node) || {};
        const artists = ((n.contributingArtists || {}).edges || [])
            .map(a => a && a.node && a.node.name).filter(Boolean);
        return {
            asin: n.id || '',
            title: n.title || n.shortTitle || '',
            artists,
            album: (n.album && (n.album.title || n.album.shortTitle)) || '',
            album_asin: (n.album && n.album.id) || '',
            duration: typeof n.duration === 'number' ? n.duration : null,
        };
    });
    return {
        title: pr.title || '',
        total: (t.totalCount ?? pr.trackCount ?? null),
        hasNext: !!(t.pageInfo && t.pageInfo.hasNextPage),
        tracks,
    };
}"""

_SCROLL_TO_END_JS = r"""() => {
    const rows = document.querySelectorAll('[class*="selectionRow"]');
    let s = rows.length ? rows[rows.length - 1] : null;
    while (s && s !== document.body) {
        const cs = getComputedStyle(s);
        if (/(auto|scroll)/.test(cs.overflowY) && s.scrollHeight > s.clientHeight + 10) break;
        s = s.parentElement;
    }
    if (!s || s === document.body) { window.scrollTo(0, document.body.scrollHeight); return; }
    s.scrollTop = s.scrollHeight;
}"""


def _join_artists(names: list[str]) -> str:
    """Match the site's display format: 'A', 'A & B', 'A, B & C'."""
    if len(names) <= 1:
        return names[0] if names else ""
    return ", ".join(names[:-1]) + " & " + names[-1]


def _format_duration(seconds) -> str:
    if not isinstance(seconds, (int, float)) or seconds <= 0:
        return ""
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


_EXPLICIT_SUFFIX_RE = re.compile(r"\s*\[Explicit\]\s*$", re.IGNORECASE)


def _track_to_song(t: dict) -> Song:
    return Song(
        # Old exports showed titles without the "[Explicit]" tag (it was a
        # badge); strip it so refreshed rows look like the existing ones.
        title=_EXPLICIT_SUFFIX_RE.sub("", t.get("title") or ""),
        artist=_join_artists(t.get("artists") or []),
        album=t.get("album") or "",
        duration=_format_duration(t.get("duration")),
        asin=t.get("asin") or "",
        album_asin=t.get("album_asin") or "",
    )


async def _read_playlist_data(page: Page) -> dict | None:
    try:
        return await page.evaluate(_READ_PLAYLIST_JS)
    except Exception as e:
        logger.warning("Could not read playlist data from page: %s", e)
        return None


async def _collect_all_tracks(page: Page) -> list[Song]:
    """Collect every track of the open playlist, in playlist order."""
    data = await _read_playlist_data(page)
    if data is None:
        logger.warning("No playlist data found on page (url=%s)", page.url)
        return []

    stale = 0
    STALE_LIMIT = 4
    last_n = len(data["tracks"])
    for batch in range(200):  # 200 pages × 100 = far above any real playlist
        total = data.get("total")
        n = len(data["tracks"])
        logger.info("Batch %d: %d of %s tracks loaded", batch + 1, n, total if total is not None else "?")
        if (total is not None and n >= total) or (not data.get("hasNext") and n > 0 and stale >= 1):
            break
        await page.evaluate(_SCROLL_TO_END_JS)
        await page.wait_for_timeout(1500 + stale * 2000)
        data = await _read_playlist_data(page) or data
        if len(data["tracks"]) > last_n:
            last_n = len(data["tracks"])
            stale = 0
        else:
            stale += 1
            if stale >= STALE_LIMIT:
                logger.warning("Track loading stalled at %d of %s", last_n, total)
                break

    songs = [_track_to_song(t) for t in data["tracks"] if t.get("title")]
    total = data.get("total")
    if total is not None and len(songs) < total:
        logger.warning("Collected %d of %d tracks (some may be unavailable)", len(songs), total)
    logger.info("Collection complete: %d total songs", len(songs))
    return songs


async def play_playlist(
    page: Page,
    playlist_url: str | None = None,
    playlist_name: str | None = None,
    shuffle: bool = False,
) -> str:
    """Open a playlist and start playback.

    If *shuffle* is True, tries the dedicated "Shuffle play" button first,
    falling back to toggling the shuffle control and then pressing Play.
    Returns a short string describing which action fired (for logging).
    """
    if playlist_url:
        logger.info("Opening playlist URL: %s", playlist_url)
        await page.goto(playlist_url, wait_until="domcontentloaded", timeout=LONG_TIMEOUT)
    elif playlist_name:
        await _navigate_to_playlist_by_name(page, playlist_name)
    else:
        raise ValueError("Provide either playlist_url or playlist_name")

    try:
        await page.wait_for_selector(
            SELECTORS["playlist_header"], state="attached", timeout=LONG_TIMEOUT
        )
    except Exception:
        logger.warning("Timed out waiting for playlist header before play")
    # Tracks and action buttons render a beat after the header — give them time.
    try:
        await page.wait_for_selector(
            'music-image-row[primary-text]', state="attached", timeout=LONG_TIMEOUT
        )
    except Exception:
        logger.warning("Timed out waiting for track rows before play")
    await page.wait_for_timeout(2500)

    # JS helper that walks light DOM + shadow roots to find a clickable element
    # matching any of the given aria / icon / text predicates. Amazon Music's
    # play/shuffle controls live inside music-detail-header's shadow tree.
    _FIND_AND_CLICK = r"""
        (args) => {
            const { ariaSubstrings, iconNames, textExact, scopeTagNames } = args;
            const matches = (el) => {
                const aria = (el.getAttribute && el.getAttribute('aria-label') || '').toLowerCase();
                const icon = (el.getAttribute && el.getAttribute('icon-name') || '').toLowerCase();
                const text = (el.textContent || '').toLowerCase().trim();
                for (const s of ariaSubstrings) if (s && aria.includes(s)) return true;
                for (const s of iconNames) if (s && icon === s) return true;
                for (const s of textExact) if (s && text === s) return true;
                return false;
            };
            const clickable = (el) => {
                const tag = (el.tagName || '').toLowerCase();
                if (tag === 'music-button' || tag === 'button') return true;
                const role = (el.getAttribute && el.getAttribute('role') || '').toLowerCase();
                return role === 'button';
            };
            // When scopeTagNames is provided, search ONLY inside those scoped
            // elements. Otherwise the persistent player bar's Play button (or
            // any matching control elsewhere on the page) can win the race
            // and we'd resume a queued track instead of starting the playlist.
            const scoped = scopeTagNames && scopeTagNames.length
                ? Array.from(document.querySelectorAll(scopeTagNames.join(',')))
                : [];
            const roots = scoped.length ? scoped : [document];
            const visited = new Set();
            const walk = (root) => {
                if (!root || visited.has(root)) return null;
                visited.add(root);
                const all = root.querySelectorAll ? root.querySelectorAll('*') : [];
                for (const el of all) {
                    if (clickable(el) && matches(el)) {
                        try { el.click(); } catch (e) { continue; }
                        return (el.getAttribute('aria-label') || el.getAttribute('icon-name')
                            || el.textContent || 'match').toString().slice(0, 60);
                    }
                    if (el.shadowRoot) {
                        const hit = walk(el.shadowRoot);
                        if (hit) return hit;
                    }
                }
                return null;
            };
            for (const r of roots) {
                const hit = walk(r);
                if (hit) return hit;
            }
            return null;
        }
    """

    if shuffle:
        fired = await page.evaluate(_FIND_AND_CLICK, {
            "ariaSubstrings": ["shuffle play", "shuffle and play", "play shuffled"],
            "iconNames": [],
            "textExact": ["shuffle play"],
            "scopeTagNames": ["music-detail-header"],
        })
        if fired:
            logger.info("Started shuffle playback via %r", fired)
            return fired

        toggled = await page.evaluate(_FIND_AND_CLICK, {
            "ariaSubstrings": ["turn shuffle on", "enable shuffle", "shuffle"],
            "iconNames": ["shuffle"],
            "textExact": ["shuffle"],
            "scopeTagNames": ["music-detail-header"],
        })
        if toggled:
            logger.info("Toggled shuffle on via %r", toggled)
            await page.wait_for_timeout(700)
        else:
            logger.warning("Could not find shuffle toggle; starting in straight order")

    fired = await page.evaluate(_FIND_AND_CLICK, {
        "ariaSubstrings": ["play ", "play all", "resume"],
        "iconNames": ["play"],
        "textExact": ["play"],
        "scopeTagNames": ["music-detail-header"],
    })
    if not fired:
        raise RuntimeError("Could not find a Play button on the playlist page")
    logger.info("Started playback via %r", fired)
    return fired


# Probes which span.index values are currently in the viewport.  Indices are
# only reliable for rows whose rect intersects the viewport — Amazon recycles
# off-screen DOM nodes and their span.index text lags until re-rendered.
_PROBE_VISIBLE_INDEX_JS = r"""
    (targetIdx) => {
        const rows = document.querySelectorAll('music-image-row[primary-text]');
        const vh = window.innerHeight || document.documentElement.clientHeight;
        const vw = window.innerWidth || document.documentElement.clientWidth;
        let visMin = null, visMax = null, found = false;
        let domMax = null, targetInDom = false;
        for (const row of rows) {
            const idxEl = row.querySelector('span.index')
                || (row.shadowRoot && row.shadowRoot.querySelector('span.index'));
            if (!idxEl) continue;
            const n = parseInt((idxEl.textContent || '').trim(), 10);
            if (!Number.isFinite(n)) continue;
            if (domMax === null || n > domMax) domMax = n;
            if (n === targetIdx) targetInDom = true;
            const rect = row.getBoundingClientRect();
            if (rect.height < 1 || rect.width < 1) continue;
            if (rect.bottom <= 0 || rect.top >= vh) continue;
            if (rect.right <= 0 || rect.left >= vw) continue;
            if (visMin === null || n < visMin) visMin = n;
            if (visMax === null || n > visMax) visMax = n;
            if (n === targetIdx) found = true;
        }
        return { visMin, visMax, found, domMax, targetInDom };
    }
"""


# Scrolls the playlist toward the target row. If the target is already in
# DOM (loaded but possibly below the viewport — virtualized lists keep some
# rows offscreen), scroll it directly into view. Otherwise scroll the last
# DOM row to the bottom of the viewport to trigger the virtualizer to load
# the next batch. We use JS scrollIntoView rather than page.mouse.wheel
# because wheel events only scroll the element under the cursor — if the
# cursor happens to sit over the persistent player bar or off-screen, the
# wheel no-ops and the probe sees a stuck v_max. See
# feedback_amazon_music_dom.md section 2 for the virtualizer behavior.
_SCROLL_TOWARDS_TARGET_JS = r"""
    (targetIdx) => {
        const rows = document.querySelectorAll('music-image-row[primary-text]');
        if (!rows.length) return { scrolled: false, reason: 'no-rows' };
        // Prefer to scroll the target row directly if it's in DOM.
        for (const row of rows) {
            const idxEl = row.querySelector('span.index')
                || (row.shadowRoot && row.shadowRoot.querySelector('span.index'));
            if (!idxEl) continue;
            const n = parseInt((idxEl.textContent || '').trim(), 10);
            if (n === targetIdx) {
                row.scrollIntoView({block: 'center', behavior: 'instant'});
                return { scrolled: true, reason: 'target' };
            }
        }
        // Target not in DOM yet. Scroll the last DOM row into view to
        // trigger the virtualizer to load more rows below it.
        rows[rows.length - 1].scrollIntoView({block: 'end', behavior: 'instant'});
        return { scrolled: true, reason: 'last-row' };
    }
"""


# Clicks the in-viewport row with the matching span.index. Restricted to
# viewport so we don't fire on a recycled-node row whose span.index is stale.
# Plain `row.click()` does NOT trigger Amazon to start playback — the row body
# only highlights. Each row contains a `music-playback-button` (the icon that
# replaces the index number on hover) which is the canonical "play this row"
# control; clicking that does start playback.
_CLICK_ROW_BY_INDEX_JS = r"""
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

            const result = {
                title: (row.getAttribute('primary-text') || '').trim(),
                artist: (row.getAttribute('secondary-text-1') || '').trim(),
                strategy: null,
            };

            const pbBtn = row.querySelector('music-playback-button')
                || (row.shadowRoot && row.shadowRoot.querySelector('music-playback-button'));
            if (pbBtn) {
                try {
                    pbBtn.click();
                    result.strategy = 'music-playback-button';
                    return result;
                } catch (e) {}
            }

            const playBtn = row.querySelector('music-button[icon-name="play"]')
                || (row.shadowRoot && row.shadowRoot.querySelector('music-button[icon-name="play"]'));
            if (playBtn) {
                try {
                    playBtn.click();
                    result.strategy = 'music-button-play';
                    return result;
                } catch (e) {}
            }

            try {
                row.dispatchEvent(new MouseEvent('dblclick', {
                    bubbles: true, cancelable: true,
                }));
                result.strategy = 'dblclick-row';
                return result;
            } catch (e) {}

            return null;
        }
        return null;
    }
"""


async def scroll_and_click_row_at_index(
    page: Page,
    track_index: int,
) -> bool:
    """Scroll the already-loaded playlist until the row at *track_index*
    (0-based) is in the viewport, then click its `music-playback-button` to
    start playback. Caller is responsible for navigating + waiting for tracks
    to render before calling this.

    Used by Classical Shuffle to avoid re-navigating after `_collect_all_tracks`
    has already produced the live track order — `play_track_at_index` is the
    single-shot wrapper that does the navigation itself.
    """
    target_idx_1based = track_index + 1
    MAX_SCROLLS = 80
    # Stall detection: if the DOM-max idx doesn't grow across N attempts AND
    # the target isn't yet loaded, the playlist is genuinely shorter than
    # we expected (stale xlsx).
    last_dom_max = -1
    stuck_count = 0
    for _attempt in range(MAX_SCROLLS):
        info = await page.evaluate(_PROBE_VISIBLE_INDEX_JS, target_idx_1based)
        if info["found"]:
            break
        dom_max = info.get("domMax")
        target_in_dom = info.get("targetInDom", False)
        if dom_max is None:
            # No rows in DOM yet — let the playlist render and retry.
            await page.wait_for_timeout(500)
            continue
        # Stall check only matters when the target isn't loaded yet — once
        # the target is in DOM we can scroll it directly and dom_max can
        # stop growing without that being a problem.
        if not target_in_dom and target_idx_1based > dom_max:
            if dom_max == last_dom_max:
                stuck_count += 1
                if stuck_count >= 3:
                    logger.warning(
                        "scroll_and_click_row_at_index: scroll stalled at "
                        "row %d in DOM; playlist may be shorter than target "
                        "%d (stale xlsx?)",
                        dom_max, target_idx_1based,
                    )
                    return False
            else:
                stuck_count = 0
                last_dom_max = dom_max
        await page.evaluate(_SCROLL_TOWARDS_TARGET_JS, target_idx_1based)
        await page.wait_for_timeout(1000)
    else:
        logger.warning(
            "scroll_and_click_row_at_index: row %d never entered viewport "
            "after %d scrolls", target_idx_1based, MAX_SCROLLS,
        )
        return False

    # Extra settle so span.index has time to render after the final scroll
    # before we read it again to click. 2026-05-20: bumped 800 → 2500 to
    # probe whether the Tennstedt-plays-Furtwangler bug is pure DOM-binding
    # lag (the music-playback-button's internal track binding may take longer
    # to update than the row's primary-text/secondary-text-1 attributes on
    # virtualizer recycle).
    await page.wait_for_timeout(2500)
    clicked = await page.evaluate(_CLICK_ROW_BY_INDEX_JS, target_idx_1based)
    if not clicked:
        logger.warning(
            "scroll_and_click_row_at_index: row %d in viewport but click failed",
            target_idx_1based,
        )
        return False

    logger.info(
        "scroll_and_click_row_at_index: clicked row %d ('%s' by '%s') via %s",
        target_idx_1based, clicked.get("title", ""), clicked.get("artist", ""),
        clicked.get("strategy", "?"),
    )
    return True


# ----------------------------------------------------------------------
# Web-restriction detection — see feedback_amazon_music_dom.md Trap 4.
# Some ASINs are licensed to play on Amazon's desktop apps but NOT on the
# web player. When the web client clicks such a row, Amazon's backend
# silently substitutes a different ASIN (typically the playlist's first
# track). The substitution is visible in the response of the
# /api/playLibraryPlaylist POST that the click fires: the request's `at`
# field carries the ASIN we asked for, the response's `key` field carries
# `<playlist-id>:<ASIN>` reflecting what Amazon actually started.
#
# `scroll_and_click_and_detect_substitution` is the response-inspection
# wrapper. The plain `scroll_and_click_row_at_index` above stays as the
# backwards-compatible path for callers that don't need the signal.

_PLAY_LIBRARY_PLAYLIST_RE = re.compile(r"/api/playLibraryPlaylist", re.IGNORECASE)
_ASIN_RE = re.compile(r"[A-Z0-9]{10}")
# Substitution-detect window. The smoke ran with 4s capture and observed the
# response within ~200ms; 3s is comfortable headroom for slow networks while
# still being much faster than the 10s post-click `get_now_playing` bailout
# that previously caught these.
_SUBSTITUTION_TIMEOUT_S = 3.0


def _extract_asin_from_request_body(body: str | None) -> str | None:
    """Pull the requested ASIN out of a playLibraryPlaylist POST body.

    The payload looks like `{"at":"catalog\\u001fB09S6Q9S9M","id":"...","index":"11",...}`
    so we parse JSON and grab `at` (after the catalog\\x1f prefix). Defensive —
    Amazon could change the shape; returns None if we can't find an ASIN.
    """
    if not body:
        return None
    try:
        obj = json.loads(body)
    except (TypeError, ValueError):
        return None
    at = obj.get("at")
    if not isinstance(at, str):
        return None
    # at = "catalog\x1fB09S6Q9S9M" — the \x1f is a literal Unit Separator.
    # Some Amazon endpoints use other prefixes (library, album, etc.); we
    # just want the trailing 10-char ASIN regardless of prefix.
    m = _ASIN_RE.search(at)
    return m.group(0) if m else None


def _extract_asin_from_response_body(body: str | None) -> str | None:
    """Pull the substituted ASIN out of the playLibraryPlaylist response.

    Looks for a `key` field of the form `<playlist-id>:<ASIN>`. Multiple keys
    fire in the response (one for the playlist, one for the now-playing
    track); we want the playing-track one, which has an ASIN suffix.
    """
    if not body:
        return None
    # Try fast path: JSON-parse and walk the `methods` array looking for keys.
    try:
        obj = json.loads(body)
    except (TypeError, ValueError):
        # Fall back to a permissive regex scan over the raw text — Amazon's
        # response can be huge JSON and we don't need the structure.
        return _scan_response_text_for_asin(body)
    methods = obj.get("methods")
    if not isinstance(methods, list):
        return _scan_response_text_for_asin(body)
    for m in methods:
        if not isinstance(m, dict):
            continue
        key = m.get("key")
        if not isinstance(key, str) or ":" not in key:
            continue
        # `bb9577f7-...-9760f8f01a99:B00138AOPO` — ASIN is what's after the
        # colon (only when it's a 10-char alphanumeric).
        suffix = key.rsplit(":", 1)[-1]
        if _ASIN_RE.fullmatch(suffix or ""):
            return suffix
    return _scan_response_text_for_asin(body)


def _scan_response_text_for_asin(body: str) -> str | None:
    """Last-resort regex sweep — find an ASIN that follows a colon in a key
    field anywhere in the body. Tolerates schema drift."""
    for m in re.finditer(r'"key"\s*:\s*"[^"]*:([A-Z0-9]{10})"', body):
        return m.group(1)
    return None


async def scroll_and_click_and_detect_substitution(
    page: Page,
    track_index: int,
) -> dict:
    """Click row at *track_index* AND watch /api/playLibraryPlaylist for ASIN
    substitution.

    Returns a dict:
        {
            "ok": bool,                       # row click succeeded at the DOM level
            "substituted": bool | None,       # True if Amazon's response carried a
                                              #   different ASIN than we requested;
                                              #   None if we couldn't capture the API
                                              #   call (network race, timeout, etc.)
            "requested_asin": str | None,
            "substituted_asin": str | None,
        }

    Backwards-compat: a caller that just wants the old True/False should check
    `result["ok"]`. The "substituted" key is the new signal — None means
    "didn't get the data, fall back to get_now_playing verification."
    """
    result: dict = {
        "ok": False,
        "substituted": None,
        "requested_asin": None,
        "substituted_asin": None,
    }

    captured: dict = {"request_body": None, "response_body": None, "done": False}
    capture_event = asyncio.Event()

    def on_response(resp):
        if captured["done"]:
            return
        if not _PLAY_LIBRARY_PLAYLIST_RE.search(resp.url):
            return
        try:
            req = resp.request
            captured["request_body"] = req.post_data
        except Exception:
            captured["request_body"] = None
        # Schedule async body fetch — Playwright's on_response is sync.
        asyncio.get_running_loop().create_task(_grab_body(resp))

    async def _grab_body(resp):
        try:
            body = await resp.text()
        except Exception:
            body = None
        captured["response_body"] = body
        captured["done"] = True
        capture_event.set()

    page.on("response", on_response)
    try:
        ok = await scroll_and_click_row_at_index(page, track_index)
        result["ok"] = ok
        if not ok:
            return result
        # Wait for the response burst — bounded by _SUBSTITUTION_TIMEOUT_S.
        try:
            await asyncio.wait_for(
                capture_event.wait(), timeout=_SUBSTITUTION_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            logger.info(
                "scroll_and_click_and_detect_substitution: no "
                "playLibraryPlaylist response within %.1fs — substitution "
                "signal unavailable, caller should fall back to "
                "get_now_playing verification",
                _SUBSTITUTION_TIMEOUT_S,
            )
            return result
    finally:
        page.remove_listener("response", on_response)

    req_asin = _extract_asin_from_request_body(captured.get("request_body"))
    resp_asin = _extract_asin_from_response_body(captured.get("response_body"))
    result["requested_asin"] = req_asin
    result["substituted_asin"] = resp_asin
    if req_asin and resp_asin:
        result["substituted"] = (req_asin != resp_asin)
        if result["substituted"]:
            logger.info(
                "scroll_and_click_and_detect_substitution: web-restricted ASIN "
                "detected — requested %s, Amazon returned %s",
                req_asin, resp_asin,
            )
        else:
            logger.debug(
                "scroll_and_click_and_detect_substitution: ASIN match — %s",
                req_asin,
            )
    else:
        # Couldn't get both sides; substituted stays None so caller falls
        # back to the slow verification path.
        logger.info(
            "scroll_and_click_and_detect_substitution: could not extract "
            "ASINs (req=%r resp=%r) — substitution signal unavailable",
            req_asin, resp_asin,
        )
    return result


async def play_track_at_index(
    page: Page,
    playlist_url: str,
    track_index: int,
) -> bool:
    """Navigate to *playlist_url* and start playback at the row at *track_index*
    (0-based, matching the order returned by `_collect_all_tracks`).

    Thin wrapper: navigate + wait + delegate to `scroll_and_click_row_at_index`.
    """
    target_idx_1based = track_index + 1
    logger.info(
        "play_track_at_index: opening %s and starting playback at row %d",
        playlist_url, target_idx_1based,
    )
    await page.goto(playlist_url, wait_until="domcontentloaded", timeout=LONG_TIMEOUT)
    try:
        await page.wait_for_selector(
            'music-image-row[primary-text]', state="attached", timeout=LONG_TIMEOUT
        )
    except Exception:
        logger.warning("play_track_at_index: timed out waiting for track rows")
        return False
    await page.wait_for_timeout(2500)
    return await scroll_and_click_row_at_index(page, track_index)


def find_performance_start_row(
    songs: list[Song], artist: str, album: str,
) -> int | None:
    """Return the 0-based index of the first song in *songs* whose (artist,
    album) matches (case-insensitive, trimmed). Returns None if not found.

    Used by classical-shuffle to pick the first track of a performance after a
    fresh `_collect_all_tracks` of the playlist (live order may differ from the
    cached xlsx if the playlist was reordered since export).
    """
    a_low = (artist or "").strip().lower()
    l_low = (album or "").strip().lower()
    for i, s in enumerate(songs):
        if (s.artist or "").strip().lower() == a_low \
                and (s.album or "").strip().lower() == l_low:
            return i
    return None


async def get_now_playing(page: Page) -> dict | None:
    """Read the persistent player bar and return the currently playing track.

    Returns a dict with keys 'title', 'artist', 'source', and (when no track
    can be parsed) 'diag'. Walks shadow DOMs because Amazon Music's player
    controls live inside custom-element shadow trees.
    """
    data = await page.evaluate(r"""
        () => {
            const TITLE_ATTRS = ['primary-text', 'primary-text-1',
                'song-title', 'title-text'];
            const ARTIST_ATTRS = ['secondary-text-1', 'secondary-text',
                'artist-text'];
            const ALBUM_ATTRS = ['secondary-text-2', 'tertiary-text',
                'album-text', 'album-name'];
            const PLAYER_HINT = /(player|transport|now[-_ ]?playing|playback|footer)/i;

            const seen = new Set();
            const deepWalkAll = (root, fn) => {
                if (!root || seen.has(root)) return;
                seen.add(root);
                const all = root.querySelectorAll ? root.querySelectorAll('*') : [];
                for (const el of all) {
                    fn(el);
                    if (el.shadowRoot) deepWalkAll(el.shadowRoot, fn);
                }
            };

            // Climb light + shadow ancestors looking for a player-ish host.
            const findPlayerAncestor = (el) => {
                let cur = el;
                let hops = 0;
                while (cur && hops++ < 40) {
                    const tag = (cur.tagName || '').toLowerCase();
                    const id = cur.id || '';
                    const cls = (cur.getAttribute && cur.getAttribute('class')) || '';
                    const aria = (cur.getAttribute && cur.getAttribute('aria-label')) || '';
                    if (PLAYER_HINT.test(tag) || PLAYER_HINT.test(id)
                        || PLAYER_HINT.test(cls) || PLAYER_HINT.test(aria)) {
                        return { tag, id, cls: cls.slice(0, 60), aria };
                    }
                    cur = cur.parentNode;
                    if (cur && cur.host) cur = cur.host;  // cross shadow boundary
                }
                return null;
            };

            // Pass 1: collect every element that carries a title-ish attribute,
            // anywhere in the page (including shadow trees).
            const candidates = [];
            deepWalkAll(document, (el) => {
                if (!el.getAttribute) return;
                let title = '';
                let titleAttr = '';
                for (const a of TITLE_ATTRS) {
                    const v = (el.getAttribute(a) || '').trim();
                    if (v) { title = v; titleAttr = a; break; }
                }
                if (!title) return;
                let artist = '';
                for (const a of ARTIST_ATTRS) {
                    const v = (el.getAttribute(a) || '').trim();
                    if (v) { artist = v; break; }
                }
                let album = '';
                for (const a of ALBUM_ATTRS) {
                    const v = (el.getAttribute(a) || '').trim();
                    if (v) { album = v; break; }
                }
                const tag = (el.tagName || '').toLowerCase();
                const ancestor = findPlayerAncestor(el);
                candidates.push({ tag, title, artist, album, titleAttr, ancestor });
            });

            // Pass 2: find the play-position slider. Prefer something with
            // role="slider" + aria-valuenow/aria-valuemax under a player-ish
            // ancestor. Fall back to a clock-text pair like "1:23 / 4:56".
            let position = null;
            let duration = null;
            let progressSource = null;
            seen.clear();
            const sliders = [];
            deepWalkAll(document, (el) => {
                if (!el.getAttribute) return;
                const role = (el.getAttribute('role') || '').toLowerCase();
                const tag = (el.tagName || '').toLowerCase();
                const type = (el.getAttribute('type') || '').toLowerCase();
                const isSlider = role === 'slider'
                    || (tag === 'input' && type === 'range');
                if (!isSlider) return;
                let n = NaN, m = NaN;
                const now = el.getAttribute('aria-valuenow');
                const max = el.getAttribute('aria-valuemax');
                if (now != null && max != null) {
                    n = parseFloat(now);
                    m = parseFloat(max);
                }
                if (!isFinite(n) || !isFinite(m)) {
                    // <input type="range"> exposes value/max as properties.
                    if ('value' in el && 'max' in el) {
                        n = parseFloat(el.value);
                        m = parseFloat(el.max);
                    }
                }
                if (isFinite(n) && isFinite(m) && m > 0) {
                    sliders.push({ el, pos: n, dur: m,
                        ancestor: findPlayerAncestor(el) });
                }
            });
            const sliderPick = sliders.find(s => s.ancestor) || sliders[0];
            if (sliderPick) {
                position = sliderPick.pos;
                duration = sliderPick.dur;
                progressSource = 'slider['
                    + (sliderPick.ancestor ? sliderPick.ancestor.tag : 'unscoped')
                    + ']';
            } else {
                // Fallback: collect "M:SS" / "MM:SS" / "H:MM:SS" text nodes,
                // then take the first pair that share a player-ish ancestor.
                const TIME_RE = /^([0-9]{1,2}:)?[0-9]{1,2}:[0-9]{2}$/;
                const parseTime = (s) => {
                    const parts = s.split(':').map(p => parseInt(p, 10));
                    if (parts.some(isNaN)) return null;
                    let secs = 0;
                    for (const p of parts) secs = secs * 60 + p;
                    return secs;
                };
                const times = [];
                seen.clear();
                deepWalkAll(document, (el) => {
                    if (!el.children || el.children.length > 0) return;
                    const txt = (el.textContent || '').trim();
                    if (!TIME_RE.test(txt)) return;
                    const secs = parseTime(txt);
                    if (secs == null) return;
                    times.push({ el, secs, ancestor: findPlayerAncestor(el) });
                });
                const scoped = times.filter(t => t.ancestor);
                const pair = scoped.length >= 2 ? scoped : times;
                if (pair.length >= 2) {
                    // Largest = duration, the other (typically smaller) =
                    // position. If equal, treat as 0/duration.
                    const sorted = pair.slice().sort((a, b) => a.secs - b.secs);
                    duration = sorted[sorted.length - 1].secs;
                    position = sorted[0].secs === duration ? 0 : sorted[0].secs;
                    progressSource = 'time-pair';
                }
            }

            // Prefer candidates whose ancestor chain includes a player-ish
            // element. If none qualify, fall back to any candidate (better
            // than nothing) but flag the source so we know.
            const ranked = candidates.filter(c => c.ancestor);
            const pick = ranked[0] || candidates[0];
            if (pick) {
                return {
                    title: pick.title,
                    artist: pick.artist,
                    album: pick.album || '',
                    source: (pick.ancestor ? pick.ancestor.tag : 'unscoped')
                        + '/' + pick.tag + '@' + pick.titleAttr,
                    position: position,
                    duration: duration,
                    progressSource: progressSource,
                };
            }

            // No title-bearing element anywhere — dump what's around so we can
            // see what selectors to add.
            const diag = [];
            const tagCounts = new Map();
            seen.clear();
            deepWalkAll(document, (el) => {
                const t = (el.tagName || '').toLowerCase();
                if (t.startsWith('music-') || t.startsWith('mw-')) {
                    tagCounts.set(t, (tagCounts.get(t) || 0) + 1);
                }
            });
            const tagList = Array.from(tagCounts.entries())
                .sort((a, b) => b[1] - a[1])
                .slice(0, 30)
                .map(([t, n]) => t + '×' + n);
            diag.push('custom tags: ' + (tagList.join(', ') || '<none>'));

            // Also dump any element whose id/class hints player-ish, so we
            // can target it.
            const hosts = [];
            seen.clear();
            deepWalkAll(document, (el) => {
                const tag = (el.tagName || '').toLowerCase();
                const id = el.id || '';
                const cls = (el.getAttribute && el.getAttribute('class')) || '';
                if (PLAYER_HINT.test(tag) || PLAYER_HINT.test(id)
                    || PLAYER_HINT.test(cls)) {
                    if (hosts.length < 8) {
                        hosts.push(tag + (id ? '#' + id : '')
                            + (cls ? '.' + cls.slice(0, 40).replace(/\s+/g, '.') : ''));
                    }
                }
            });
            diag.push('player-ish hosts: ' + (hosts.join(' | ') || '<none>'));

            return { diag };
        }
    """)
    return data


async def wait_until_off_playlist(
    page: Page,
    track_set: set[tuple[str, str]],
    *,
    poll_seconds: float = 5.0,
    consecutive_off_polls: int = 2,
    no_track_timeout_seconds: float = 180.0,
    max_wait_seconds: float | None = None,
) -> str:
    """Poll the persistent player until the current track is no longer in
    *track_set* for `consecutive_off_polls` polls in a row.

    *track_set* holds `(title_lower, artist_lower)` tuples — we match exactly
    on both first, then fall back to title-only (Amazon sometimes reformats
    artist strings between playlist view and the player bar).

    Returns a short reason string: 'off-playlist', 'no-track-timeout', or
    'max-wait'.
    """
    import time as _time

    def _norm(s: str) -> str:
        return (s or "").lower().strip()

    titles_only = {t for (t, _a) in track_set}
    consecutive_off = 0
    last_seen_title = None
    no_track_since: float | None = None
    started_at = _time.monotonic()

    while True:
        if max_wait_seconds is not None and _time.monotonic() - started_at > max_wait_seconds:
            return "max-wait"

        np = await get_now_playing(page)
        if not np or not np.get("title"):
            if no_track_since is None:
                no_track_since = _time.monotonic()
                logger.info("No now-playing info yet — waiting")
            elif _time.monotonic() - no_track_since > no_track_timeout_seconds:
                logger.warning("No now-playing info for %.0fs — giving up",
                               no_track_timeout_seconds)
                return "no-track-timeout"
        else:
            no_track_since = None
            title = _norm(np["title"])
            artist = _norm(np.get("artist", ""))

            if title != last_seen_title:
                logger.info("Now playing: %r by %r (source=%s)",
                            np["title"], np.get("artist", ""), np.get("source", ""))
                last_seen_title = title

            in_set = (title, artist) in track_set or title in titles_only
            if in_set:
                consecutive_off = 0
            else:
                consecutive_off += 1
                logger.info("Off-playlist (%d/%d): %r by %r",
                            consecutive_off, consecutive_off_polls,
                            np["title"], np.get("artist", ""))
                if consecutive_off >= consecutive_off_polls:
                    return "off-playlist"

        await page.wait_for_timeout(int(poll_seconds * 1000))


async def delete_songs_from_playlist(
    page: Page,
    songs: list[dict],
    playlist_url: str | None = None,
    playlist_name: str | None = None,
) -> set[int]:
    """Delete specific songs from an Amazon Music playlist.

    Each song dict should have 'title', 'artist', and 'row' keys.
    Navigates to the playlist, finds each song, right-clicks, and removes it.
    Returns set of row numbers that were successfully deleted.
    """
    # Navigate to the playlist
    if playlist_url:
        logger.info("Opening playlist URL: %s", playlist_url)
        await page.goto(playlist_url, wait_until="domcontentloaded", timeout=LONG_TIMEOUT)
    elif playlist_name:
        await _navigate_to_playlist_by_name(page, playlist_name)
    else:
        raise ValueError("Must provide either playlist_url or playlist_name")

    # Wait for tracks to load fully
    try:
        await page.wait_for_selector(
            'music-image-row[primary-text]', state="attached", timeout=LONG_TIMEOUT
        )
    except Exception:
        logger.error("Could not find tracks on the playlist page")
        return

    # Give the page extra time to fully render
    await page.wait_for_timeout(5000)

    # Sort by (title, artist, occurrence_index) so that when the same song
    # appears multiple times we always delete earlier occurrences first.
    # This keeps the effective DOM index correct: after deleting occurrence K,
    # the next target's effective index = its occurrence_index - deletions_so_far.
    songs = sorted(
        songs,
        key=lambda s: (
            s["title"].lower(),
            s.get("artist", "").lower(),
            s.get("occurrence_index", 0),
        ),
    )

    deleted = 0
    deleted_rows = set()
    failed = []
    # Track how many of each (title, artist) pair we've already removed so we
    # can compute the effective DOM index for subsequent duplicates.
    deleted_counts: dict[tuple[str, str], int] = {}

    for song in songs:
        title = song["title"]
        artist = song.get("artist", "")
        occurrence_index = song.get("occurrence_index", 0)
        norm_key = (title.lower(), artist.lower())
        # After deleting earlier occurrences they're gone from the DOM, so the
        # target's position shifts down by that many slots.
        effective_index = occurrence_index - deleted_counts.get(norm_key, 0)
        logger.info(
            "Deleting '%s' by '%s' (occurrence %d, effective DOM index %d)",
            title, artist, occurrence_index, effective_index,
        )

        # Scroll through the playlist to find the song (it may not be in the DOM
        # if the list is virtualized or batched).  Use case-insensitive trimmed
        # comparison because Amazon's attribute values occasionally differ in
        # casing or whitespace from what the scraper stored.
        _FIND_ROW_JS = """(args) => {
            const [title, artist, effectiveIndex] = args;
            const tLow = title.trim().toLowerCase();
            const aLow = artist.trim().toLowerCase();
            const rows = document.querySelectorAll('music-image-row[primary-text]');
            let count = 0;
            for (const row of rows) {
                const t = (row.getAttribute('primary-text') || '').trim().toLowerCase();
                const a = (row.getAttribute('secondary-text-1') || '').trim().toLowerCase();
                if (t === tLow && a === aLow) {
                    if (count === effectiveIndex) return row;
                    count++;
                }
            }
            return null;
        }"""
        row_handle = None
        for attempt in range(15):
            row_handle = await page.evaluate_handle(_FIND_ROW_JS, [title, artist, effective_index])
            is_null = await row_handle.evaluate("el => el === null")
            if not is_null:
                break
            await page.keyboard.press("End")
            await page.mouse.wheel(0, 3000)
            await page.wait_for_timeout(2000)

        is_null = await row_handle.evaluate("el => el === null")
        if is_null and effective_index > 0:
            # The spreadsheet recorded this as occurrence N, but Amazon may have
            # fewer copies (e.g. the duplicate only existed in the xlsx).  Fall
            # back to occurrence 0 so we still delete whichever copy is present.
            logger.info(
                "Falling back to occurrence 0 for '%s' (occurrence %d not found in DOM)",
                title, effective_index,
            )
            for attempt in range(5):
                row_handle = await page.evaluate_handle(_FIND_ROW_JS, [title, artist, 0])
                is_null = await row_handle.evaluate("el => el === null")
                if not is_null:
                    break
                await page.keyboard.press("End")
                await page.mouse.wheel(0, 3000)
                await page.wait_for_timeout(2000)
            is_null = await row_handle.evaluate("el => el === null")

        if is_null:
            logger.warning("Could not find '%s' by '%s' in playlist", title, artist)
            failed.append(song)
            continue

        # Scroll the element into view and hover to reveal the 3-dot menu
        await row_handle.evaluate("el => el.scrollIntoView({ block: 'center' })")
        await page.wait_for_timeout(1000)

        box = await row_handle.bounding_box()
        if not box:
            await page.wait_for_timeout(1000)
            box = await row_handle.bounding_box()

        if not box:
            logger.warning("Could not get bounding box for '%s'", title)
            failed.append(song)
            continue

        # Hover over the row to make the 3-dot menu button appear
        await page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        await page.wait_for_timeout(1000)

        # Click the 3-dot "more" button within this row
        more_clicked = await page.evaluate("""(args) => {
            const [title, artist, effectiveIndex] = args;
            const tLow = title.trim().toLowerCase();
            const aLow = artist.trim().toLowerCase();
            const rows = document.querySelectorAll('music-image-row[primary-text]');
            let count = 0;
            for (const row of rows) {
                const t = (row.getAttribute('primary-text') || '').trim().toLowerCase();
                const a = (row.getAttribute('secondary-text-1') || '').trim().toLowerCase();
                if (t === tLow && a === aLow) {
                    if (count === effectiveIndex) {
                        // Look for the more/kebab button inside the row
                        const moreBtn = row.querySelector('music-button[icon-name="more"]')
                            || row.querySelector('[icon-name="more"]')
                            || row.querySelector('[aria-label*="menu"]')
                            || row.querySelector('[aria-label*="More"]')
                            || row.querySelector('[aria-label*="more"]');
                        if (moreBtn) {
                            moreBtn.click();
                            return 'found-in-row';
                        }
                        // Try shadow root
                        if (row.shadowRoot) {
                            const sBtn = row.shadowRoot.querySelector('[icon-name="more"]')
                                || row.shadowRoot.querySelector('music-button[icon-name="more"]');
                            if (sBtn) {
                                sBtn.click();
                                return 'found-in-shadow';
                            }
                        }
                        // Fallback: look for any button-like element at the right side
                        const btns = row.querySelectorAll('music-button');
                        for (const btn of btns) {
                            const icon = btn.getAttribute('icon-name') || '';
                            if (icon === 'more' || icon === 'kebab' || icon === 'overflow') {
                                btn.click();
                                return 'found-button';
                            }
                        }
                        return null;
                    }
                    count++;
                }
            }
            return null;
        }""", [title, artist, effective_index])

        if not more_clicked:
            # Fallback: try right-clicking the row
            logger.info("3-dot menu not found, trying right-click for '%s'", title)
            await page.mouse.click(
                box["x"] + box["width"] / 2,
                box["y"] + box["height"] / 2,
                button="right"
            )

        await page.wait_for_timeout(2000)  # Wait for menu to appear

        # Look for "Remove from playlist" in the context/dropdown menu
        removed = await page.evaluate("""() => {
            // Strategy 1: Look in music-list-item elements (Amazon's custom menu items)
            const listItems = document.querySelectorAll('music-list-item');
            for (const item of listItems) {
                const text = (item.getAttribute('primary-text') || item.textContent || '').toLowerCase();
                if (text.includes('remove') && text.includes('playlist')) {
                    item.click();
                    return 'music-list-item';
                }
            }

            // Strategy 2: Look for any element with "remove" text in a popup/overlay/menu
            const allEls = document.querySelectorAll(
                '[class*="context"] *, [class*="menu"] *, [class*="popup"] *, ' +
                '[class*="overlay"] *, [class*="dropdown"] *, ' +
                '[role="menu"] *, [role="menuitem"]'
            );
            for (const el of allEls) {
                const text = (el.textContent || '').toLowerCase().trim();
                if (text.includes('remove') && text.includes('playlist')) {
                    el.click();
                    return 'context-menu';
                }
            }

            // Strategy 3: Broad search for any clickable element with the right text
            const buttons = document.querySelectorAll(
                'button, [role="button"], [role="menuitem"], a, span, music-button'
            );
            for (const btn of buttons) {
                const text = (btn.textContent || '').toLowerCase().trim();
                if (text === 'remove from playlist' || text === 'remove from this playlist'
                    || text === 'delete from playlist') {
                    btn.click();
                    return 'button';
                }
            }

            return null;
        }""")

        if removed:
            deleted += 1
            deleted_rows.add(song.get("row", 0))
            deleted_counts[norm_key] = deleted_counts.get(norm_key, 0) + 1
            logger.info("Removed '%s' from playlist (via %s)", title, removed)
            await page.wait_for_timeout(3000)  # Wait for UI to update
        else:
            # Log what's in the context menu for debugging
            menu_items = await page.evaluate("""() => {
                const items = [];
                document.querySelectorAll('music-list-item').forEach(el => {
                    items.push(el.getAttribute('primary-text') || el.textContent || '');
                });
                return items;
            }""")
            logger.warning("Could not find 'Remove from playlist' for '%s'. Menu items: %s",
                          title, menu_items)
            # Dismiss the context menu
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(1000)
            failed.append(song)

    print(f"\nRemoved {deleted}/{len(songs)} song(s) from Amazon Music playlist.")
    if failed:
        print(f"Failed to remove {len(failed)} song(s):")
        for s in failed:
            print(f"  - {s['title']} by {s['artist']}")

    return deleted_rows


async def delete_playlist(page: Page, playlist_url: str) -> None:
    """Permanently delete an Amazon Music playlist.

    Navigates to the playlist page, opens the "more options" menu on the header
    (using the recursive shadow walker from play_playlist), then clicks
    "Delete playlist" from the dropdown. Dropdown items are music-list-item
    elements — NOT music-button — so they are handled with a direct JS query
    rather than _FIND_AND_CLICK. Confirms any modal that appears.
    Raises RuntimeError if any step fails.
    """
    logger.info("Navigating to playlist for deletion: %s", playlist_url)
    await page.goto(playlist_url, wait_until="domcontentloaded", timeout=LONG_TIMEOUT)

    try:
        await page.wait_for_selector(
            SELECTORS["playlist_header"], state="attached", timeout=LONG_TIMEOUT
        )
        logger.info("Playlist header detected")
    except Exception:
        logger.warning("Timed out waiting for playlist header")

    try:
        await page.wait_for_selector(
            'music-image-row[primary-text]', state="attached", timeout=LONG_TIMEOUT
        )
    except Exception:
        pass
    await page.wait_for_timeout(2500)

    # Recursive shadow walker — same as _FIND_AND_CLICK in play_playlist.
    # Used only for the "more" button which lives inside music-detail-header's
    # shadow tree. Dropdown items (music-list-item) are NOT buttons and are
    # handled separately below.
    _FIND_AND_CLICK = r"""
        (args) => {
            const { ariaSubstrings, iconNames, scopeTagNames } = args;
            const matches = (el) => {
                const aria = (el.getAttribute && el.getAttribute('aria-label') || '').toLowerCase();
                const icon = (el.getAttribute && el.getAttribute('icon-name') || '').toLowerCase();
                for (const s of ariaSubstrings) if (s && aria.includes(s)) return true;
                for (const s of iconNames)      if (s && icon === s)        return true;
                return false;
            };
            const clickable = (el) => {
                const tag = (el.tagName || '').toLowerCase();
                if (tag === 'music-button' || tag === 'button') return true;
                return (el.getAttribute && el.getAttribute('role') || '').toLowerCase() === 'button';
            };
            const scoped = scopeTagNames && scopeTagNames.length
                ? Array.from(document.querySelectorAll(scopeTagNames.join(',')))
                : [];
            const roots = scoped.length ? scoped : [document];
            const visited = new Set();
            const walk = (root) => {
                if (!root || visited.has(root)) return null;
                visited.add(root);
                const all = root.querySelectorAll ? root.querySelectorAll('*') : [];
                for (const el of all) {
                    if (clickable(el) && matches(el)) {
                        try { el.click(); } catch (e) { continue; }
                        return (el.getAttribute('aria-label') || el.getAttribute('icon-name')
                            || el.textContent || 'match').toString().slice(0, 60);
                    }
                    if (el.shadowRoot) {
                        const hit = walk(el.shadowRoot);
                        if (hit) return hit;
                    }
                }
                return null;
            };
            for (const r of roots) {
                const hit = walk(r);
                if (hit) return hit;
            }
            return null;
        }
    """

    # Strategy 1: direct "Delete playlist" button on the header (some Amazon
    # Music layouts expose this as a first-class button with the right aria-label).
    fired = await page.evaluate(_FIND_AND_CLICK, {
        "ariaSubstrings": ["delete playlist", "delete this playlist"],
        "iconNames": [],
        "scopeTagNames": ["music-detail-header"],
    })
    if fired:
        logger.info("Clicked direct delete button on header (via %r)", fired)
    else:
        # Strategy 2: open the "more options" (3-dot) menu, then pick the item.
        opened = await page.evaluate(_FIND_AND_CLICK, {
            "ariaSubstrings": ["more", "options"],
            "iconNames": ["more"],
            "scopeTagNames": ["music-detail-header"],
        })
        if not opened:
            raise RuntimeError(
                "Could not find a delete button or options menu on the playlist page. "
                "Make sure the playlist page has fully loaded."
            )
        logger.info("Opened playlist options menu (via %r)", opened)
        await page.wait_for_timeout(2000)

        # Amazon Music renders dropdown items as music-list-item elements with a
        # primary-text attribute — they are NOT buttons and won't be found by the
        # shadow walker above. Use a direct querySelectorAll instead.
        fired = await page.evaluate("""() => {
            const items = document.querySelectorAll('music-list-item');
            for (const item of items) {
                const text = (
                    item.getAttribute('primary-text') || item.textContent || ''
                ).toLowerCase().trim();
                if (text.includes('delete') && text.includes('playlist')) {
                    item.click();
                    return item.getAttribute('primary-text') || text;
                }
            }
            // Fallback: any button whose exact text is "delete playlist"
            for (const el of document.querySelectorAll(
                'button, [role="button"], music-button'
            )) {
                const text = (el.textContent || '').toLowerCase().trim();
                if (text === 'delete playlist' || text === 'delete this playlist') {
                    el.click();
                    return 'btn:' + text;
                }
            }
            return null;
        }""")

        if not fired:
            menu_items = await page.evaluate("""() =>
                Array.from(document.querySelectorAll('music-list-item')).map(
                    el => el.getAttribute('primary-text') || el.textContent?.trim() || ''
                )
            """)
            logger.warning("Menu items visible: %s", menu_items)
            await page.keyboard.press("Escape")
            raise RuntimeError(
                "Could not find 'Delete playlist' in the options menu. "
                f"Items visible: {menu_items}"
            )
        logger.info("Clicked 'Delete playlist' menu item: %r", fired)
    await page.wait_for_timeout(2000)

    # Handle the confirmation modal.
    # Scope the search to dialog/modal elements to avoid accidentally clicking
    # the wrong "Delete" button elsewhere on the page.
    confirmed = await page.evaluate("""() => {
        const dialogRoots = Array.from(document.querySelectorAll(
            '[role="dialog"], [role="alertdialog"], music-dialog, ' +
            '[class*="modal" i], [class*="dialog" i], [class*="overlay" i]'
        ));
        const search = dialogRoots.length ? dialogRoots : [document];
        for (const root of search) {
            for (const el of root.querySelectorAll(
                'button, music-button, [role="button"]'
            )) {
                const text = (
                    el.textContent || el.getAttribute('aria-label') || ''
                ).toLowerCase().trim();
                // Match the confirm button exactly — not "Cancel", not
                // "Delete playlist" (that was the menu item, now gone).
                if (text === 'delete' || text === 'yes' || text === 'confirm') {
                    el.click();
                    return text;
                }
            }
        }
        return null;
    }""")

    if confirmed:
        logger.info("Confirmed deletion dialog (clicked %r button)", confirmed)
    else:
        logger.info("No confirmation dialog detected — deletion assumed immediate")
    await page.wait_for_timeout(3000)
    logger.info("Playlist deletion complete")


# ---------------------------------------------------------------------------
# Playlist reorder helpers
# ---------------------------------------------------------------------------

# Scroll the playlist's inner container to *scrollTop* px from its top.
# Falls back to window.scrollTo if the container can't be found.
_SCROLL_CONTAINER_JS = """(scrollTop) => {
    const row = document.querySelector('music-image-row[primary-text]');
    if (!row) { window.scrollTo(0, scrollTop); return; }
    let el = row.parentElement;
    while (el && el !== document.body) {
        const style = el.getAttribute('style') || '';
        if (style.includes('overflow') && style.includes('will-change')) {
            el.scrollTop = scrollTop;
            return;
        }
        el = el.parentElement;
    }
    window.scrollTo(0, scrollTop);
}"""

# Returns coords for both source and target in one call, BEFORE any drag
# starts.  Skips any row whose click point falls behind the persistent
# now-playing player bar — a fixed-position overlay z-stacked OVER the
# playlist scroll container (NOT contained within it).  We locate the
# player bar via its music-horizontal-item child, then walk up to find
# the topmost wide ancestor (the actual player bar wrapper).
# Returns null src/tgt if the corresponding row isn't currently in the
# safe zone; caller should scroll and retry.
_FETCH_DRAG_COORDS_JS = """(args) => {
    const [sIdx1, sLow, saLow, tIdx1, tLow, taLow] = args;

    // Locate the player bar's effective top y.  Amazon scatters
    // music-horizontal-item elements across the page (many off-screen or
    // narrow UI bits), so we MUST filter to candidates that are (a) visible
    // in the viewport and (b) wide enough to be the player bar.  Then pick
    // the one with the largest top — that's the player bar.
    function findPlayerBarTop() {
        const items = document.querySelectorAll('music-horizontal-item');
        let item = null;
        for (const it of items) {
            const r = it.getBoundingClientRect();
            if (r.top < 0 || r.top >= window.innerHeight) continue;
            if (r.width < window.innerWidth * 0.3) continue;
            if (!item || r.top > item.getBoundingClientRect().top) item = it;
        }
        if (!item) return window.innerHeight - 120;
        // Walk up looking for the player-bar wrapper (position:fixed/sticky).
        let el = item.parentElement;
        while (el && el !== document.body && el !== document.documentElement) {
            const cs = window.getComputedStyle(el);
            if (cs.position === 'fixed' || cs.position === 'sticky') {
                return el.getBoundingClientRect().top;
            }
            el = el.parentElement;
        }
        // Fallback: item.top minus margin for padding/ancestor wrapping above.
        return item.getBoundingClientRect().top - 30;
    }

    function rowIndex1(row) {
        const idxEl = row.querySelector('span.index')
            || (row.shadowRoot && row.shadowRoot.querySelector('span.index'));
        if (!idxEl) return null;
        const n = parseInt(idxEl.textContent.trim(), 10);
        return Number.isFinite(n) ? n : null;
    }

    const playerBarTop = findPlayerBarTop();
    const safeTop      = 100;                  // below playlist column headers
    const safeBottom   = playerBarTop - 10;    // 10px above player bar overlay

    // Pick rows by absolute position (span.index) FIRST — disambiguates
    // duplicate-titled rows (which can appear as drop targets even though
    // duplicate-titled SOURCES are filtered upfront).  Fall back to
    // attribute matching only if position lookup fails (e.g., span.index
    // missing on this row variant).
    let srcRow = null, tgtRow = null;
    let srcByIdx = false, tgtByIdx = false;
    const rows = document.querySelectorAll('music-image-row[primary-text]');
    for (const row of rows) {
        const r1 = rowIndex1(row);
        if (!srcRow && r1 === sIdx1) { srcRow = row; srcByIdx = true; }
        if (!tgtRow && r1 === tIdx1) { tgtRow = row; tgtByIdx = true; }
        if (srcRow && tgtRow) break;
    }
    if (!srcRow) {
        for (const row of rows) {
            const t = (row.getAttribute('primary-text') || '').trim().toLowerCase();
            const a = (row.getAttribute('secondary-text-1') || '').trim().toLowerCase();
            if (t === sLow && a === saLow) { srcRow = row; break; }
        }
    }
    if (!tgtRow) {
        for (const row of rows) {
            const t = (row.getAttribute('primary-text') || '').trim().toLowerCase();
            const a = (row.getAttribute('secondary-text-1') || '').trim().toLowerCase();
            if (t === tLow && a === taLow) { tgtRow = row; break; }
        }
    }

    // Sanity check: when matched by position, verify the row's title hint
    // matches expectation.  Amazon's `span.index` is reliable after a 2 s
    // scroll settle, but if it disagrees with the song attributes that's a
    // signal the DOM is mid-transition; surface to the caller.
    let srcTitleMismatch = false, tgtTitleMismatch = false;
    if (srcRow && srcByIdx) {
        const t = (srcRow.getAttribute('primary-text') || '').trim().toLowerCase();
        const a = (srcRow.getAttribute('secondary-text-1') || '').trim().toLowerCase();
        srcTitleMismatch = (t !== sLow || a !== saLow);
    }
    if (tgtRow && tgtByIdx) {
        const t = (tgtRow.getAttribute('primary-text') || '').trim().toLowerCase();
        const a = (tgtRow.getAttribute('secondary-text-1') || '').trim().toLowerCase();
        tgtTitleMismatch = (t !== tLow || a !== taLow);
    }

    function rectInfo(row) {
        if (!row) return null;
        const r = row.getBoundingClientRect();
        return {
            top: r.top, height: r.height, left: r.left,
            cx: r.left + 20,
            centerY: r.top + r.height / 2,
            topY: r.top + 4,
            title: (row.getAttribute('primary-text') || '').trim(),
            artist: (row.getAttribute('secondary-text-1') || '').trim(),
        };
    }
    const srcInfo = rectInfo(srcRow);
    const tgtInfo = rectInfo(tgtRow);

    let src = null, tgt = null;
    if (srcInfo && srcInfo.centerY > safeTop && srcInfo.centerY < safeBottom) {
        src = {x: srcInfo.cx, y: srcInfo.centerY};
    }
    if (tgtInfo && tgtInfo.topY > safeTop && tgtInfo.topY < safeBottom) {
        tgt = {x: tgtInfo.cx, y: tgtInfo.topY};
    }

    return {
        src, tgt, safeTop, safeBottom, playerBarTop,
        viewportHeight: window.innerHeight,
        srcInDom: !!srcRow, tgtInDom: !!tgtRow,
        srcInfo, tgtInfo,
        srcByIdx, tgtByIdx,
        srcTitleMismatch, tgtTitleMismatch,
    };
}"""

_ROW_HEIGHT = 74   # approximate px height of each music-image-row


_WAIT_FOR_ROWS_JS = """([sTitLow, tTitLow]) => {
    const rows = document.querySelectorAll('music-image-row[primary-text]');
    let srcFound = false, tgtFound = false;
    for (const r of rows) {
        const t = (r.getAttribute('primary-text') || '').trim().toLowerCase();
        if (t === sTitLow) srcFound = true;
        if (t === tTitLow) tgtFound = true;
        if (srcFound && tgtFound) return true;
    }
    return false;
}"""


async def _scroll_both_into_view(
    page: Page,
    src_abs: int,
    tgt_abs: int,
) -> None:
    """Scroll the list so that both the source row and target row are visible
    simultaneously.  Centres the viewport on the midpoint between them.

    Amazon's virtualizer uses IntersectionObserver, which only fires on real
    scroll events — not programmatic scrollTop changes.  We therefore use
    mouse-wheel events to trigger the virtualizer, then wait 2 s for Chromium's
    scroll-inertia state to fully settle.  Without that wait, a subsequent
    mousedown is mis-classified as a continued scroll and the drag is ignored.
    """
    mid = (src_abs + tgt_abs) // 2
    scroll_target = max(0, mid * _ROW_HEIGHT - 300)
    vp = page.viewport_size or {"width": 1280, "height": 800}
    await page.mouse.move(vp["width"] // 2, vp["height"] // 2)
    await page.mouse.wheel(0, -50000)           # scroll to top; loads top rows
    if scroll_target > 0:
        await page.mouse.wheel(0, scroll_target)
    # Let Chromium's scroll-inertia state expire before the drag gesture.
    await page.wait_for_timeout(2000)


# Reads visible (in-DOM) rows and returns their absolute index + title + artist.
# Amazon renders the row number in a span.index inside each music-image-row;
# that text is the 1-based position regardless of virtualization, so we can
# compare it directly against our predicted current_order to detect divergence.
_VERIFY_VISIBLE_ROWS_JS = """() => {
    // Only verify rows actually inside the viewport. Amazon's virtualised list
    // keeps off-screen rows in the DOM with non-zero size and recycles those
    // nodes for new tracks during scroll; the `primary-text` attribute updates
    // immediately but the `span.index` text content can lag, leaving a recycled
    // node with current title but stale index. Reading those produces phantom
    // mismatches at fixed offsets (e.g. row showing 'Alone I Break' with
    // span.index='65' because the node previously held track 65 'Somebody
    // Someone'). Filtering to rows that intersect the viewport eliminates the
    // recycled-DOM-cache rows whose indices haven't been re-rendered yet.
    const rows = document.querySelectorAll('music-image-row[primary-text]');
    const vh = window.innerHeight || document.documentElement.clientHeight;
    const vw = window.innerWidth || document.documentElement.clientWidth;
    const out = [];
    for (const row of rows) {
        const rect = row.getBoundingClientRect();
        if (rect.height < 1 || rect.width < 1) continue;
        if (rect.bottom <= 0 || rect.top >= vh) continue;
        if (rect.right <= 0 || rect.left >= vw) continue;
        const idxEl = row.querySelector('span.index')
            || (row.shadowRoot && row.shadowRoot.querySelector('span.index'));
        const idxText = idxEl ? idxEl.textContent.trim() : '';
        const idx = parseInt(idxText, 10);
        if (!Number.isFinite(idx)) continue;
        out.push({
            idx: idx,
            title: (row.getAttribute('primary-text') || '').trim(),
            artist: (row.getAttribute('secondary-text-1') || '').trim(),
        });
    }
    return out;
}"""


async def _verify_against_prediction(
    page: Page,
    current_order: list[dict],
) -> list[tuple[int, str, str, str, str]]:
    """Compare visible rows against `current_order`. Returns list of mismatches
    as (zero_based_pos, predicted_title, predicted_artist, actual_title,
    actual_artist). Empty list means everything visible matches the prediction.
    """
    visible = await page.evaluate(_VERIFY_VISIBLE_ROWS_JS)
    mismatches: list[tuple[int, str, str, str, str]] = []
    for v in visible:
        pos = v["idx"] - 1   # convert to 0-based
        if pos < 0 or pos >= len(current_order):
            continue
        pred = current_order[pos]
        if (pred["title"].strip().lower() != v["title"].strip().lower()
                or pred["artist"].strip().lower() != v["artist"].strip().lower()):
            mismatches.append(
                (pos, pred["title"], pred["artist"], v["title"], v["artist"])
            )
    return mismatches


async def _verify_against_prediction_stable(
    page: Page,
    current_order: list[dict],
    settle_ms: int = 1500,
    confirm_ms: int = 800,
) -> list[tuple[int, str, str, str, str]]:
    """Verify with a settle wait and a double-check pass.

    Amazon's virtualised tracklist can show transiently-stale `span.index`
    values for ~1 s after a drop while it re-renders rows.  Reading those
    in-flight values produces phantom mismatches and triggers needless
    full-playlist re-scrapes.

    We mitigate by:
      1. Waiting `settle_ms` for the DOM to quiesce.
      2. Running an initial verify; if clean, return immediately.
      3. If mismatches found, wait `confirm_ms` and verify again — only
         mismatches that appear at the same (pos, predicted title, predicted
         artist) in BOTH passes are treated as real divergence.
    """
    await page.wait_for_timeout(settle_ms)
    first = await _verify_against_prediction(page, current_order)
    if not first:
        return []
    await page.wait_for_timeout(confirm_ms)
    second = await _verify_against_prediction(page, current_order)
    second_keys = {(m[0], m[1].strip().lower(), m[2].strip().lower()) for m in second}
    confirmed = [
        m for m in first
        if (m[0], m[1].strip().lower(), m[2].strip().lower()) in second_keys
    ]
    if first and not confirmed:
        logger.info(
            "Reorder: ignored %d transient mismatch(es) — DOM settled to "
            "match prediction on second check",
            len(first),
        )
    return confirmed


async def _drag_song(
    page: Page,
    src_title: str,
    src_artist: str,
    tgt_title: str,
    tgt_artist: str,
    src_abs: int,
    tgt_abs: int,
) -> bool:
    """Drag *src* song to the screen position currently occupied by *tgt* song.

    Both coordinates are captured in a single JS call **before** mousedown to
    avoid the DOM-shift problem that occurs when Amazon's drag handler fires on
    mousedown.  The drag starts from the left-edge / row-number area (x ≈ 20px
    into the row) — the same area that was confirmed to work in manual testing.

    If the two songs are too far apart to be simultaneously visible (~10+ rows),
    the move is broken into viewport-sized hops, each calling this function
    recursively with an intermediate virtual position.

    Returns True if the drag completed (both coords were found).
    """
    sLow  = src_title.strip().lower()
    saLow = src_artist.strip().lower()
    tLow  = tgt_title.strip().lower()
    taLow = tgt_artist.strip().lower()
    sIdx1 = src_abs + 1
    tIdx1 = tgt_abs + 1
    js_args = [sIdx1, sLow, saLow, tIdx1, tLow, taLow]

    # Scroll so both rows are visible, then poll (via page.evaluate, not
    # wait_for_function which fails silently) until both are in the DOM.
    await _scroll_both_into_view(page, src_abs, tgt_abs)
    for _poll in range(40):   # up to 4 s at 100 ms intervals
        in_dom = await page.evaluate(_WAIT_FOR_ROWS_JS, [sLow, tLow])
        if in_dom:
            break
        await page.wait_for_timeout(100)

    # Capture both positions before touching the mouse.  If either row is
    # not yet in a usable safe zone, scroll the right direction and retry.
    # Direction logic (see Trap 1 in feedback_amazon_music_dom.md — the
    # player-bar overlays the bottom of the playlist scroll container, so
    # rows whose centers sit below safeBottom must be pushed UP via a wheel
    # DOWN; rows that aren't in the DOM at all need a scroll TOWARD their
    # absolute position):
    #
    #   - row in DOM but center below safeBottom → wheel down (push up)
    #   - row in DOM but center above safeTop    → wheel up (push down)
    #   - row not in DOM                         → wheel toward expected
    #
    # When both rows are missing we fall back to re-centering on the
    # midpoint via _scroll_both_into_view (slow but reliable).
    coords = await page.evaluate(_FETCH_DRAG_COORDS_JS, js_args)
    src = coords.get("src")
    tgt = coords.get("tgt")

    def _compute_dy() -> int:
        """Wheel delta (positive = scroll down) to bring missing rows into
        the safe zone.  Uses measured rect positions when available — this
        is critical because _ROW_HEIGHT is approximate and the playlist
        header chrome adds ~200px to the first-row offset, so position-from-
        index estimates are unreliable.
        """
        s_info = coords.get("srcInfo")
        t_info = coords.get("tgtInfo")
        safe_top = coords.get("safeTop", 100)
        safe_bottom = coords.get("safeBottom", 700)
        safe_mid = (safe_top + safe_bottom) / 2
        max_step = int((safe_bottom - safe_top) * 0.45)

        # Both rows in DOM but at least one out of safe zone — re-center
        # their midpoint at safe_mid in one shot.
        if s_info and t_info:
            mid = (s_info["centerY"] + t_info["centerY"]) / 2
            return max(-max_step, min(max_step, int(mid - safe_mid)))
        # Only the row we still need has info; aim that row at safe_mid.
        info = s_info if not src else t_info
        if info:
            return max(-max_step, min(max_step, int(info["centerY"] - safe_mid)))
        # Neither row's info available.  Fall back to row-position estimate.
        missing_abs = src_abs if not src else tgt_abs
        other_abs   = tgt_abs if not src else src_abs
        if missing_abs > other_abs:
            return _ROW_HEIGHT * 3
        if missing_abs < other_abs:
            return -_ROW_HEIGHT * 3
        return _ROW_HEIGHT * 3

    for _retry in range(4):
        if src and tgt:
            break
        s_in_dom = coords.get("srcInDom", False)
        t_in_dom = coords.get("tgtInDom", False)
        safe_top    = coords.get("safeTop", 100)
        safe_bottom = coords.get("safeBottom", 700)

        # If neither row is in DOM at all, do a full re-center via the
        # to-top + scroll-to-midpoint dance — only way to (re)load rows.
        if not s_in_dom and not t_in_dom:
            logger.info(
                "_drag_song: neither row in DOM — re-centering via full "
                "scroll (retry %d)", _retry + 1,
            )
            await _scroll_both_into_view(page, src_abs, tgt_abs)
        else:
            dy = _compute_dy()
            logger.info(
                "_drag_song: src=%s(inDom=%s) tgt=%s(inDom=%s) safe=[%s,%s] "
                "playerBarTop=%s vh=%s — wheel(%+d) for retry %d",
                bool(src), s_in_dom, bool(tgt), t_in_dom,
                safe_top, safe_bottom,
                coords.get("playerBarTop"), coords.get("viewportHeight"),
                dy, _retry + 1,
            )
            if abs(dy) >= 10:
                vp = page.viewport_size or {"width": 1280, "height": 800}
                await page.mouse.move(vp["width"] // 2, vp["height"] // 2)
                await page.mouse.wheel(0, dy)
                await page.wait_for_timeout(1500)
            else:
                # No useful direction — wait a beat for virtualizer to settle.
                await page.wait_for_timeout(800)
        coords = await page.evaluate(_FETCH_DRAG_COORDS_JS, js_args)
        src = coords.get("src")
        tgt = coords.get("tgt")

    # Stabilize coords before the drag: span.index can be transiently stale for
    # ~1s after a wheel scroll while Amazon's virtualizer re-renders rows.  When
    # stale, the JS picks the row currently *displaying* span.index = N — which
    # may be at a different logical position than N.  The drag then fires at a
    # pixel Y belonging to the wrong logical row, landing the song far from the
    # intended slot (BBE 13→14 landed at 21, Creamer hop 41→35 landed at 33,
    # both runs 2026-05-06).  The existing retry loop's 1500 ms wait isn't
    # always enough — drags that needed only one wheel retry landed wrong, while
    # drags with two retries (giving span.index more total settle time) landed
    # right.
    #
    # Refetch with a longer settle wait and confirm srcTitleMismatch /
    # tgtTitleMismatch are clear.  If a mismatch persists, bail rather than
    # corrupt the virtual order — verifier-triggered rescrape would catch it
    # later but cost ~95 s; skipping is cheaper.
    if src and tgt:
        stabilized = False
        for _stab in range(3):
            await page.wait_for_timeout(900 if _stab == 0 else 700)
            new_coords = await page.evaluate(_FETCH_DRAG_COORDS_JS, js_args)
            new_src = new_coords.get("src")
            new_tgt = new_coords.get("tgt")
            if not (new_src and new_tgt):
                continue
            coords = new_coords
            src, tgt = new_src, new_tgt
            if (not coords.get("srcTitleMismatch")
                    and not coords.get("tgtTitleMismatch")):
                stabilized = True
                break
            logger.info(
                "_drag_song: stabilize attempt %d/3 — title mismatch "
                "(src@%d='%s' exp '%s', tgt@%d='%s' exp '%s'), retrying",
                _stab + 1,
                sIdx1, (coords.get("srcInfo") or {}).get("title", "?"), src_title,
                tIdx1, (coords.get("tgtInfo") or {}).get("title", "?"), tgt_title,
            )
        if not stabilized:
            logger.warning(
                "_drag_song: coords never stabilized for '%s' → '%s' — "
                "span.index/title disagreement persisted across 3 attempts. "
                "Skipping drag to avoid corrupting playlist order.",
                src_title, tgt_title,
            )
            return False

    # Surface DOM/index disagreement at the row level — useful when chasing
    # a future regression where span.index goes stale during reorder.
    if coords.get("srcTitleMismatch"):
        logger.warning(
            "_drag_song: src row at idx %d has title '%s' (expected '%s') — "
            "DOM may be stale", sIdx1,
            (coords.get("srcInfo") or {}).get("title", "?"), src_title,
        )
    if coords.get("tgtTitleMismatch"):
        logger.warning(
            "_drag_song: tgt row at idx %d has title '%s' (expected '%s') — "
            "DOM may be stale", tIdx1,
            (coords.get("tgtInfo") or {}).get("title", "?"), tgt_title,
        )

    if not src:
        logger.warning(
            "_drag_song: source '%s' never reached safe zone (player-bar obscured)",
            src_title,
        )
        return False

    if not tgt:
        logger.warning(
            "_drag_song: target '%s' never reached safe zone (player-bar obscured or too far)",
            tgt_title,
        )
        return False

    # Execute the drag: move → hover-settle → press → move to target → release.
    # Hover settle gives Amazon's per-row hover state time to apply before
    # mousedown — without it, the press can be interpreted as a stale-state
    # click instead of a drag start.
    await page.mouse.move(src["x"], src["y"])
    await page.wait_for_timeout(300)
    await page.mouse.down()
    await page.wait_for_timeout(200)
    await page.mouse.move(tgt["x"], tgt["y"], steps=25)
    await page.wait_for_timeout(100)
    await page.mouse.up()
    await page.wait_for_timeout(800)
    return True


async def reorder_playlist(
    page: Page,
    target_songs: list[dict],
    playlist_url: str | None = None,
    playlist_name: str | None = None,
) -> dict:
    """Reorder a playlist so its tracks match the order of *target_songs*.

    Strategy — selection sort with title/artist–based drag:
      • Collect the full playlist order once upfront (via _collect_all_tracks).
      • Maintain a virtual *current_order* list that mirrors what Amazon has.
      • For each target position i, find where that song currently sits (j >= i)
        and drag it from j to i, releasing on whichever song is currently at i.
      • Update current_order after every successful drag.

    Because songs are found by title/artist (not DOM index), this works correctly
    with Amazon's virtualised row list — only ~10–11 rows are in the DOM at once.

    Duplicate-titled songs are skipped (Amazon's drag handler can't
    disambiguate them — drags silently no-op).  After every successful drag
    we verify visible rows match our prediction; on divergence we re-scrape
    the playlist to resync.

    Returns {'moved': int, 'skipped': int, 'skipped_duplicates': int,
             'duplicate_keys': list[(title_lower, artist_lower)]}.
    """
    if playlist_url:
        logger.info("Opening playlist for reorder: %s", playlist_url)
        await page.goto(playlist_url, wait_until="domcontentloaded", timeout=LONG_TIMEOUT)
    elif playlist_name:
        await _navigate_to_playlist_by_name(page, playlist_name)
    else:
        raise ValueError("Must provide playlist_url or playlist_name")

    await page.wait_for_selector(
        "music-image-row[primary-text]", state="attached", timeout=LONG_TIMEOUT
    )
    await page.wait_for_timeout(3000)

    # Scroll to top before collecting so order is deterministic.
    await page.evaluate(_SCROLL_CONTAINER_JS, 0)
    await page.wait_for_timeout(800)

    # Collect the full current playlist order (scrolls top→bottom).
    songs = await _collect_all_tracks(page)
    current_order = [{"title": s.title, "artist": s.artist} for s in songs]
    logger.info("Reorder: %d songs in current playlist", len(current_order))
    for idx, s in enumerate(current_order):
        logger.info("  INITIAL [%d] %s – %s", idx, s["title"], s["artist"])

    def _norm(title: str, artist: str) -> tuple[str, str]:
        return (title.strip().lower(), artist.strip().lower())

    target_keys = [
        _norm(s.get("title", ""), s.get("artist", "")) for s in target_songs
    ]

    # Detect duplicate-titled songs.  Amazon's drag handler appears unable to
    # disambiguate rows whose (title, artist) match more than one track in the
    # playlist — the drag silently no-ops.  We skip such rows up front rather
    # than let silent failures corrupt our position prediction.  We also flag
    # duplicates in the target list, since we have no way to know which copy
    # the user meant to put at which position.
    cur_counts = Counter(_norm(s["title"], s["artist"]) for s in current_order)
    tgt_counts = Counter(target_keys)
    duplicate_keys = {
        k for k, c in cur_counts.items() if c > 1
    } | {
        k for k, c in tgt_counts.items() if c > 1
    }
    if duplicate_keys:
        logger.warning(
            "Reorder: %d duplicate-titled song(s) will be skipped (Amazon's "
            "drag handler can't disambiguate duplicates):", len(duplicate_keys),
        )
        for k in sorted(duplicate_keys):
            logger.warning("  duplicate: %s – %s", k[0], k[1])

    async def _resync_after_drag(reason: str) -> list[dict]:
        """Re-scrape the full playlist and return a fresh current_order."""
        logger.warning("Reorder: %s — re-scraping playlist to resync", reason)
        vp = page.viewport_size or {"width": 1280, "height": 800}
        await page.mouse.move(vp["width"] // 2, vp["height"] // 2)
        await page.mouse.wheel(0, -50000)
        await page.wait_for_timeout(1000)
        fresh = await _collect_all_tracks(page)
        return [{"title": s.title, "artist": s.artist} for s in fresh]

    moved = 0
    skipped = 0
    skipped_dup = 0

    # Bounded retry on real divergence: when a resync reveals the drag
    # didn't land where intended, we re-evaluate position `i` against the
    # resynced `current_order` instead of advancing.  Often the song is now
    # close to `i` (e.g. it landed one row off), and a single retry from the
    # fresh state corrects it.  Each retry costs ~1.5 min (full re-scrape),
    # so cap to keep pathological cases bounded.
    MAX_RETRIES_PER_POS = 2

    i = 0
    retries_at_i = 0
    while i < len(target_keys):
        target_key = target_keys[i]

        if target_key in duplicate_keys:
            skipped_dup += 1
            i += 1; retries_at_i = 0
            continue

        current_keys = [_norm(s["title"], s["artist"]) for s in current_order]

        # Find target song.  Prefer an instance at idx >= i (upward drag —
        # the natural selection-sort case).  If none, look at idx < i for a
        # downward drag — needed when prior duplicate-skips broke the
        # invariant that earlier passes push later rows down by one.
        j = next(
            (idx for idx, k in enumerate(current_keys) if k == target_key and idx >= i),
            None,
        )
        if j is None:
            j = next(
                (idx for idx, k in enumerate(current_keys) if k == target_key and idx < i),
                None,
            )
        if j is None:
            logger.warning("Reorder: '%s' not found in current order", target_key[0])
            skipped += 1
            i += 1; retries_at_i = 0
            continue
        if j == i:
            i += 1; retries_at_i = 0
            continue  # Already in place.

        # Amazon's drop semantic is symmetric (verified empirically
        # 2026-05-05): "drop on row at K → song lands at K".  For an
        # up-drag the drop target shifts down to K+1; for a down-drag it
        # shifts up to K-1.  Either way, dropping on the row currently at
        # the goal position lands the song at the goal.
        going_down = j < i
        drop_abs = i

        src = current_order[j]                # song to drag
        tgt = current_order[drop_abs]         # row we'll drop on

        logger.info(
            "Reorder [%d/%d]: drag %s '%s' from %d → %d  (drop on '%s' at %d)",
            i + 1, len(target_keys),
            "DOWN" if going_down else "UP",
            src["title"], j, i, tgt["title"], drop_abs,
        )

        # If source and target are more than ~8 rows apart they won't both fit
        # in the visible window.  Move in viewport-sized hops: each hop shifts
        # the source ~6 positions toward the target.  Drop target = landing
        # position (Amazon's symmetric drop-on-K-lands-at-K semantic).
        # HOP=6 keeps source+target within the safe zone (~7.8 rows tall) with
        # margin; HOP=8 was right at the edge and failed when re-center couldn't
        # fit both rows simultaneously (see Creamer 41→25 case, 2026-05-06).
        HOP = 6
        cur_j = j
        hop_ok = True
        hop_resynced = False

        def _next_hop_landing(cj: int) -> int | None:
            """Return the next hop's landing index, or None if no further
            hopping is required (final leg covers the rest)."""
            if going_down:
                landing = cj + HOP
                return landing if landing < i else None
            else:
                landing = cj - HOP
                return landing if landing > i else None

        while True:
            hop_landing_abs = _next_hop_landing(cur_j)
            if hop_landing_abs is None:
                break
            hop_drop_row = current_order[hop_landing_abs]
            logger.info(
                "  hop: drag '%s' from %d → %d  (drop on '%s')",
                src["title"], cur_j, hop_landing_abs, hop_drop_row["title"],
            )
            ok = await _drag_song(
                page,
                src_title=src["title"],
                src_artist=src["artist"],
                tgt_title=hop_drop_row["title"],
                tgt_artist=hop_drop_row["artist"],
                src_abs=cur_j,
                tgt_abs=hop_landing_abs,
            )
            if not ok:
                hop_ok = False
                break
            # Symmetric insert: pop(cur_j); insert(hop_landing_abs, song)
            # for both up- and down-hops.
            song = current_order.pop(cur_j)
            current_order.insert(hop_landing_abs, song)
            cur_j = hop_landing_abs

            # Verify (with settle wait + double-check) and resync on real
            # divergence — see _verify_against_prediction_stable.
            mismatches = await _verify_against_prediction_stable(page, current_order)
            if mismatches:
                logger.warning(
                    "Reorder: %d mismatch(es) after hop drag of '%s' — first: "
                    "pos %d predicted '%s – %s', actual '%s – %s'",
                    len(mismatches), src["title"], *mismatches[0],
                )
                current_order = await _resync_after_drag("hop divergence")
                hop_ok = False
                hop_resynced = True
                break

        if not hop_ok:
            if hop_resynced and retries_at_i < MAX_RETRIES_PER_POS:
                retries_at_i += 1
                logger.info(
                    "Reorder: retry %d/%d at position %d after hop divergence",
                    retries_at_i, MAX_RETRIES_PER_POS, i,
                )
                continue  # Retry same `i` against the resynced order.
            if hop_resynced:
                logger.warning(
                    "Reorder: gave up on '%s' at position %d after %d retries",
                    src["title"], i, retries_at_i,
                )
            else:
                logger.warning("Reorder: hop failed for '%s'", src["title"])
            skipped += 1
            i += 1; retries_at_i = 0
            continue

        # Final leg: cur_j → i.  Re-read drop row in case current_order
        # shifted during hops (length unchanged but indexes did).
        final_drop_row = current_order[i]
        ok = await _drag_song(
            page,
            src_title=src["title"],
            src_artist=src["artist"],
            tgt_title=final_drop_row["title"],
            tgt_artist=final_drop_row["artist"],
            src_abs=cur_j,
            tgt_abs=i,
        )

        if ok:
            # pop(cur_j); insert(i, song) lands the song at idx i for both
            # up- and down-drags (drop_abs was chosen accordingly).
            song = current_order.pop(cur_j)
            current_order.insert(i, song)

            mismatches = await _verify_against_prediction_stable(page, current_order)
            if mismatches:
                logger.warning(
                    "Reorder: %d mismatch(es) after drag of '%s' — first: "
                    "pos %d predicted '%s – %s', actual '%s – %s'",
                    len(mismatches), src["title"], *mismatches[0],
                )
                current_order = await _resync_after_drag("final drag divergence")
                if retries_at_i < MAX_RETRIES_PER_POS:
                    retries_at_i += 1
                    logger.info(
                        "Reorder: retry %d/%d at position %d after final divergence",
                        retries_at_i, MAX_RETRIES_PER_POS, i,
                    )
                    continue  # Retry same `i` without crediting this drag.
                logger.warning(
                    "Reorder: gave up on '%s' at position %d after %d retries",
                    src["title"], i, retries_at_i,
                )
                # Drag DID move the row in Amazon (just landed wrong); count
                # it so the result reflects that work happened, even if the
                # final position isn't what we asked for.
                moved += 1
                i += 1; retries_at_i = 0
                continue

            moved += 1
            i += 1; retries_at_i = 0
        else:
            logger.warning("Reorder: final drag failed for '%s'", src["title"])
            skipped += 1
            i += 1; retries_at_i = 0

    logger.info(
        "Reorder complete: %d moved, %d skipped (drag failures), "
        "%d skipped (duplicates)",
        moved, skipped, skipped_dup,
    )
    logger.info("Virtual final order (what we think Amazon has):")
    for idx, s in enumerate(current_order):
        logger.info("  VIRTUAL [%d] %s", idx, s["title"])

    # Re-scrape to confirm actual Amazon order — use wheel to top so the
    # virtualizer reloads the first rows before _collect_all_tracks runs.
    vp = page.viewport_size or {"width": 1280, "height": 800}
    await page.mouse.move(vp["width"] // 2, vp["height"] // 2)
    await page.mouse.wheel(0, -50000)
    await page.wait_for_timeout(1000)
    actual_songs = await _collect_all_tracks(page)
    logger.info("Actual final order (re-scraped from Amazon):")
    for idx, s in enumerate(actual_songs):
        logger.info("  ACTUAL  [%d] %s", idx, s.title)

    return {
        "moved": moved,
        "skipped": skipped,
        "skipped_duplicates": skipped_dup,
        "duplicate_keys": sorted(duplicate_keys),
    }
