import csv
import os

from playwright.async_api import Page

from config import BASE_URL, LONG_TIMEOUT, DEFAULT_TIMEOUT
from models import Song
from search import hybrid_select, SelectionResult
from utils import logger, screenshot_on_error


def read_csv(csv_path: str) -> list[Song]:
    """Parse a CSV file into a list of Song objects.

    Expected columns: title, artist  (album is optional).
    """
    if not os.path.isfile(csv_path):
        raise FileNotFoundError(f"CSV file not found: {csv_path}")

    songs: list[Song] = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)

        # Validate required columns
        fields = [c.lower().strip() for c in (reader.fieldnames or [])]
        if "title" not in fields or "artist" not in fields:
            raise ValueError("CSV must have at least 'title' and 'artist' columns")

        for row in reader:
            row = {k.lower().strip(): v.strip() for k, v in row.items()}
            if row.get("title") and row.get("artist"):
                songs.append(Song(
                    title=row["title"].replace("_", " "),
                    artist=row["artist"].replace("_", " "),
                    album=row.get("album", ""),
                ))

    logger.info("Read %d songs from %s", len(songs), csv_path)
    return songs


async def create_playlist_from_csv(
    page: Page,
    csv_path: str,
    playlist_name: str,
    auto_threshold: int = 90,
) -> dict:
    """End-to-end: read CSV, create a new playlist, search & add each song."""
    songs = read_csv(csv_path)
    if not songs:
        print("No songs found in CSV — nothing to do.")
        return {"added": 0, "skipped": 0, "results": []}

    return await create_playlist_from_songs(page, songs, playlist_name, auto_threshold)


async def create_playlist_from_songs(
    page: Page,
    songs: list[Song],
    playlist_name: str,
    auto_threshold: int = 90,
    skip_create: bool = False,
) -> dict:
    """Create an Amazon Music playlist and add songs to it.

    Args:
        page: Playwright page (must be logged in).
        songs: List of Song objects with at least title and artist.
        playlist_name: Name for the new playlist.
        auto_threshold: Score threshold for auto-selecting matches.
        skip_create: If True, assume playlist already exists (don't try to create it).

    Returns:
        Summary dict with 'added', 'skipped' counts and 'results' list.
    """
    print(f"\nAdding {len(songs)} songs to playlist \"{playlist_name}\"...\n")

    # Step 1: create a new empty playlist (unless skip_create is set)
    if not skip_create:
        await _create_empty_playlist(page, playlist_name)

    # Step 2: for each song, search and add
    results: list[SelectionResult] = []
    added = 0
    skipped = 0

    for idx, song in enumerate(songs, 1):
        print(f"[{idx}/{len(songs)}] Searching for \"{song.title}\" by {song.artist}...")
        result = await hybrid_select(page, song.title, song.artist, auto_threshold)

        if result.status == "added" and result.selected:
            success = await _add_song_to_playlist(page, result.selected, playlist_name)
            if success:
                added += 1
                print(f"  Added to playlist.")
            else:
                result.status = "skipped"
                skipped += 1
                print(f"  Failed to add to playlist.")
        else:
            skipped += 1

        results.append(result)

    # Step 3: print summary
    print("\n" + "=" * 50)
    print(f"  Playlist: {playlist_name}")
    print(f"  Added:    {added}/{len(songs)}")
    print(f"  Skipped:  {skipped}/{len(songs)}")
    print("=" * 50 + "\n")

    if skipped:
        print("Skipped songs:")
        for r in results:
            if r.status == "skipped":
                print(f"  - {r.song_title} by {r.song_artist}")
        print()

    return {"added": added, "skipped": skipped, "results": results}


# ---------------------------------------------------------------------------
# Adding tracks / creating playlists — redesigned site (Nov 2026)
#
# The web player is React Native Web; everything is addressed by
# data-testid, and ASINs live in React props rather than the DOM:
#   album page row      [data-playback-engage-region]   (fiber prop edgeRef.id = track ASIN)
#   row ⋯ button        [data-testid*="AlbumTrackEdge_ContextMenu"]
#   menu item           [data-testid="OverflowMenu_Option_AddToPlaylist"]
#   picker              [data-testid="AddToPlaylistModal"], items
#                       [data-testid^="AddToPlaylist_Item_"] with a
#                       [data-testid="PlaylistItem_Title"]; 10 per page,
#                       more load as its list scrolls
#   create (library)    LibraryPlaylists_NewPlaylistButton ->
#                       CreatePlaylist_TextInput -> CreatePlaylist_SaveButton
# The ?trackAsin= deep link no longer highlights the track, so the row is
# found by matching the ASIN in its React props.
# ---------------------------------------------------------------------------
_ALBUM_ROW = "[data-playback-engage-region]"
_ROW_MORE_BTN = '[data-testid*="AlbumTrackEdge_ContextMenu"]'
_MENU_ADD = '[data-testid="OverflowMenu_Option_AddToPlaylist"]'
_MENU_ANY = '[data-testid^="OverflowMenu_Option_"]'
_PICKER = '[data-testid="AddToPlaylistModal"]'

# Marks the album row whose track ASIN matches with data-amm-target="1" and
# returns its 1-based row index, or 0 when it isn't rendered (yet).
_MARK_ROW_JS = r"""(asin) => {
    document.querySelectorAll('[data-amm-target]').forEach(e => e.removeAttribute('data-amm-target'));
    const rows = document.querySelectorAll('[data-playback-engage-region]');
    for (let idx = 0; idx < rows.length; idx++) {
        const r = rows[idx];
        const fk = Object.keys(r).find(k => k.startsWith('__reactFiber'));
        let f = fk ? r[fk] : null;
        for (let i = 0; i < 12 && f; i++, f = f.return) {
            const p = f.memoizedProps;
            if (!p) continue;
            for (const key of ['edgeRef', 'trackRef', 'track']) {
                const v = p[key];
                const id = v && (v.id || (v.node && v.node.id));
                if (id === asin) { r.setAttribute('data-amm-target', '1'); return idx + 1; }
            }
        }
    }
    return 0;
}"""

_SCROLL_PAGE_JS = r"""(dy) => {
    const rows = document.querySelectorAll('[data-playback-engage-region]');
    let s = rows.length ? rows[rows.length - 1] : null;
    while (s && s !== document.body) {
        const cs = getComputedStyle(s);
        if (/(auto|scroll)/.test(cs.overflowY) && s.scrollHeight > s.clientHeight + 10) break;
        s = s.parentElement;
    }
    if (!s || s === document.body) { const y = window.scrollY; window.scrollBy(0, dy); return window.scrollY - y; }
    const before = s.scrollTop; s.scrollTop = before + dy; return s.scrollTop - before;
}"""

# Marks the picker item whose title equals *name* (case-insensitive) with
# data-amm-pick="1"; otherwise scrolls the list to load more. Returns
# {found, count}.
_PICKER_FIND_JS = r"""(name) => {
    const want = name.trim().toLowerCase();
    const m = document.querySelector('[data-testid="AddToPlaylistModal"]');
    if (!m) return {found: false, count: -1};
    const items = m.querySelectorAll('[data-testid^="AddToPlaylist_Item_"]');
    for (const it of items) {
        const t = it.querySelector('[data-testid="PlaylistItem_Title"]');
        const txt = ((t && (t.getAttribute('aria-label') || t.textContent)) || '').trim().toLowerCase();
        if (txt === want) { it.setAttribute('data-amm-pick', '1'); return {found: true, count: items.length}; }
    }
    let sc = m.querySelector('[data-testid="AddToPlaylist_RecentsList"]') || m;
    while (sc && sc !== m.parentElement &&
           !(sc.scrollHeight > sc.clientHeight + 10 && /(auto|scroll)/.test(getComputedStyle(sc).overflowY)))
        sc = sc.parentElement;
    if (sc) sc.scrollTop = sc.scrollHeight;
    return {found: false, count: items.length};
}"""


async def _dismiss_overlays(page: Page) -> None:
    for _ in range(2):
        try:
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(300)
        except Exception:
            pass


async def _find_album_row(page: Page, track_asin: str):
    """Scroll the album page until the row for *track_asin* is rendered.

    Returns a Locator for it, or None.
    """
    for attempt in range(60):  # long box sets render rows lazily
        idx = await page.evaluate(_MARK_ROW_JS, track_asin)
        if idx:
            return page.locator('[data-amm-target="1"]').first
        moved = await page.evaluate(_SCROLL_PAGE_JS, 900)
        await page.wait_for_timeout(600)
        if not moved and attempt > 2:
            break
    return None


async def add_track_by_asin(
    page: Page,
    album_asin: str,
    track_asin: str,
    playlist_name: str,
    *,
    source_playlist_url: str = "",
    source_playlist_name: str = "",
    source_row_index: int | None = None,
    source_title: str = "",
    source_artist: str = "",
    source_album: str = "",
) -> bool:
    """Add a single track to *playlist_name*, addressed by its exact ASIN.

    Opens the album page, finds the track's row by the ASIN in its React
    props, opens the row's menu -> "Add to Playlist", and picks the
    playlist by name in the picker.

    The ``source_*`` kwargs describe where *track_asin* came from; they're
    used only when the menu opens without an "Add to Playlist" item —
    Amazon hides it for some web-restricted tracks — so the track is
    recorded in ``web_restricted`` and later runs skip it.
    """
    if not album_asin or not track_asin:
        logger.warning("add_track_by_asin: missing ASIN(s) — album=%r track=%r", album_asin, track_asin)
        return False
    if not playlist_name:
        logger.warning("add_track_by_asin: missing playlist_name")
        return False

    url = f"{BASE_URL}/albums/{album_asin}?trackAsin={track_asin}"
    logger.info("add_track_by_asin: navigating to %s", url)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=LONG_TIMEOUT)
        await page.wait_for_selector(_ALBUM_ROW, state="attached", timeout=LONG_TIMEOUT)
    except Exception as e:
        logger.warning("add_track_by_asin: album page did not load (%s): %s", url, e)
        await screenshot_on_error(page, "album_not_loaded")
        return False
    await page.wait_for_timeout(1000)

    row = await _find_album_row(page, track_asin)
    if row is None:
        logger.warning("add_track_by_asin: track %s not found on album %s", track_asin, album_asin)
        await screenshot_on_error(page, f"track_row_missing_{track_asin}")
        return False

    status = await _open_menu_and_pick(page, row, playlist_name, debug_tag=track_asin)

    if status == "menu_hidden" and source_playlist_url:
        try:
            import web_restricted
            web_restricted.add(
                playlist_url=source_playlist_url,
                playlist_name=source_playlist_name,
                row_index=source_row_index,
                title=source_title,
                artist=source_artist,
                album=source_album,
                requested_asin=track_asin,
                substituted_asin="",
            )
            logger.info(
                "add_track_by_asin: recorded web-restricted track %s (album %s) "
                "— source playlist=%s row=%s",
                track_asin, album_asin, source_playlist_name, source_row_index,
            )
        except Exception:
            logger.exception("add_track_by_asin: web_restricted.add failed (non-fatal)")
    return status == ""


async def _open_menu_and_pick(page: Page, row, playlist_name: str, *, debug_tag: str = "") -> str:
    """Open *row*'s menu, choose "Add to Playlist", pick *playlist_name*.

    Returns "" on success, "menu_hidden" when the menu opened without an
    "Add to Playlist" item (web-restriction signal), "menu_failed" when no
    menu opened, or "picker_failed" when the playlist wasn't in the picker.
    """
    tag = debug_tag or "noasin"
    try:
        await row.scroll_into_view_if_needed(timeout=5000)
        await row.hover(timeout=5000)
        await page.wait_for_timeout(300)
        more = row.locator(_ROW_MORE_BTN).first
        if await more.count():
            await more.click(timeout=5000)
        else:
            await row.click(button="right", timeout=5000)
        await page.wait_for_selector(_MENU_ANY, state="visible", timeout=5000)
    except Exception as e:
        logger.warning("Track menu did not open (track=%s): %s", tag, e)
        await screenshot_on_error(page, f"menu_failed_{tag}")
        await _dismiss_overlays(page)
        return "menu_failed"

    add_item = page.locator(_MENU_ADD).first
    if not await add_item.count() or not await add_item.is_visible():
        logger.warning("'Add to Playlist' not in track menu (track=%s) — likely web-restricted", tag)
        await screenshot_on_error(page, f"no_add_to_playlist_{tag}")
        await _dismiss_overlays(page)
        return "menu_hidden"
    await add_item.click(timeout=5000)
    logger.info("Clicked 'Add to Playlist' (track=%s)", tag)

    return await _pick_playlist_in_picker(page, playlist_name)


async def _pick_playlist_in_picker(page: Page, playlist_name: str) -> str:
    try:
        await page.wait_for_selector(
            f'{_PICKER} [data-testid^="AddToPlaylist_Item_"]', state="visible", timeout=10_000
        )
    except Exception:
        logger.warning("Playlist picker did not appear")
        await screenshot_on_error(page, "picker_missing")
        await _dismiss_overlays(page)
        return "picker_failed"

    last_count, stale = -1, 0
    for _ in range(200):  # 10 playlists per page -> up to ~2000 playlists
        res = await page.evaluate(_PICKER_FIND_JS, playlist_name)
        if res.get("found"):
            item = page.locator('[data-amm-pick="1"]').first
            await item.scroll_into_view_if_needed(timeout=5000)
            await item.click(timeout=5000)
            await page.evaluate("() => document.querySelectorAll('[data-amm-pick]').forEach(e => e.removeAttribute('data-amm-pick'))")
            try:
                await page.wait_for_selector(_PICKER, state="detached", timeout=8000)
            except Exception:
                logger.warning("Picker stayed open after choosing '%s'", playlist_name)
                await screenshot_on_error(page, "picker_stuck")
                await _dismiss_overlays(page)
                return "picker_failed"
            logger.info("Added song to playlist '%s'", playlist_name)
            await page.wait_for_timeout(800)
            return ""
        count = res.get("count", 0)
        if count == last_count:
            stale += 1
            if stale >= 4:
                break
        else:
            last_count, stale = count, 0
        await page.wait_for_timeout(700)

    logger.warning("Playlist '%s' not found in picker (%d playlists listed)", playlist_name, last_count)
    await screenshot_on_error(page, "playlist_not_in_picker")
    await _dismiss_overlays(page)
    return "picker_failed"


async def create_playlist(page: Page, name: str) -> str:
    """Create an empty playlist named *name*; return its URL ("" if unknown).

    Raises RuntimeError if the create dialog can't be driven.
    """
    import re as _re
    logger.info("Creating new playlist '%s'", name)
    await page.goto(f"{BASE_URL}/my/playlists/all", wait_until="domcontentloaded", timeout=LONG_TIMEOUT)
    btn = page.locator('[data-testid*="LibraryPlaylists_NewPlaylistButton"]').first
    try:
        await btn.wait_for(state="visible", timeout=LONG_TIMEOUT)
        await btn.click()
        name_input = page.locator('[data-testid*="CreatePlaylist_TextInput"]').first
        await name_input.wait_for(state="visible", timeout=10_000)
        await name_input.fill(name)
        await page.wait_for_timeout(300)
        await page.locator('[data-testid*="CreatePlaylist_SaveButton"]').first.click()
    except Exception as e:
        await screenshot_on_error(page, "create_playlist_failed")
        await _dismiss_overlays(page)
        raise RuntimeError(f"Could not create playlist '{name}': {e}") from e

    uuid_re = _re.compile(r"/playlists/([0-9a-f]{8}-[0-9a-f-]{27})")
    try:
        await page.wait_for_url(_re.compile(r".*/playlists/[0-9a-f]{8}-"), timeout=10_000)
    except Exception:
        await page.wait_for_timeout(2000)
    m = uuid_re.search(page.url)
    if not m:
        # Didn't navigate — look the new playlist up in the library list.
        try:
            href = await page.evaluate(
                """(n) => { for (const a of document.querySelectorAll('a[href*="playlists/"]')) {
                       const t = (a.getAttribute('aria-label') || a.textContent || '').trim();
                       if (t.toLowerCase() === n.toLowerCase()) return a.getAttribute('href'); }
                   return ''; }""", name)
            m = uuid_re.search(href or "")
        except Exception:
            m = None
    if not m:
        logger.warning("Playlist '%s' created but its URL is unknown (page at %s)", name, page.url)
        return ""
    url = f"{BASE_URL}/my/playlists/{m.group(1)}"
    logger.info("Playlist '%s' created: %s", name, url)
    return url


async def _create_empty_playlist(page: Page, name: str):
    """CLI wrapper: create the playlist, falling back to asking the user."""
    try:
        await create_playlist(page, name)
    except RuntimeError as e:
        logger.warning("%s", e)
        print(f"\n  Could not create the playlist automatically. Please create \"{name}\" manually.")
        input("  Press Enter when ready to continue...")


async def _add_song_to_playlist(page: Page, candidate, playlist_name: str) -> bool:
    """Add a search-result *candidate* to *playlist_name* (via its ASINs)."""
    asin = getattr(candidate, "asin", "")
    album_asin = getattr(candidate, "album_asin", "")
    if not asin or not album_asin:
        logger.warning("Search result '%s' by '%s' has no ASIN — cannot add", candidate.title, candidate.artist)
        return False
    try:
        return await add_track_by_asin(page, album_asin, asin, playlist_name)
    except Exception as e:
        logger.error("Failed to add song to playlist: %s", e)
        await screenshot_on_error(page, "add_song_error")
        await _dismiss_overlays(page)
        return False
