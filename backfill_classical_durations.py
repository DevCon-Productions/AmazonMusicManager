"""One-time backfill: re-scrape Classical playlists so their cached xlsx files
gain Duration values in column G (Step 4.5 of the Classical Shuffle playlist
generator — see project_classical_shuffle_playlist_gen_plan).

Why a script (not `cmd_export` in a loop):
  * Reuses one browser session for the whole batch (auth once, no relaunch).
  * Idempotent: skips playlists whose xlsx already has non-empty Duration
    cells, so it's safe to re-run after an interrupted batch.
  * Skips playlists with no cached xlsx (those should be exported first via
    the normal flow — the script just reports them).

Mirrors `backfill_classical_asins.py` (the Step 2.5 backfill that ran
2026-06-02). The only difference is the "needs backfill" check: ASIN
backfill checks for non-empty Track ASIN values; this one checks for
non-empty Duration values. Both predicates are stricter than "header
exists" because a partial scrape can leave the column header in place
with all-empty cells.

Usage:
    python backfill_classical_durations.py                # backfill everything under Classical/
    python backfill_classical_durations.py --dry-run      # show plan, do nothing
    python backfill_classical_durations.py --limit 3      # backfill only first 3 (smoke)
    python backfill_classical_durations.py --scope Classical/Beethoven

After Step 4.5 ships and all Classical playlists are backfilled, this script
can be deleted (or kept for future column-additions, like its ASIN sibling).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Iterable

# Windows consoles default to cp1252, which can't print arrows or composer
# names with diacritics. Reconfigure once at import time.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

from openpyxl import load_workbook

from playwright.async_api import Error as PlaywrightError

from auth import ensure_logged_in
from browser import BrowserManager
from exporter import export_to_xlsx
from organization import load_org, iter_playlists_recursive
from scraper import scrape_playlist
from spreadsheet_tags import get_source
from utils import logger, setup_logging


class _BrowserDied(Exception):
    """Raised when the browser/page closed mid-scrape so the loop bails out
    instead of cascading TargetClosedError through every remaining playlist."""


# Windows SetThreadExecutionState flags — keep the system awake (no sleep, no
# display-off) while the backfill runs. On non-Windows this is a no-op.
def _keep_awake(enable: bool) -> None:
    """Block (enable=True) or allow (enable=False) Windows idle sleep.

    Backfill takes ~30 min unattended; if the system sleeps mid-run, Chrome
    Remote Debugging detaches and every remaining scrape fails with
    TargetClosedError. Bracketing the run with this prevents that.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ES_CONTINUOUS = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        ES_DISPLAY_REQUIRED = 0x00000002
        if enable:
            flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
        else:
            flags = ES_CONTINUOUS  # clear the request
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
    except Exception:
        # Best-effort; if SetThreadExecutionState isn't available, just don't.
        pass


_OUTPUT_DIR = "./output"


def _build_url_to_xlsx_index(output_dir: str = _OUTPUT_DIR) -> dict[str, str]:
    """Scan *output_dir* for .xlsx files, return {source_url: xlsx_path}.

    Inline copy so this script doesn't pull in PyQt6 from gui.py.
    """
    index: dict[str, str] = {}
    if not os.path.isdir(output_dir):
        return index
    for dirpath, _dirs, files in os.walk(output_dir):
        for fn in files:
            if not fn.lower().endswith(".xlsx") or fn.startswith("~$"):
                continue
            path = os.path.join(dirpath, fn)
            try:
                src = get_source(path)
            except Exception:
                continue
            url = (src.get("url") or "").strip()
            if not url:
                continue
            index.setdefault(url, path)
    return index


def _xlsx_already_has_durations(path: str) -> bool:
    """Return True if *path*'s Duration column has at least one non-empty
    value among its data rows.

    A file with the header but all-empty cells is treated as needing
    backfill — that's exactly the state every Classical xlsx is in right
    now (column reserved, never populated).
    """
    try:
        wb = load_workbook(path, read_only=True)
        try:
            ws = wb.active
            headers = [
                str(c.value).strip().lower() if c.value else ""
                for c in next(ws.iter_rows(min_row=1, max_row=1))
            ]
            try:
                dur_idx = headers.index("duration")
            except ValueError:
                return False
            for row in ws.iter_rows(min_row=2, values_only=True):
                if dur_idx < len(row):
                    v = row[dur_idx]
                    if v not in (None, ""):
                        return True
            return False
        finally:
            wb.close()
    except Exception:
        return False


def _dedupe_by_url(playlists: Iterable[dict]) -> list[dict]:
    """Return playlist items deduped by url (first-seen wins). The same
    playlist may be filed in multiple folders — we only want to re-scrape
    it once."""
    seen: set[str] = set()
    out: list[dict] = []
    for pl in playlists:
        url = (pl.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(pl)
    return out


def _classify(playlists: list[dict], url_to_xlsx: dict[str, str]) -> dict:
    """Sort playlists into buckets: needs_backfill / already_has / no_xlsx."""
    needs: list[tuple[dict, str]] = []  # (playlist, xlsx_path)
    already: list[tuple[dict, str]] = []
    missing: list[dict] = []
    for pl in playlists:
        url = pl["url"]
        xlsx = url_to_xlsx.get(url)
        if not xlsx:
            missing.append(pl)
            continue
        if _xlsx_already_has_durations(xlsx):
            already.append((pl, xlsx))
        else:
            needs.append((pl, xlsx))
    return {"needs": needs, "already": already, "missing": missing}


async def _backfill_one(page, pl: dict, xlsx_path: str) -> bool:
    """Scrape *pl* and overwrite *xlsx_path*. Returns True on success.

    Raises _BrowserDied if Playwright reports the page/context/browser is
    closed — the caller should stop the batch instead of churning through
    every remaining playlist hitting the same dead browser.
    """
    name = pl.get("name") or pl["url"]
    try:
        playlist = await scrape_playlist(page, playlist_url=pl["url"])
    except PlaywrightError as e:
        msg = str(e).lower()
        if "target page" in msg or "browser has been closed" in msg or "target closed" in msg:
            logger.error("backfill: browser/page is closed — aborting batch (%s)", name)
            raise _BrowserDied(name) from e
        logger.exception("backfill: scrape failed for %s", name)
        return False
    except Exception:
        logger.exception("backfill: scrape failed for %s", name)
        return False
    if not playlist.songs:
        logger.warning("backfill: '%s' returned 0 songs — skipping write", name)
        return False
    # Sanity-check: did the scraper actually capture durations? If 0 out of
    # N tracks have a duration, something's wrong with the scraper change —
    # better to warn (and still write, so ASINs etc. don't regress) than to
    # silently produce another empty-Duration xlsx.
    with_dur = sum(1 for s in playlist.songs if (s.duration or "").strip())
    if with_dur == 0:
        logger.warning(
            "backfill: '%s' scraped %d tracks but 0 have durations — "
            "scraper DOM extraction may be broken. Writing anyway.",
            name, len(playlist.songs),
        )
    else:
        logger.info("backfill: '%s' got durations for %d/%d tracks",
                    name, with_dur, len(playlist.songs))
    try:
        export_to_xlsx(
            playlist, xlsx_path,
            source_url=pl["url"], source_name=playlist.name or name,
        )
    except Exception:
        logger.exception("backfill: export failed for %s", name)
        return False
    return True


async def _run(args: argparse.Namespace) -> int:
    setup_logging()

    scope = [p for p in args.scope.split("/") if p]
    logger.info("backfill-durations: scope = %s", "/".join(scope) or "<root>")

    org = load_org()
    all_pls = _dedupe_by_url(iter_playlists_recursive(org, scope))
    logger.info("backfill-durations: %d unique playlists under %s",
                len(all_pls), "/".join(scope))

    if not all_pls:
        print("No playlists found under that scope.")
        return 0

    url_to_xlsx = _build_url_to_xlsx_index()
    logger.info("backfill-durations: indexed %d cached xlsx files in %s",
                len(url_to_xlsx), _OUTPUT_DIR)

    buckets = _classify(all_pls, url_to_xlsx)
    needs = buckets["needs"]
    already = buckets["already"]
    missing = buckets["missing"]

    print()
    print(f"  Needs backfill : {len(needs)}")
    print(f"  Already has    : {len(already)}")
    print(f"  No cached xlsx : {len(missing)}")
    print()

    if missing:
        print("Playlists with no cached xlsx (export these normally first):")
        for pl in missing:
            print(f"  - {pl.get('name', '<unnamed>')}  [{pl['url']}]")
        print()

    if args.limit and len(needs) > args.limit:
        print(f"  --limit {args.limit} — trimming queue from {len(needs)} to {args.limit}")
        needs = needs[: args.limit]
        print()

    if not needs:
        print("Nothing to backfill.")
        return 0

    print(f"Queue ({len(needs)} playlists):")
    for pl, xlsx in needs:
        rel = os.path.relpath(xlsx, _OUTPUT_DIR)
        print(f"  - {pl.get('name', '<unnamed>')}  ->  {rel}")
    print()

    if args.dry_run:
        print("--dry-run: no scrapes will be performed.")
        return 0

    if not args.yes:
        try:
            resp = input(f"Proceed with backfilling {len(needs)} playlists? [y/N] ").strip().lower()
        except EOFError:
            resp = ""
        if resp not in ("y", "yes"):
            print("Aborted.")
            return 1

    done = 0
    failed: list[str] = []
    aborted_at: str | None = None
    _keep_awake(True)
    if sys.platform == "win32":
        print("(System idle-sleep blocked for the duration of the run.)")
    try:
        async with BrowserManager(headless=args.headless, slow_mo=args.slow_mo) as bm:
            await ensure_logged_in(bm.page)
            for i, (pl, xlsx) in enumerate(needs, 1):
                name = pl.get("name") or pl["url"]
                print(f"\n[{i}/{len(needs)}] {name}")
                try:
                    ok = await _backfill_one(bm.page, pl, xlsx)
                except _BrowserDied:
                    aborted_at = name
                    print(f"   [ABORTED] browser/page closed -- stopping batch.")
                    print(f"   Re-run the script when Chrome is back; it'll skip what's done.")
                    break
                if ok:
                    done += 1
                    print(f"   [OK] updated {os.path.relpath(xlsx, _OUTPUT_DIR)}")
                else:
                    failed.append(name)
                    print(f"   [FAIL] (see log above)")
    finally:
        _keep_awake(False)

    print()
    if aborted_at:
        remaining = len(needs) - done - len(failed) - 1
        print(f"Backfill ABORTED at '{aborted_at}'. {done} succeeded, "
              f"{len(failed)} prior failures, {remaining} not attempted.")
        print(f"Re-run `python backfill_classical_durations.py` to resume "
              f"-- it's idempotent.")
        return 3
    print(f"Backfill complete: {done}/{len(needs)} succeeded.")
    if failed:
        print(f"Failed ({len(failed)}):")
        for name in failed:
            print(f"  - {name}")
        return 2
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description="Re-scrape Classical playlists to populate the Duration column.",
    )
    p.add_argument("--scope", default="Classical",
                   help="Org-tree folder path to recurse from (default: 'Classical'). "
                        "Use '/' as separator, e.g. 'Classical/Beethoven'.")
    p.add_argument("--dry-run", action="store_true",
                   help="Print the plan, do nothing.")
    p.add_argument("--limit", type=int, default=0,
                   help="Backfill at most N playlists (for testing). 0 = no limit.")
    p.add_argument("--yes", "-y", action="store_true",
                   help="Skip the y/N confirmation prompt.")
    p.add_argument("--headless", action="store_true",
                   help="Run Chrome headlessly (not recommended -- auth flow is fragile headless).")
    p.add_argument("--slow-mo", type=int, default=100,
                   help="Playwright slow_mo in ms (default: 100).")
    args = p.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
