"""Row-diff + network-capture probe for the web-player row-misroute bug.

Compares row 1 (Szell mvt I, plays correctly) vs row 12 (Berliner Phil mvt IV,
clicks silently misroute to row 1) in the playlist "Classical Dvorak Symphony 9".

User confirmed 2026-05-22: clicking row 12 in the Amazon Music Windows desktop
app plays the Berliner Phil track. So the track is NOT delisted — only the
web player misroutes. Goal of this smoke: find a programmatic signal — in the
DOM or in the click-time network traffic — that distinguishes row 12 from row 1.

What it captures, per row:
  1. Full row attribute / class / computed-style dump (descendant-walking,
     same shape as smoke_compare_rows_9_11.py — extended with row-level
     `href`, `image-src`, `data-key` which the prior smoke flagged as the
     only differing attributes between working and broken rows).
  2. Network requests fired during a 4 s window starting at the click.
     Recorded fields: method, URL, resource type, response status (or pending).
  3. The actually-playing track immediately after the click (from get_now_playing)
     — confirms whether the click resolved correctly or misrouted.

Output: per-row console summary + smoke_compare_dvorak_rows_dump.json with
the full profile (DOM + requests + np) for offline diffing.

Non-destructive — pauses playback after the verification window so the
smoke doesn't leave music playing.
"""
import asyncio
import json
import re
from collections import Counter

from auth import ensure_logged_in
from browser import BrowserManager
from scraper import _PROBE_VISIBLE_INDEX_JS, get_now_playing

PLAYLIST_URL = "https://music.amazon.com/my/playlists/PLAYLIST-ID-HERE"
PLAYLIST_NAME = "Classical Dvorak Symphony 9"
# Row 1 = George Szell, "Symphony No. 9 ... I. Adagio" — plays correctly.
# Row 12 = Berliner Philharmoniker, "Dvořák: Symphony No. 9 ... IV. Allegro con fuoco"
#         from album "Deep Dive — Dvořák" — clicks silently play row 1 in the web
#         player, but play correctly in the Windows desktop app.
TARGETS = [1, 12]

NET_CAPTURE_SECONDS = 4.0
# How many ms of click-fired requests are interesting — keeps the dump small.
RELEVANT_PATH_PATTERNS = re.compile(
    r"(playback|license|manifest|m3u8|mpd|stream|track|catalog|api/play|recommendation)",
    re.IGNORECASE,
)
SKIP_RESOURCE_TYPES = {"image", "font", "stylesheet"}


SCROLL_INTO_VIEW_JS = r"""
    (targetIdx) => {
        const rows = document.querySelectorAll('music-image-row[primary-text]');
        for (const row of rows) {
            const idxEl = row.querySelector('span.index')
                || (row.shadowRoot && row.shadowRoot.querySelector('span.index'));
            if (!idxEl) continue;
            const n = parseInt((idxEl.textContent || '').trim(), 10);
            if (n !== targetIdx) continue;
            row.scrollIntoView({ block: 'center', behavior: 'instant' });
            return true;
        }
        return false;
    }
"""


# Same extraction shape as smoke_compare_rows_9_11.py, lightly extended to
# explicitly surface row-level href / image-src / data-key (which the prior
# smoke noted as the only differing attributes but didn't print up front).
EXTRACT_ROW_PROFILE_JS = r"""
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

            const attrsOf = (el) => {
                const out = {};
                if (!el.attributes) return out;
                for (const a of el.attributes) out[a.name] = a.value;
                return out;
            };
            const csOf = (el, keys) => {
                const cs = getComputedStyle(el);
                const o = {};
                for (const k of keys) o[k] = cs.getPropertyValue(k);
                return o;
            };
            const csKeys = [
                'opacity', 'pointer-events', 'cursor', 'color',
                'filter', 'visibility', 'text-decoration',
            ];

            const rowAttrs = attrsOf(row);
            // Highlight fields the prior smoke noted as the only differences
            // between working and broken rows — surface them at the top level.
            const highlight = {
                href: rowAttrs['href'] || null,
                'image-src': rowAttrs['image-src'] || null,
                'data-key': rowAttrs['data-key'] || null,
                'primary-text': rowAttrs['primary-text'] || null,
                'secondary-text-1': rowAttrs['secondary-text-1'] || null,
            };

            const descendants = [];
            const disabledFlags = [];
            let playbackBtn = null;
            const visited = new Set();
            const walk = (root) => {
                if (!root || visited.has(root)) return;
                visited.add(root);
                const all = root.querySelectorAll ? root.querySelectorAll('*') : [];
                for (const el of all) {
                    const tag = (el.tagName || '').toLowerCase();
                    const cls = ((el.className || '') + '').toString();
                    if (tag === 'music-playback-button' && !playbackBtn) playbackBtn = el;

                    const interestingAttrs = {};
                    if (el.attributes) {
                        for (const a of el.attributes) {
                            const name = a.name.toLowerCase();
                            if (
                                name === 'disabled' || name === 'aria-disabled'
                                || name.startsWith('data-')
                                || name === 'icon-name' || name === 'aria-label'
                                || name === 'role' || name === 'tabindex'
                                || name === 'variant' || name === 'aria-hidden'
                                || name === 'href' || name === 'image-src'
                                || name.includes('unavail')
                                || name.includes('explicit')
                                || name === 'is-loading'
                            ) {
                                interestingAttrs[a.name] = a.value;
                            }
                        }
                    }
                    descendants.push({
                        tag,
                        cls: cls.slice(0, 100),
                        attrs: interestingAttrs,
                    });

                    if (el.attributes) {
                        for (const a of el.attributes) {
                            const name = a.name.toLowerCase();
                            const val = (a.value || '').toLowerCase();
                            if (
                                name === 'disabled'
                                || (name === 'aria-disabled' && val === 'true')
                                || name.includes('unavail')
                                || name === 'data-disabled'
                                || (name === 'aria-label' && /unavail/.test(val))
                            ) {
                                disabledFlags.push({ where: tag, attr: a.name, val: a.value });
                            }
                        }
                    }
                    if (el.shadowRoot) walk(el.shadowRoot);
                }
            };
            walk(row);

            return {
                idx: n,
                highlight,
                rowAttrs,
                rowClass: ((row.className || '') + '').toString(),
                rowComputed: csOf(row, csKeys),
                playbackBtnAttrs: playbackBtn ? attrsOf(playbackBtn) : null,
                playbackBtnComputed: playbackBtn ? csOf(playbackBtn, csKeys) : null,
                disabledFlags,
                descendantCount: descendants.length,
                descendants,
            };
        }
        return null;
    }
"""


CLICK_ROW_JS = r"""
    (targetIdx) => {
        // Scroll into view first to mirror runtime click site.
        const rows = document.querySelectorAll('music-image-row[primary-text]');
        for (const row of rows) {
            const idxEl = row.querySelector('span.index')
                || (row.shadowRoot && row.shadowRoot.querySelector('span.index'));
            if (!idxEl) continue;
            const n = parseInt((idxEl.textContent || '').trim(), 10);
            if (n !== targetIdx) continue;
            // Locate the playback button (deep walk, same as scraper).
            const visited = new Set();
            const find = (root) => {
                if (!root || visited.has(root)) return null;
                visited.add(root);
                const els = root.querySelectorAll ? root.querySelectorAll('music-playback-button') : [];
                if (els.length) return els[0];
                for (const el of (root.querySelectorAll ? root.querySelectorAll('*') : [])) {
                    if (el.shadowRoot) {
                        const found = find(el.shadowRoot);
                        if (found) return found;
                    }
                }
                return null;
            };
            const btn = find(row);
            if (!btn) return { ok: false, reason: 'no playback button' };
            btn.click();
            return { ok: true, primary: row.getAttribute('primary-text'),
                     secondary: row.getAttribute('secondary-text-1') };
        }
        return { ok: false, reason: 'row not in DOM' };
    }
"""


def relevant_request(req_meta):
    """Filter for requests that look like playback resolution — keeps the
    dump small but preserves anything that could carry a track ID."""
    if req_meta["resource_type"] in SKIP_RESOURCE_TYPES:
        return False
    return bool(RELEVANT_PATH_PATTERNS.search(req_meta["url"]))


async def capture_row(page, target_idx, label):
    """Profile + click + network-capture for one row.

    Returns dict with: profile, requests (all + filtered), np_after."""
    print(f"\n=== Row {target_idx} ({label}) ===")

    # Scroll into view and let virtualizer settle.
    ok = await page.evaluate(SCROLL_INTO_VIEW_JS, target_idx)
    if not ok:
        return {"error": f"could not scroll row {target_idx} into DOM"}
    await page.wait_for_timeout(1000)
    probe = await page.evaluate(_PROBE_VISIBLE_INDEX_JS, target_idx)
    if not probe.get("found"):
        return {"error": f"row {target_idx} not in viewport after scroll: {probe}"}

    profile = await page.evaluate(EXTRACT_ROW_PROFILE_JS, target_idx)
    if not profile:
        return {"error": f"profile extraction failed for row {target_idx}"}

    print(f"  primary-text:     {profile['highlight']['primary-text']!r}")
    print(f"  secondary-text-1: {profile['highlight']['secondary-text-1']!r}")
    print(f"  href:             {profile['highlight']['href']!r}")
    print(f"  data-key:         {profile['highlight']['data-key']!r}")
    print(f"  image-src:        {profile['highlight']['image-src']!r}")
    print(f"  descendant count: {profile['descendantCount']}")
    print(f"  disabledFlags:    {profile['disabledFlags']}")

    # Set up request capture. For POST requests we also grab the body —
    # critical for /api/playLibraryPlaylist to see whether the wrong row
    # gets the wrong trackIndex (client bug) or the right index (backend
    # fallback).
    requests_log = []

    def on_request(req):
        body = None
        if req.method == "POST":
            try:
                body = req.post_data
            except Exception:
                body = None
        requests_log.append({
            "phase": "request",
            "method": req.method,
            "url": req.url,
            "resource_type": req.resource_type,
            "post_data": body,
            "status": None,
            "response_body": None,
        })

    async def _maybe_capture_response_body(resp):
        # Only bother grabbing bodies for the small, interesting API calls.
        url = resp.url
        if "/api/playLibraryPlaylist" not in url and "/api/dmls" not in url:
            return None
        try:
            return await resp.text()
        except Exception:
            return None

    def on_response(resp):
        # Match by URL + (no status yet) — same approach as before.
        for entry in reversed(requests_log):
            if entry["url"] == resp.url and entry["status"] is None:
                entry["status"] = resp.status
                # Fire-and-forget body grab; this callback is sync but we
                # schedule the async work on the event loop.
                try:
                    asyncio.get_running_loop().create_task(
                        _attach_response_body(entry, resp)
                    )
                except Exception:
                    pass
                break

    async def _attach_response_body(entry, resp):
        body = await _maybe_capture_response_body(resp)
        if body is not None:
            entry["response_body"] = body

    page.on("request", on_request)
    page.on("response", on_response)
    try:
        print(f"  clicking row {target_idx}...")
        click_result = await page.evaluate(CLICK_ROW_JS, target_idx)
        print(f"  click result: {click_result}")
        if not click_result.get("ok"):
            return {"error": f"click failed: {click_result}", "profile": profile}

        # Capture window — let the request burst fire.
        await page.wait_for_timeout(int(NET_CAPTURE_SECONDS * 1000))
        # Give the scheduled response-body grabs a moment to finish.
        await page.wait_for_timeout(500)
    finally:
        page.remove_listener("request", on_request)
        page.remove_listener("response", on_response)

    # What actually started playing?
    np = await get_now_playing(page)
    print(f"  get_now_playing after click: title={(np or {}).get('title', '')!r}")
    print(f"                                artist={(np or {}).get('artist', '')!r}")

    filtered = [r for r in requests_log if relevant_request(r)]
    print(f"  captured {len(requests_log)} total requests, {len(filtered)} relevant")
    # Print top URL patterns.
    domains = Counter()
    for r in filtered:
        m = re.match(r"https?://([^/]+)", r["url"])
        if m:
            domains[m.group(1)] += 1
    for dom, n in domains.most_common(8):
        print(f"    {dom}: {n} requests")

    # Inline-print the playLibraryPlaylist payload — the central question.
    for r in requests_log:
        if "/api/playLibraryPlaylist" in r["url"]:
            print(f"  >>> playLibraryPlaylist REQUEST body:")
            print(f"      {r.get('post_data')!r}")
            if r.get("response_body"):
                rb = r["response_body"]
                if len(rb) > 600:
                    rb = rb[:600] + "...(truncated)"
                print(f"  >>> playLibraryPlaylist RESPONSE body (truncated):")
                print(f"      {rb}")
            break

    return {
        "target_idx": target_idx,
        "label": label,
        "profile": profile,
        "np_after": np,
        "all_requests": requests_log,
        "relevant_requests": filtered,
    }


_INLINE_PAUSE_JS = r"""
    () => {
        // Deep-walk for a visible play/pause button whose aria-label is
        // currently 'Pause' (i.e., the player is playing). Filter to visible
        // elements — Amazon has hidden DOM twins that no-op when clicked.
        const isVisible = (el) => {
            const r = el.getBoundingClientRect();
            if (r.width === 0 || r.height === 0) return false;
            const cs = getComputedStyle(el);
            return cs.display !== 'none' && cs.visibility !== 'hidden'
                && parseFloat(cs.opacity || '1') !== 0;
        };
        const visited = new Set();
        let hit = null;
        const walk = (root) => {
            if (!root || visited.has(root) || hit) return;
            visited.add(root);
            const all = root.querySelectorAll ? root.querySelectorAll('*') : [];
            for (const el of all) {
                if (hit) return;
                const aria = (el.getAttribute && el.getAttribute('aria-label') || '').toLowerCase();
                if ((aria === 'pause' || aria === 'play') && isVisible(el)) {
                    if (aria === 'pause') { hit = el; return; }
                }
                if (el.shadowRoot) walk(el.shadowRoot);
            }
        };
        walk(document);
        if (hit) { hit.click(); return 'paused'; }
        return 'no-visible-pause';
    }
"""


async def pause_playback(page):
    """Best-effort pause via the player bar so the smoke doesn't leave audio
    playing while you read the diff."""
    try:
        result = await page.evaluate(_INLINE_PAUSE_JS)
        print(f"  pause: {result}")
    except Exception as e:
        print(f"  pause failed: {e}")


async def run():
    async with BrowserManager(headless=False, slow_mo=50) as bm:
        page = bm.page
        await ensure_logged_in(page)
        await page.goto(PLAYLIST_URL, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_selector('music-image-row[primary-text]', timeout=30000)
        await page.wait_for_timeout(2500)

        results = {}
        for idx, label in zip(TARGETS, ["working (Szell)", "broken (Berliner Phil)"]):
            results[idx] = await capture_row(page, idx, label)
            await pause_playback(page)
            await page.wait_for_timeout(800)

        out_path = "smoke_compare_dvorak_rows_dump.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, default=str)
        print(f"\nFull dump written to {out_path}")

        # Quick side-by-side highlight: anything different in the URL set?
        if all(isinstance(results[i], dict) and "relevant_requests" in results[i] for i in TARGETS):
            a, b = TARGETS
            urls_a = {(r["method"], _normalize_url(r["url"])) for r in results[a]["relevant_requests"]}
            urls_b = {(r["method"], _normalize_url(r["url"])) for r in results[b]["relevant_requests"]}
            only_a = urls_a - urls_b
            only_b = urls_b - urls_a
            print(f"\n=== URL set diff (normalized — query strings stripped) ===")
            print(f"Only on row {a} ({len(only_a)}):")
            for m, u in sorted(only_a):
                print(f"  {m} {u}")
            print(f"Only on row {b} ({len(only_b)}):")
            for m, u in sorted(only_b):
                print(f"  {m} {u}")

        await page.wait_for_timeout(2000)


def _normalize_url(url):
    """Strip query strings + numeric IDs so URL-set diff isn't drowned by per-call IDs."""
    base = url.split("?", 1)[0]
    base = re.sub(r"/[A-Z0-9]{10}(?=/|$)", "/<ASIN>", base)
    base = re.sub(r"/\d{4,}", "/<N>", base)
    return base


if __name__ == "__main__":
    asyncio.run(run())
