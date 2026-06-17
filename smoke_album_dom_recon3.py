"""Recon #3: dump first 15 <music-text-row> elements on the album page —
their outerHTML + parent ancestry + primary-href + any data-* attrs.
Goal: identify how to (a) scope to the album's own tracklist section vs.
the "more by artist" rows, and (b) locate a specific row by title.

Usage:
    python smoke_album_dom_recon3.py
"""

from __future__ import annotations

import argparse
import asyncio
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
        # Wait until at least one music-text-row has real text (i.e., not the
        # "Tuning in" loading state). Poll for up to 45s.
        print("waiting for tracklist to populate...")
        for attempt in range(45):
            ready = await bm.page.evaluate("""() => {
                const rows = document.querySelectorAll('music-text-row');
                let withText = 0;
                rows.forEach(r => { if ((r.textContent || '').trim().length > 5) withText++; });
                return {total: rows.length, withText};
            }""")
            print(f"  t+{attempt}s: total={ready['total']} withText={ready['withText']}")
            if ready["withText"] >= 5:
                break
            await bm.page.wait_for_timeout(1000)
        await bm.page.wait_for_timeout(2000)

        # Dump first 15 music-text-row: attrs + outerHTML + parent ancestry
        print("=== First 15 <music-text-row> ===\n")
        rows = await bm.page.evaluate("""() => {
            const els = document.querySelectorAll('music-text-row');
            const out = [];
            for (let i = 0; i < Math.min(15, els.length); i++) {
                const el = els[i];
                const attrs = {};
                for (const a of el.attributes) attrs[a.name] = a.value;
                let p = el.parentElement;
                const ancestors = [];
                for (let j = 0; j < 8 && p; j++) {
                    const pa = {};
                    for (const a of p.attributes) pa[a.name] = a.value;
                    ancestors.push({tag: p.tagName.toLowerCase(), attrs: pa});
                    p = p.parentElement;
                }
                out.push({
                    index: i,
                    attrs,
                    text: (el.textContent || '').trim().slice(0, 200),
                    outerHTML: el.outerHTML.slice(0, 600),
                    ancestors,
                });
            }
            return out;
        }""")
        for r in rows:
            print(f"\n  [row {r['index']}] text={r['text']!r}")
            print(f"    attrs={r['attrs']}")
            print(f"    outerHTML (600 chars): {r['outerHTML']}")
            print("    ancestors (closest-first):")
            for j, a in enumerate(r["ancestors"]):
                # Only show interesting attrs
                interesting = {k: v for k, v in a["attrs"].items()
                               if k in ("class", "id", "header", "label", "kind") or "section" in k}
                print(f"      [{j}] <{a['tag']}> {interesting}")
        print()

        # How many rows are *visible* (in viewport / not below "more by artist")?
        # Look for any element with class like 'tracklist' or 'track-list' or 'section header'
        print("=== Section headers visible on the page ===")
        headers = await bm.page.evaluate("""() => {
            const headers = [];
            document.querySelectorAll('h1,h2,h3,h4,music-detail-header,[header]').forEach(el => {
                const t = (el.textContent || '').trim();
                if (t.length > 0 && t.length < 200) headers.push({tag: el.tagName.toLowerCase(), text: t.slice(0, 100)});
            });
            return headers.slice(0, 30);
        }""")
        for h in headers:
            print(f"  <{h['tag']}> {h['text']!r}")
        print()

        # For our specific target title, find the row and its INDEX in the music-text-row list
        TARGET = 'Dvořák: Symphony No. 9 in E Minor, Op. 95, B. 178, "From the New World": IV. Allegro con fuoco'
        print(f"=== Locate target row: {TARGET!r} ===")
        loc = await bm.page.evaluate("""(needle) => {
            const all = Array.from(document.querySelectorAll('music-text-row'));
            const hits = [];
            for (let i = 0; i < all.length; i++) {
                const t = (all[i].textContent || '').trim();
                if (t.includes(needle)) {
                    const r = all[i].getBoundingClientRect();
                    hits.push({
                        index: i,
                        text: t.slice(0, 200),
                        rect: {x: r.x, y: r.y, w: r.width, h: r.height},
                        primaryHref: all[i].getAttribute('primary-href'),
                        allAttrs: Object.fromEntries(Array.from(all[i].attributes).map(a => [a.name, a.value])),
                    });
                }
            }
            return hits;
        }""", TARGET)
        for h in loc:
            print(f"  index={h['index']}  rect={h['rect']}")
            print(f"    primary-href = {h['primaryHref']!r}")
            print(f"    all attrs   = {h['allAttrs']}")
            print(f"    text         = {h['text']!r}")
        print()

        print("(browser stays open for 8s)")
        await bm.page.wait_for_timeout(8000)
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--album", default="B09S6SS3F5")
    p.add_argument("--track", default="B09S6Q9S9M")
    args = p.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
