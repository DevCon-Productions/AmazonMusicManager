"""Follow-up recon: <music-image-row> on album pages has no primary-* attrs.
Dump its outerHTML (truncated), shadowRoot content, and direct children so we
can find where the track metadata (title, asin) actually lives.

Usage:
    python smoke_album_dom_recon2.py
    python smoke_album_dom_recon2.py --album B0... --track B0...
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
        await bm.page.wait_for_timeout(5000)

        # 1) For each of the 11 music-image-row: outerHTML (truncated)
        print("=== music-image-row outerHTML (each truncated to 800 chars) ===")
        outers = await bm.page.evaluate("""() => {
            const els = document.querySelectorAll('music-image-row');
            return Array.from(els).map(el => el.outerHTML.slice(0, 800));
        }""")
        for i, html in enumerate(outers, 1):
            print(f"\n  [row {i}]")
            print(f"    {html}")
        print()

        # 2) For the FIRST music-image-row: shadowRoot HTML if any + child tag list
        print("=== first music-image-row: shadowRoot + children ===")
        deep = await bm.page.evaluate("""() => {
            const el = document.querySelector('music-image-row');
            if (!el) return null;
            const childTags = Array.from(el.children).map(c => ({
                tag: c.tagName.toLowerCase(),
                attrs: Object.fromEntries(Array.from(c.attributes).map(a => [a.name, a.value])),
                textPreview: (c.textContent || '').trim().slice(0, 100),
            }));
            const shadow = el.shadowRoot ? el.shadowRoot.innerHTML.slice(0, 1500) : null;
            return { childTags, shadow };
        }""")
        if deep:
            print(f"\n  shadowRoot HTML (first 1500 chars):\n    {deep['shadow']!r}\n")
            print("  Children:")
            for c in deep["childTags"]:
                print(f"    <{c['tag']}> attrs={c['attrs']} text={c['textPreview']!r}")
        print()

        # 3) Look for the trackAsin in INNER TEXT (not attrs) anywhere on page
        print(f"=== Any element whose innerText/textContent mentions {args.track} ===")
        matches = await bm.page.evaluate("""(asin) => {
            const out = [];
            document.querySelectorAll('*').forEach(el => {
                const t = el.textContent || '';
                if (t.indexOf(asin) !== -1 && el.children.length === 0) {
                    out.push({tag: el.tagName.toLowerCase(), text: t.slice(0, 200)});
                }
            });
            return out.slice(0, 10);
        }""", args.track)
        if not matches:
            print("  (no leaf elements contain the trackAsin string)")
        for m in matches:
            print(f"  <{m['tag']}> {m['text']!r}")
        print()

        # 4) Title-based: find any element whose text matches the expected title
        TITLE = "Allegro con fuoco"
        print(f"=== Leaf elements whose text contains {TITLE!r} ===")
        title_matches = await bm.page.evaluate("""(needle) => {
            const out = [];
            document.querySelectorAll('*').forEach(el => {
                const t = (el.textContent || '').trim();
                if (t.toLowerCase().includes(needle.toLowerCase()) && el.children.length === 0) {
                    let parent = el.parentElement;
                    const ancestors = [];
                    for (let i = 0; i < 5 && parent; i++) {
                        ancestors.push(parent.tagName.toLowerCase());
                        parent = parent.parentElement;
                    }
                    out.push({tag: el.tagName.toLowerCase(), text: t.slice(0, 120), ancestors});
                }
            });
            return out.slice(0, 10);
        }""", TITLE)
        if not title_matches:
            print("  (no leaf elements contain the title)")
        for m in title_matches:
            print(f"  <{m['tag']}> {m['text']!r} ancestors={m['ancestors']}")
        print()

        # 5) JS properties on first row — maybe asin is exposed as a property
        print("=== JS properties on first music-image-row (not attributes) ===")
        props = await bm.page.evaluate("""() => {
            const el = document.querySelector('music-image-row');
            if (!el) return null;
            const out = {};
            for (const k in el) {
                try {
                    const v = el[k];
                    if (v === null || v === undefined) continue;
                    if (typeof v === 'function') continue;
                    if (typeof v === 'object') continue;
                    out[k] = String(v).slice(0, 150);
                } catch (e) {}
            }
            return out;
        }""")
        if props:
            for k, v in props.items():
                if v and v not in ("0", "false", "true", ""):
                    print(f"  {k} = {v!r}")
        print()

        print("(browser stays open for 8s for visual inspection)")
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
