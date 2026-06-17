"""Quick recon: navigate to the album deep-link and dump the actual row-like
elements so we can fix add_track_by_asin's selector.

Lists every custom element tag-name on the page with a count, then dumps
the attributes of the first 3 "row-looking" elements (anything with
primary-text/title/href hinting at being a track). This tells us what
selector to target on album pages.

Usage:
    python smoke_album_dom_recon.py
    python smoke_album_dom_recon.py --album B0... --track B0...
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

from auth import ensure_logged_in
from browser import BrowserManager
from config import BASE_URL, LONG_TIMEOUT
from utils import setup_logging


async def _run(args: argparse.Namespace) -> int:
    setup_logging()
    url = f"{BASE_URL}/albums/{args.album}?trackAsin={args.track}"
    print(f"recon URL: {url}\n")

    async with BrowserManager(headless=False, slow_mo=100) as bm:
        await ensure_logged_in(bm.page)
        await bm.page.goto(url, wait_until="domcontentloaded", timeout=LONG_TIMEOUT)
        # Give the page time to lazy-load tracks
        await bm.page.wait_for_timeout(5000)

        # 1) Count every custom element (tag containing a dash)
        tag_counts = await bm.page.evaluate("""() => {
            const counts = {};
            document.querySelectorAll('*').forEach(el => {
                const tag = el.tagName.toLowerCase();
                if (tag.includes('-')) {
                    counts[tag] = (counts[tag] || 0) + 1;
                }
            });
            return counts;
        }""")
        print("=== Custom-element tag counts ===")
        for tag, n in sorted(tag_counts.items(), key=lambda x: -x[1]):
            print(f"  {n:4d}  {tag}")
        print()

        # 2) Dump first 5 of each potentially-row tag
        row_candidates = [
            "music-image-row",
            "music-track-row",
            "music-album-row",
            "music-list-item",
            "music-horizontal-item",
            "music-row",
            "music-detail-row",
        ]
        for tag in row_candidates:
            n = tag_counts.get(tag, 0)
            if n == 0:
                continue
            print(f"=== first 5 of <{tag}> (total: {n}) ===")
            attrs = await bm.page.evaluate(f"""() => {{
                const els = document.querySelectorAll('{tag}');
                const out = [];
                for (let i = 0; i < Math.min(5, els.length); i++) {{
                    const el = els[i];
                    const a = {{}};
                    for (const attr of el.attributes) {{
                        a[attr.name] = attr.value;
                    }}
                    out.push(a);
                }}
                return out;
            }}""")
            for i, a in enumerate(attrs, 1):
                print(f"  [{i}]")
                for k, v in a.items():
                    # Truncate giant values
                    vs = v if len(v) < 200 else v[:200] + "..."
                    print(f"      {k} = {vs!r}")
            print()

        # 3) Any element whose attributes mention our trackAsin
        print(f"=== Elements whose attributes mention trackAsin {args.track} ===")
        matches = await bm.page.evaluate("""(trackAsin) => {
            const results = [];
            document.querySelectorAll('*').forEach(el => {
                for (const attr of el.attributes) {
                    if (attr.value.indexOf(trackAsin) !== -1) {
                        results.push({
                            tag: el.tagName.toLowerCase(),
                            attr: attr.name,
                            value: attr.value.length > 200 ? attr.value.slice(0, 200) + '...' : attr.value,
                        });
                        break;
                    }
                }
            });
            return results.slice(0, 20);
        }""", args.track)
        for m in matches:
            print(f"  <{m['tag']}> {m['attr']}={m['value']!r}")
        print()
        print("(browser stays open for 10s for visual inspection)")
        await bm.page.wait_for_timeout(10000)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="DOM recon for the album deep-link page.")
    p.add_argument("--album", default="B09S6SS3F5")
    p.add_argument("--track", default="B09S6Q9S9M")
    args = p.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
