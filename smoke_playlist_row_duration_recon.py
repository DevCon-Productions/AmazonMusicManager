"""Recon: where does duration (e.g. "11:52") live inside <music-image-row>
on a playlist page? scraper.py reads primary-text/secondary-text-1/-2/
primary-href from each row but NOT duration, and 0/39 cached xlsx have any
duration data as a result. This script dumps:

  1. Every attribute on the first 5 <music-image-row> elements
  2. Their full outerHTML (truncated)
  3. Any leaf element whose textContent matches /^\\d{1,2}:\\d{2}$/

so we can pick a robust selector/attribute for the scraper.

Default playlist: the Dvorak Symphony 9 playlist (already used as our
Step 2.5/3 reference). Pass --url to override.

Usage:
    python smoke_playlist_row_duration_recon.py
    python smoke_playlist_row_duration_recon.py --url 'https://music.amazon.com/my/playlists/...'
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
from config import LONG_TIMEOUT
from utils import setup_logging


# Dvorak Sym 9 playlist (where we have ground truth from earlier recon —
# Berliner Phil mvt IV = 11:52). Read from org tree at runtime would be
# ideal, but hard-coding it keeps this script standalone.
DEFAULT_URL = (
    "https://music.amazon.com/my/playlists/"
    "bb9577f7-acb3-4b5b-a1c7-9760f8f01a99"  # Classical Dvorak Symphony 9
)


async def _run(args: argparse.Namespace) -> int:
    setup_logging()
    print(f"recon URL: {args.url}\n")

    async with BrowserManager(headless=False, slow_mo=100) as bm:
        await ensure_logged_in(bm.page)
        await bm.page.goto(args.url, wait_until="domcontentloaded", timeout=LONG_TIMEOUT)
        # Wait for tracklist
        print("waiting for tracklist...")
        for attempt in range(45):
            n = await bm.page.evaluate("""() => {
                return document.querySelectorAll('music-image-row[primary-text]').length;
            }""")
            print(f"  t+{attempt}s: rows={n}")
            if n >= 5:
                break
            await bm.page.wait_for_timeout(1000)
        await bm.page.wait_for_timeout(2000)

        # 1) First 5 rows: attrs + outerHTML
        print("\n=== first 5 <music-image-row> attrs + outerHTML ===")
        info = await bm.page.evaluate("""() => {
            const rows = document.querySelectorAll('music-image-row[primary-text]');
            const out = [];
            for (let i = 0; i < Math.min(5, rows.length); i++) {
                const r = rows[i];
                const attrs = {};
                for (const a of r.attributes) attrs[a.name] = a.value;
                out.push({
                    index: i,
                    attrs,
                    outerHTML: r.outerHTML.slice(0, 1200),
                });
            }
            return out;
        }""")
        for r in info:
            print(f"\n  [row {r['index']}]")
            print(f"    attrs={r['attrs']}")
            print(f"    outerHTML (1200 chars): {r['outerHTML']}")

        # 2) Find leaf elements with text matching MM:SS pattern, group by
        # their ancestor music-image-row
        print("\n=== leaf elements with /^\\d{1,2}:\\d{2}$/ text ===")
        matches = await bm.page.evaluate("""() => {
            const out = [];
            document.querySelectorAll('*').forEach(el => {
                if (el.children.length !== 0) return;
                const t = (el.textContent || '').trim();
                if (!/^\\d{1,2}:\\d{2}$/.test(t)) return;
                // Walk up to the row
                let row = el.parentElement;
                let depth = 0;
                while (row && row.tagName.toLowerCase() !== 'music-image-row' && depth < 15) {
                    row = row.parentElement;
                    depth++;
                }
                out.push({
                    text: t,
                    tag: el.tagName.toLowerCase(),
                    cls: el.className || '',
                    parentCls: el.parentElement ? el.parentElement.className : '',
                    rowFound: !!row,
                    rowTitle: row ? row.getAttribute('primary-text') : null,
                });
            });
            return out.slice(0, 15);
        }""")
        if not matches:
            print("  (no MM:SS leaf elements found)")
        for m in matches:
            print(f"  text={m['text']!r} tag=<{m['tag']}> class={m['cls']!r} "
                  f"parentCls={m['parentCls']!r} rowFound={m['rowFound']} "
                  f"rowTitle={m['rowTitle']!r}")

        # 3) Dump shadowRoot of first row if any
        print("\n=== shadowRoot of first row ===")
        shadow = await bm.page.evaluate("""() => {
            const r = document.querySelector('music-image-row[primary-text]');
            if (!r) return null;
            return r.shadowRoot ? r.shadowRoot.innerHTML.slice(0, 2000) : '(no shadowRoot)';
        }""")
        print(f"  {shadow}")

        print("\n(browser stays open for 8s)")
        await bm.page.wait_for_timeout(8000)
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default=DEFAULT_URL)
    args = p.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
