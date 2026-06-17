"""Sniff the Amazon Music "add track to playlist" API call.

Build step 2 of the Classical Shuffle playlist generator (see the project
memory): we want an ASIN-direct `add_track_by_asin` helper, but we don't yet
know how Amazon's web client persists an add-to-playlist.

Findings so far: adding a track via the 3-dot menu SUCCEEDS (tracks appear in
the playlist), yet no obvious add endpoint fires — the only cirrus calls are
`reportClientActions`. That strongly implies the mutation is either carried
INSIDE a reportClientActions body (cirrus "client actions" = library
mutations) or sent over a WebSocket. Earlier runs filtered reportClientActions
as telemetry, so its body was never captured.

So this version records EVERYTHING: every non-GET amazon request body in full,
plus all WebSocket frames. It's open-ended (press Enter to stop). On stop it
scans every captured payload for add/playlist tokens and prints the matches
with surrounding context, so the hidden mutation surfaces.

Usage:
    python smoke_sniff_add_track.py [SEARCH_TERM ...]

Pass optional SEARCH_TERMs (e.g. a track ASIN, or part of the target playlist
name/id) to scan for them specifically — the most reliable way to pin the add.

In the Chrome window:
    1. Hover a track row -> 3-dot (...) menu -> 'Add to playlist' -> pick one.
    2. Verify the track appears in that playlist.
    3. Press ENTER in this terminal to stop and analyse.
"""
import asyncio
import json
import os
import sys

from auth import ensure_logged_in
from browser import BrowserManager
from config import BASE_URL
from utils import logger

OUTPUT_PATH = os.path.join("output", "sniff_add_track.json")
MAX_BODY_CHARS = 40_000  # truncate huge bodies (dmls manifests etc.)

# Operations we know are pure telemetry/playback — shown as terse '·' lines so
# the console stays readable, but their bodies ARE still captured to the JSON
# (the add may be hiding inside reportClientActions).
TELEMETRY_TARGETS = (
    "reportclientactions",
    "getdashmanifests",
    "getlicenseforplayback",
    "getmetrics",
)

# Tokens that would appear in an add-to-playlist mutation payload. Used only for
# the post-capture scan / live highlighting — nothing is filtered out.
SEARCH_TOKENS = (
    "addtrack", "addtoplaylist", "add_to_playlist", "addtracks",
    "appendtrack", "appendtracks", "appendobjects", "insertobjects",
    "createplaylist", "editplaylist", "updateplaylist", "privateplaylist",
    "playlisttrack", "addobjecttoplaylist",
)


def _is_telemetry_target(target: str | None) -> bool:
    t = (target or "").lower()
    return any(h in t for h in TELEMETRY_TARGETS)


def _truncate(s, n=MAX_BODY_CHARS):
    if isinstance(s, (bytes, bytearray)):
        try:
            s = s.decode("utf-8", "replace")
        except Exception:
            s = repr(s)
    if s and len(s) > n:
        return s[:n] + "...<truncated>"
    return s


def _scan_for_tokens(text: str, extra_terms) -> list[str]:
    """Return which tokens/terms appear in *text* (case-insensitive)."""
    if not text:
        return []
    low = text.lower()
    hits = [t for t in SEARCH_TOKENS if t in low]
    hits += [t for t in extra_terms if t and t.lower() in low]
    return hits


async def run():
    extra_terms = [a for a in sys.argv[1:] if a.strip()]

    requests_log: list[dict] = []   # every non-GET amazon request, full
    ws_frames: list[dict] = []      # websocket frames (sent + received)
    seen = 0

    async with BrowserManager(headless=False, slow_mo=0) as bm:
        page = bm.page
        await ensure_logged_in(page)
        loop = asyncio.get_running_loop()

        # ---- WebSocket capture -------------------------------------------
        def on_websocket(ws):
            print(f"  [ws] open: {ws.url}")

            def record(direction, payload):
                text = _truncate(payload, 8000)
                hits = _scan_for_tokens(text if isinstance(text, str) else "",
                                        extra_terms)
                ws_frames.append({"dir": direction, "url": ws.url,
                                  "payload": text, "hits": hits})
                if hits:
                    print(f"  [ws {direction}] *** token hit {hits} ***")

            ws.on("framesent", lambda p: record("sent", p))
            ws.on("framereceived", lambda p: record("recv", p))

        page.on("websocket", on_websocket)

        # ---- HTTP response capture ---------------------------------------
        async def handle_response(resp):
            nonlocal seen
            try:
                req = resp.request
                if req.method == "GET":
                    return
                url = resp.url
                if "amazon" not in url:
                    return
                seen += 1
                post_data = req.post_data
                try:
                    req_headers = await req.all_headers()
                except Exception:
                    req_headers = dict(req.headers)
                target = req_headers.get("x-amz-target")
                try:
                    body_text = _truncate(await resp.text())
                except Exception as e:
                    body_text = f"<no body: {e}>"

                hits = _scan_for_tokens((post_data or "") + " " + (url or ""),
                                        extra_terms)
                entry = {
                    "method": req.method, "url": url, "status": resp.status,
                    "x_amz_target": target, "request_headers": req_headers,
                    "post_data": post_data, "response_body": body_text,
                    "hits": hits,
                }
                requests_log.append(entry)

                op = target or url.replace(BASE_URL, "")
                if hits:
                    print(f"  [HIT] {req.method} {op} -> {resp.status}  "
                          f"tokens={hits}")
                elif _is_telemetry_target(target):
                    print(f"  ·  {op}")
                else:
                    print(f"  [captured] {req.method} {op} -> {resp.status}")
            except Exception as e:
                logger.warning("sniff handler error: %s", e)

        page.on("response", lambda r: loop.create_task(handle_response(r)))

        # Fresh load so the WebSocket handshake happens under our listener.
        await page.goto(f"{BASE_URL}/my/playlists/all",
                        wait_until="domcontentloaded", timeout=30000)

        print("\n" + "=" * 64)
        print("  FULL-CAPTURE SNIFFER ARMED — no time limit")
        print("=" * 64)
        if extra_terms:
            print(f"  Also scanning for: {extra_terms}")
        print("  Recording ALL request bodies + WebSocket frames.")
        print()
        print("  1. Hover a track row -> 3-dot (...) menu -> 'Add to playlist'")
        print("     -> pick a playlist. Verify it lands in the playlist.")
        print("  2. A [HIT] line means a payload matched an add/playlist token.")
        print("  3. Press ENTER here when done to stop and analyse.")
        print("=" * 64 + "\n")

        await asyncio.to_thread(input)

    os.makedirs("output", exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump({"requests": requests_log, "ws_frames": ws_frames,
                   "search_terms": extra_terms}, f, indent=2, ensure_ascii=False)

    print("\n" + "=" * 64)
    print(f"  Done. {len(requests_log)} non-GET request(s), "
          f"{len(ws_frames)} WebSocket frame(s) captured.")
    print(f"  Full dump: {OUTPUT_PATH}")
    print("=" * 64)

    # ---- Analysis: surface payloads that matched add/playlist tokens -----
    req_hits = [e for e in requests_log if e["hits"]]
    ws_hits = [w for w in ws_frames if w["hits"]]

    if not req_hits and not ws_hits:
        print("\n  No token matched. The mutation didn't carry any expected")
        print("  add/playlist token. Inspect the JSON: every reportClientActions")
        print("  body is now captured — look at requests[].post_data for the one")
        print("  whose clientActionList describes the add. Re-run with the track")
        print("  ASIN as an argument to scan for it directly:")
        print("      python smoke_sniff_add_track.py B09S6Q9S9M")
        # Still show distinct cirrus operations + their action types as a hint.
        print("\n  Distinct cirrus operations captured:")
        ops = {}
        for e in requests_log:
            t = e["x_amz_target"]
            if t and "cirrus" in t.lower():
                ops[t] = ops.get(t, 0) + 1
        for t, n in ops.items():
            print(f"    {n:3}x  {t}")
        return

    print("\n  >>> PAYLOADS MATCHING add/playlist TOKENS <<<\n")
    for e in req_hits:
        print(f"  HTTP {e['method']} {e['url']}  -> {e['status']}")
        print(f"       x-amz-target: {e['x_amz_target']}")
        print(f"       tokens: {e['hits']}")
        print(f"       post_data: {_truncate(e['post_data'], 1200)}")
        print()
    for w in ws_hits:
        print(f"  WS {w['dir']} {w['url']}")
        print(f"       tokens: {w['hits']}")
        print(f"       payload: {_truncate(w['payload'], 1200)}")
        print()


if __name__ == "__main__":
    asyncio.run(run())
