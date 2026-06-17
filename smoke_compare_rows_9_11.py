"""Row-diff probe — find the DOM signal that marks delisted tracks.

Dumps row 9 (Karajan, available) and row 11 (Bernstein, catalog-delisted) of
Classical Beethoven Symphony 9 with the SAME extraction routine, then prints
what differs. Reports:
  - Attribute presence/absence differences on the music-image-row.
  - Attribute value differences for shared attributes.
  - Class-list differences.
  - music-playback-button attribute differences.
  - Computed style differences (opacity, pointer-events, cursor, color).
  - Any aria-disabled / disabled / data-unavailable-ish flags on any descendant.

Non-destructive — never triggers playback.
"""
import asyncio
import json

from auth import ensure_logged_in
from browser import BrowserManager
from scraper import _PROBE_VISIBLE_INDEX_JS

PLAYLIST_URL = "https://music.amazon.com/my/playlists/PLAYLIST-ID-HERE"
# Updated after the user's delete/re-add manual fix:
#   rows 1-5: "Beethoven: Symphony No. 9..." (working — Karajan)
#   rows 6-10: "Symphony No. 9..." NO "Beethoven:" prefix (suspected delisted)
#   rows 11-15: "Beethoven: Symphony No. 9..." (working — re-added)
TARGETS = [1, 6]  # working vs suspected delisted


# Extract a structured profile of one row. Mirrors row 11's hover-probe
# enumeration but adds: row's own attribute map, class list, computed style,
# music-playback-button's attribute map, and a scan for any "disabled-ish"
# attributes anywhere in the row subtree.
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

            // Walk light + shadow tree once, collecting:
            //   - All descendants' tag + selected attributes
            //   - Any "disabled-ish" attribute on any descendant
            const descendants = [];
            const disabledFlags = [];
            let playbackBtn = null;
            let moreBtn = null;
            let a11yWrapper = null;
            const visited = new Set();
            const walk = (root) => {
                if (!root || visited.has(root)) return;
                visited.add(root);
                const all = root.querySelectorAll ? root.querySelectorAll('*') : [];
                for (const el of all) {
                    const tag = (el.tagName || '').toLowerCase();
                    const role = el.getAttribute && el.getAttribute('role');
                    const cls = ((el.className || '') + '').toString();

                    if (tag === 'music-playback-button' && !playbackBtn) playbackBtn = el;
                    if (tag === 'music-button' && el.getAttribute && el.getAttribute('icon-name') === 'more' && !moreBtn) moreBtn = el;
                    if (tag === 'div' && role === 'button' && /\ba11y\b/.test(cls) && !a11yWrapper) a11yWrapper = el;

                    // Probe interesting attributes — keeps payload small.
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
                                || name.includes('unavail')
                                || name.includes('explicit')
                                || name === 'primary-text-secondary'
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

                    // Flag explicit "unavailable / disabled" markers.
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
                                disabledFlags.push({
                                    where: tag,
                                    attr: a.name,
                                    val: a.value,
                                });
                            }
                        }
                    }

                    if (el.shadowRoot) walk(el.shadowRoot);
                }
            };
            walk(row);

            return {
                idx: n,
                rowAttrs: attrsOf(row),
                rowClass: ((row.className || '') + '').toString(),
                rowComputed: csOf(row, csKeys),
                playbackBtnAttrs: playbackBtn ? attrsOf(playbackBtn) : null,
                playbackBtnClass: playbackBtn ? ((playbackBtn.className || '') + '').toString() : null,
                playbackBtnComputed: playbackBtn ? csOf(playbackBtn, csKeys) : null,
                moreBtnAttrs: moreBtn ? attrsOf(moreBtn) : null,
                a11yWrapperAttrs: a11yWrapper ? attrsOf(a11yWrapper) : null,
                disabledFlags: disabledFlags,
                descendantCount: descendants.length,
                descendants: descendants,
            };
        }
        return null;
    }
"""


SCROLL_INTO_VIEW_JS = r"""
    (targetIdx) => {
        // Find the row whose span.index reads exactly targetIdx, regardless
        // of viewport. Then scrollIntoView({block: 'center'}). This bypasses
        // wheel-scroll quirks (jump-by-1500px overshoots etc.).
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


async def scroll_row_into_view(page, target_idx_1based, max_scrolls=80):
    """First try scrollIntoView (instant). If the row isn't in DOM, fall back
    to wheel-scroll. Most playlists fit fully in DOM so scrollIntoView wins."""
    ok = await page.evaluate(SCROLL_INTO_VIEW_JS, target_idx_1based)
    if ok:
        await page.wait_for_timeout(800)
        # Verify it's now in viewport.
        info = await page.evaluate(_PROBE_VISIBLE_INDEX_JS, target_idx_1based)
        if info["found"]:
            return True
        print(f"    scrollIntoView ran but row {target_idx_1based} still not in viewport: {info}")

    # Fallback: wheel-scroll loop (kept for completeness — usually scrollIntoView wins).
    last_v_max = -1
    stuck_count = 0
    for attempt in range(max_scrolls):
        info = await page.evaluate(_PROBE_VISIBLE_INDEX_JS, target_idx_1based)
        if attempt < 5 or attempt % 10 == 0:
            print(f"    scroll attempt {attempt}: found={info['found']} visMin={info['visMin']} visMax={info['visMax']}")
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
                    print(f"    bailed: stuck at v_max={v_max}")
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
    print(f"    bailed: exhausted {max_scrolls} attempts")
    return False


def diff_dicts(label, da, db):
    """Return a list of human-readable diff lines for two flat dicts. Skips
    'primary-text' / 'secondary-text-*' / 'aria-label' since those naturally
    differ by content."""
    if da is None and db is None:
        return [f"  {label}: both None"]
    if da is None:
        return [f"  {label}: row 9 has NO {label}, row 11 has one with attrs={list(db.keys())}"]
    if db is None:
        return [f"  {label}: row 9 has {label}, row 11 does NOT"]
    keys_a = set(da.keys())
    keys_b = set(db.keys())
    skip = {"primary-text", "secondary-text-1", "secondary-text-2", "aria-label", "id"}
    lines = []
    only_a = (keys_a - keys_b) - skip
    only_b = (keys_b - keys_a) - skip
    common = (keys_a & keys_b) - skip
    for k in sorted(only_a):
        lines.append(f"    [{label}] only on row 9: {k}={da[k]!r}")
    for k in sorted(only_b):
        lines.append(f"    [{label}] only on row 11: {k}={db[k]!r}")
    for k in sorted(common):
        if da[k] != db[k]:
            lines.append(f"    [{label}] differs at {k}: 9={da[k]!r}  11={db[k]!r}")
    if not lines:
        lines.append(f"  {label}: no relevant differences")
    return lines


def diff_descendants(da_list, db_list):
    """Group descendants by tag, compare counts + per-tag attribute sets."""
    from collections import defaultdict
    bucket = lambda lst: defaultdict(list)
    a_by_tag = defaultdict(list)
    b_by_tag = defaultdict(list)
    for d in da_list:
        a_by_tag[d["tag"]].append(d)
    for d in db_list:
        b_by_tag[d["tag"]].append(d)
    lines = []
    all_tags = set(a_by_tag.keys()) | set(b_by_tag.keys())
    for tag in sorted(all_tags):
        ca = len(a_by_tag[tag])
        cb = len(b_by_tag[tag])
        if ca != cb:
            lines.append(f"    tag={tag}: count 9={ca}, 11={cb}")
            continue
        # Same count — compare attribute KEY sets (order may differ but presence shouldn't).
        a_attr_sets = [frozenset(d["attrs"].keys()) for d in a_by_tag[tag]]
        b_attr_sets = [frozenset(d["attrs"].keys()) for d in b_by_tag[tag]]
        # Sort both lists to ignore positional differences.
        if sorted(a_attr_sets) != sorted(b_attr_sets):
            lines.append(f"    tag={tag}: attribute-key sets differ")
            for i, (sa, sb) in enumerate(zip(sorted(a_attr_sets), sorted(b_attr_sets))):
                if sa != sb:
                    lines.append(f"      occurrence {i}: 9={sorted(sa)}  11={sorted(sb)}")
        # Compare class strings too — same count, ordered-by-original-position.
        a_classes = [d["cls"] for d in a_by_tag[tag]]
        b_classes = [d["cls"] for d in b_by_tag[tag]]
        for i, (ca_cls, cb_cls) in enumerate(zip(a_classes, b_classes)):
            if ca_cls != cb_cls:
                lines.append(f"    tag={tag} occurrence {i}: class differs")
                lines.append(f"      9={ca_cls!r}")
                lines.append(f"      11={cb_cls!r}")
    return lines


async def run():
    async with BrowserManager(headless=False, slow_mo=100) as bm:
        await ensure_logged_in(bm.page)
        await bm.page.goto(PLAYLIST_URL, wait_until="domcontentloaded", timeout=30000)
        await bm.page.wait_for_selector('music-image-row[primary-text]', timeout=30000)
        await bm.page.wait_for_timeout(2500)

        profiles = {}
        for idx in TARGETS:
            print(f"\n=== Scrolling row {idx} into view ===")
            if not await scroll_row_into_view(bm.page, idx):
                print(f"Could not reach row {idx}.")
                return
            await bm.page.wait_for_timeout(800)
            p = await bm.page.evaluate(EXTRACT_ROW_PROFILE_JS, idx)
            if not p:
                print(f"Profile extraction failed for row {idx}.")
                return
            print(f"  row {idx}: primary-text={p['rowAttrs'].get('primary-text', '')[:70]!r}")
            print(f"  row {idx}: secondary-text-1={p['rowAttrs'].get('secondary-text-1', '')[:60]!r}")
            print(f"  row {idx}: descendant count = {p['descendantCount']}")
            print(f"  row {idx}: disabledFlags = {p['disabledFlags']}")
            profiles[idx] = p

        a_idx, b_idx = TARGETS[0], TARGETS[1]
        pa = profiles[a_idx]
        pb = profiles[b_idx]
        print(f"\n\n========== DIFF (row {a_idx} vs row {b_idx}) ==========")

        print(f"\n--- music-image-row attributes ---")
        for ln in diff_dicts("rowAttrs", pa["rowAttrs"], pb["rowAttrs"]):
            print(ln)

        print(f"\n--- music-image-row class string ---")
        if pa["rowClass"] != pb["rowClass"]:
            print(f"  row {a_idx}:  {pa['rowClass']!r}")
            print(f"  row {b_idx}:  {pb['rowClass']!r}")
        else:
            print(f"  identical: {pa['rowClass']!r}")

        print(f"\n--- music-image-row computed style ---")
        for ln in diff_dicts("rowComputed", pa["rowComputed"], pb["rowComputed"]):
            print(ln)

        print(f"\n--- music-playback-button attributes ---")
        for ln in diff_dicts("playbackBtnAttrs", pa["playbackBtnAttrs"], pb["playbackBtnAttrs"]):
            print(ln)

        print(f"\n--- music-playback-button class string ---")
        if pa["playbackBtnClass"] != pb["playbackBtnClass"]:
            print(f"  row {a_idx}:  {pa['playbackBtnClass']!r}")
            print(f"  row {b_idx}:  {pb['playbackBtnClass']!r}")
        else:
            print(f"  identical: {pa['playbackBtnClass']!r}")

        print(f"\n--- music-playback-button computed style ---")
        for ln in diff_dicts("playbackBtnComputed", pa["playbackBtnComputed"], pb["playbackBtnComputed"]):
            print(ln)

        print(f"\n--- more (...) button attributes ---")
        for ln in diff_dicts("moreBtnAttrs", pa["moreBtnAttrs"], pb["moreBtnAttrs"]):
            print(ln)

        print(f"\n--- div[role=button].a11y attributes ---")
        for ln in diff_dicts("a11yWrapperAttrs", pa["a11yWrapperAttrs"], pb["a11yWrapperAttrs"]):
            print(ln)

        print(f"\n--- disabled / unavailable flags ---")
        print(f"  row {a_idx}:  {pa['disabledFlags']}")
        print(f"  row {b_idx}:  {pb['disabledFlags']}")

        print(f"\n--- descendant structure ---")
        desc_lines = diff_descendants(pa["descendants"], pb["descendants"])
        if desc_lines:
            for ln in desc_lines:
                print(ln)
        else:
            print("  identical structure (same tags, same class strings, same attribute keys)")

        # Save full profiles for offline inspection.
        with open("smoke_compare_rows_9_11_dump.json", "w", encoding="utf-8") as f:
            json.dump(profiles, f, indent=2)
        print("\nFull profiles saved to smoke_compare_rows_9_11_dump.json")
        await bm.page.wait_for_timeout(3000)


if __name__ == "__main__":
    asyncio.run(run())
