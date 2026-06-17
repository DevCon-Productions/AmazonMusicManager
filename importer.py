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
    """Add a single track to *playlist_name* via the Amazon-Music deep-link.

    Navigates to ``/albums/<album_asin>?trackAsin=<track_asin>`` — Amazon
    renders the album page with the target track scrolled into view —
    then opens that row's 3-dot menu and picks the playlist by name.

    Why this exists (vs. search-based add): for Classical recordings,
    Amazon's Songs search results expose only (title, artist) — NOT album
    — so `score_match` can't distinguish recordings with the same title.
    The deep link sidesteps search entirely and addresses the exact ASIN.

    Returns True on success. On failure (track not found on album page,
    "Add to playlist" not in menu, playlist not in picker) returns False
    and dismisses any open menus.

    The optional ``source_*`` kwargs describe where *track_asin* came from
    in the user's library (which playlist, which row, what title). They
    are used only when the picker reports ``"menu_hidden"`` — Amazon's web
    player is hiding the "Add to playlist" item for the track, which is
    the 2026-06-12 web-restriction symptom. In that case the track is
    recorded in ``web_restricted`` so the pool builder excludes the
    surrounding performance on subsequent generator runs. Callers without
    source metadata (e.g. ad-hoc adds) can omit the kwargs; the recording
    step quietly no-ops without ``source_playlist_url``.
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
    except Exception as e:
        logger.error("add_track_by_asin: navigation failed: %s", e)
        return False

    # Album pages render tracks as <music-text-row> (NOT <music-image-row>
    # as playlist pages do). The ?trackAsin= deep-link adds class="focused"
    # to the matching row, so we wait for that to appear — both confirms
    # the tracklist has loaded AND identifies the correct row. ASINs are
    # NOT exposed anywhere in the album-page DOM (verified via recon
    # 2026-06-03: no element attribute, innerText, or JS property carries
    # the trackAsin), so the focused-class is the only ASIN-level handle.
    try:
        await page.wait_for_selector(
            'music-text-row[focused]', state="attached", timeout=LONG_TIMEOUT
        )
    except Exception:
        logger.warning(
            "add_track_by_asin: focused row never appeared on %s — "
            "deep-link may have failed or the album's tracklist is "
            "still loading", url,
        )
        await screenshot_on_error(page, "album_no_focused_row")
        return False

    # Brief settle to let the focused row finish painting and the
    # auto-scroll-into-view animation complete.
    await page.wait_for_timeout(1500)

    row_handle = await page.evaluate_handle("""() => {
        const rows = document.querySelectorAll('music-text-row[focused]');
        // Should be exactly one — but in case the picker matched
        // multiple (shouldn't happen for a real ASIN), take the first.
        return rows.length ? rows[0] : null;
    }""")

    is_null = await row_handle.evaluate("el => el === null")
    if is_null:
        logger.warning(
            "add_track_by_asin: focused row vanished between wait and "
            "lookup on album %s (track %s)", album_asin, track_asin,
        )
        await screenshot_on_error(page, "track_row_missing")
        return False

    # Scroll, hover, click 3-dot — same pre-menu choreography as
    # _add_song_to_playlist. Duplicated here (a small block) rather than
    # extracted; row-finding logic differs upstream so a shared wrapper
    # adds little.
    await row_handle.evaluate("el => el.scrollIntoView({ block: 'center', inline: 'center' })")
    await page.wait_for_timeout(1000)

    box = await row_handle.bounding_box()
    if not box:
        logger.warning("add_track_by_asin: matched row has no bounding box")
        return False

    await page.mouse.move(box["x"] + box["width"] - 30, box["y"] + box["height"] / 2)
    await page.wait_for_timeout(1000)

    more_clicked = await _click_more_button(page, row_handle)
    menu_opener = "3-dot" if more_clicked else "right-click"
    if not more_clicked:
        logger.info(
            "add_track_by_asin: 3-dot not found for track %s — falling "
            "back to right-click", track_asin,
        )
        await page.mouse.click(
            box["x"] + box["width"] / 2, box["y"] + box["height"] / 2,
            button="right",
        )

    # Poll for menu appearance instead of a fixed 2s wait. Records when
    # (if ever) music-list-item shows up so the next failure tells us
    # 'click never opened menu' vs 'menu opened+closed before our find'.
    async def _wait_for_menu(max_ms: int) -> int:
        elapsed = 0
        while elapsed < max_ms:
            await page.wait_for_timeout(250)
            elapsed += 250
            try:
                count = await page.evaluate(
                    "() => document.querySelectorAll('music-list-item').length"
                )
            except Exception:
                count = 0
            if count and count > 0:
                return elapsed
        return -1

    menu_seen_at_ms = await _wait_for_menu(3000)
    if menu_seen_at_ms >= 0:
        logger.info(
            "add_track_by_asin: menu appeared at %dms after %s for "
            "track %s", menu_seen_at_ms, menu_opener, track_asin,
        )
    else:
        # 2026-06-14: the 3-dot JS-click on Beethoven 8 mvt III rows
        # returns 'found_direct' but Amazon's React handler doesn't open
        # a menu in response. User confirmed right-click manually shows
        # "Add to Playlist" on the same row. Retry with a real Playwright
        # right-click (a fresh bounding-box query in case the page
        # scrolled since the original hover) before giving up.
        logger.warning(
            "add_track_by_asin: menu did not appear in 3000ms after "
            "%s click for track %s — retrying with right-click",
            menu_opener, track_asin,
        )
        try:
            # Dismiss any stale overlay just in case.
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(500)
            retry_box = await row_handle.bounding_box()
            if retry_box:
                await page.mouse.click(
                    retry_box["x"] + retry_box["width"] / 2,
                    retry_box["y"] + retry_box["height"] / 2,
                    button="right",
                )
                menu_opener = f"{menu_opener}+rclick_retry"
                menu_seen_at_ms = await _wait_for_menu(3000)
                if menu_seen_at_ms >= 0:
                    logger.info(
                        "add_track_by_asin: menu appeared at %dms after "
                        "right-click retry for track %s",
                        menu_seen_at_ms, track_asin,
                    )
                else:
                    logger.warning(
                        "add_track_by_asin: menu STILL did not appear "
                        "after right-click retry for track %s",
                        track_asin,
                    )
            else:
                logger.warning(
                    "add_track_by_asin: row had no bounding box on "
                    "right-click retry for track %s", track_asin,
                )
        except Exception:
            logger.exception(
                "add_track_by_asin: right-click retry raised "
                "(non-fatal, continuing)"
            )

    status = await _open_add_to_playlist_picker_and_pick(
        page, playlist_name, debug_tag=track_asin, menu_opener=menu_opener,
    )
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
                "add_track_by_asin: recorded web-restricted track "
                "%s (album %s) — source playlist=%s row=%s",
                track_asin, album_asin, source_playlist_name,
                source_row_index,
            )
        except Exception:
            logger.exception(
                "add_track_by_asin: web_restricted.add failed "
                "(non-fatal, continuing)"
            )
    return status == ""


async def _create_empty_playlist(page: Page, name: str):
    """Navigate to the playlist page and create a new empty playlist.

    Flow: Click "Create Playlist" → native-click "Curate a playlist" →
    fill name input → click "Save".
    """
    logger.info("Creating new playlist '%s'", name)

    await page.goto(f"{BASE_URL}/my/playlists/all", wait_until="domcontentloaded", timeout=LONG_TIMEOUT)
    await page.wait_for_timeout(3000)

    # Step 1: Click "Create Playlist" button
    clicked = await page.evaluate("""() => {
        const els = document.querySelectorAll('music-button, button, a, [role="button"]');
        for (const el of els) {
            const text = (el.textContent || '').trim().toLowerCase();
            if (text.includes('create playlist')) {
                el.click();
                return true;
            }
        }
        return false;
    }""")

    if not clicked:
        logger.warning("Could not find Create Playlist button")
        print(f"\n  Could not find Create Playlist button. Please create \"{name}\" manually.")
        input("  Press Enter when ready to continue...")
        return

    logger.info("Clicked Create Playlist button")
    await page.wait_for_timeout(3000)

    # Step 2: Native-click "Curate a playlist" in the dialog
    # Must use Playwright's real mouse click — JS .click() doesn't work here
    curate_handle = await page.evaluate_handle("""() => {
        const d = document.querySelector('[role="dialog"], [aria-modal="true"]');
        if (!d) return null;
        const rows = d.querySelectorAll('music-image-row');
        for (const row of rows) {
            const pt = (row.getAttribute('primary-text') || '').toLowerCase();
            if (pt.includes('curate')) return row;
        }
        return null;
    }""")

    is_null = await curate_handle.evaluate("el => el === null")
    if is_null:
        logger.warning("Could not find 'Curate a playlist' option")
        print(f"\n  Could not find 'Curate a playlist' option. Please create \"{name}\" manually.")
        input("  Press Enter when ready to continue...")
        return

    box = await curate_handle.bounding_box()
    if box:
        await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        logger.info("Clicked 'Curate a playlist'")
    else:
        logger.warning("No bounding box for Curate option")
        return

    await page.wait_for_timeout(3000)

    # Step 3: Fill in the playlist name
    name_input = await page.query_selector('input[placeholder*="Playlist" i]')
    if not name_input:
        # Broader search
        name_input = await page.query_selector('[role="dialog"] input[type="text"]')

    if name_input:
        await name_input.fill("")
        await name_input.type(name, delay=30)
        logger.info("Entered playlist name: '%s'", name)
        await page.wait_for_timeout(500)
    else:
        logger.warning("Could not find name input — typing with keyboard")
        await page.keyboard.type(name, delay=30)

    # Step 4: Click "Save"
    save_btn = await page.query_selector('[role="dialog"] music-button:has-text("Save")')
    if not save_btn:
        # Broader: find any button with "Save" text in the dialog
        save_handle = await page.evaluate_handle("""() => {
            const d = document.querySelector('[role="dialog"], [aria-modal="true"]');
            if (!d) return null;
            const btns = d.querySelectorAll('music-button, button');
            for (const btn of btns) {
                const text = (btn.textContent || '').trim().toLowerCase();
                if (text === 'save') return btn;
            }
            return null;
        }""")
        is_null = await save_handle.evaluate("el => el === null")
        if not is_null:
            save_btn = save_handle

    if save_btn:
        box = await save_btn.bounding_box()
        if box:
            await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            logger.info("Clicked Save button")
        else:
            await save_btn.click()
    else:
        logger.warning("Could not find Save button — pressing Enter")
        await page.keyboard.press("Enter")

    await page.wait_for_timeout(3000)
    logger.info("Playlist '%s' created successfully", name)


async def _add_song_to_playlist(page: Page, candidate, playlist_name: str) -> bool:
    """Add a song from search results to a playlist.

    Instead of trying to interact with the search result element directly
    (which may scroll off in a horizontal shoveler), we use the search result's
    index to re-find and interact with it on the current page.
    """
    try:
        title = candidate.title
        artist = candidate.artist

        # Re-find the element on the page by matching title + artist
        # Search results are music-horizontal-item elements in the Songs shoveler
        row_handle = await page.evaluate_handle("""(args) => {
            const [title, artist] = args;

            // First check music-horizontal-item (search results)
            const items = document.querySelectorAll('music-horizontal-item');
            for (const item of items) {
                const pt = item.getAttribute('primary-text') || '';
                const st = item.getAttribute('secondary-text') || '';
                if (pt === title && st === artist) return item;
            }

            // Fallback: check music-image-row (playlist page results)
            const rows = document.querySelectorAll('music-image-row');
            for (const row of rows) {
                const pt = row.getAttribute('primary-text') || '';
                const st = row.getAttribute('secondary-text-1') || '';
                if (pt === title && st === artist) return row;
            }

            return null;
        }""", [title, artist])

        is_null = await row_handle.evaluate("el => el === null")
        if is_null:
            logger.warning("Could not re-find '%s' by '%s' on page", title, artist)
            return False

        # Scroll the element into view
        await row_handle.evaluate("el => el.scrollIntoView({ block: 'center', inline: 'center' })")
        await page.wait_for_timeout(1000)

        # Get bounding box
        box = await row_handle.bounding_box()
        if not box:
            logger.warning("Element has no bounding box after scroll")
            return False

        # Hover over the row to reveal the 3-dot menu button
        await page.mouse.move(box["x"] + box["width"] - 30, box["y"] + box["height"] / 2)
        await page.wait_for_timeout(1000)

        # Try clicking the 3-dot (more) button within this row
        more_clicked = await _click_more_button(page, row_handle)

        if not more_clicked:
            # Try right-clicking as fallback
            logger.debug("3-dot button not found, trying right-click")
            await page.mouse.click(
                box["x"] + box["width"] / 2, box["y"] + box["height"] / 2,
                button="right"
            )

        await page.wait_for_timeout(2000)
        status = await _open_add_to_playlist_picker_and_pick(page, playlist_name)
        return status == ""

    except Exception as e:
        logger.error("Failed to add song to playlist: %s", e)
        await screenshot_on_error(page, "add_song_error")
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        return False


async def _open_add_to_playlist_picker_and_pick(
    page: Page, playlist_name: str,
    *, debug_tag: str = "", menu_opener: str = "",
) -> str:
    """After the row's 3-dot context menu is open, click "Add to playlist",
    then find and click *playlist_name* in the resulting picker.

    Shared between the search-based `_add_song_to_playlist` and the
    ASIN-direct `add_track_by_asin` flows — same picker, same JS selectors,
    same scroll-and-retry loop. Pre-condition: the context menu (containing
    an "Add to playlist" item) is visible. On exit (success or failure)
    the menu/picker is dismissed.

    Returns a status code:

      ``""``               — success
      ``"menu_hidden"``    — "Add to playlist" item not present in the
                             context menu. This is the new web-restriction
                             symptom (2026-06-12): for some track ASINs
                             Amazon hides the menu item entirely on the web
                             player — a separate failure mode from the
                             silent ASIN substitution detected at click
                             time. Callers with source-track metadata
                             should persist the track to
                             ``web_restricted`` so the pool filter excludes
                             it on subsequent generator runs.
      ``"picker_failed"``  — Add-to-playlist menu opened but the target
                             playlist was not found in the picker after
                             scrolling. Not a web-restriction signal.
    """
    # Take a screenshot to see the context menu — uniquely-named so failed
    # attempts don't overwrite history when comparing across tracks.
    tag = debug_tag or "noasin"
    await screenshot_on_error(page, f"context_menu_{tag}")

    # Look for "Add to playlist" in the context menu
    add_clicked = await page.evaluate("""() => {
        const allEls = document.querySelectorAll(
            'music-list-item, [role="menuitem"], [role="option"], button, a, li, span, div'
        );
        for (const item of allEls) {
            const text = (item.textContent || '').trim().toLowerCase();
            const label = (item.getAttribute('aria-label') || '').toLowerCase();
            // Match "Add to playlist" but not parent elements that contain more text
            if ((text === 'add to playlist' || text === 'add to a playlist' ||
                 label === 'add to playlist' || label === 'add to a playlist') &&
                item.offsetParent !== null) {
                item.click();
                return 'found';
            }
        }
        // Also check for primary-text attribute on music-list-item
        const listItems = document.querySelectorAll('music-list-item');
        for (const item of listItems) {
            const pt = item.getAttribute('primary-text') || '';
            if (pt.toLowerCase().includes('add to playlist')) {
                item.click();
                return 'found_via_attr';
            }
        }
        return null;
    }""")

    if not add_clicked:
        logger.warning(
            "'Add to playlist' option not found in context menu "
            "(track=%s, opened via %s)", tag, menu_opener or "?",
        )
        # Diagnostic dump: what music-list-items + role=menuitem elements
        # exist in the live DOM right now? The screenshot tells us what
        # rendered, this tells us what's reachable from
        # document.querySelectorAll — so we can tell shadow-DOM vs
        # attribute-not-set vs wrong-element-type apart on the next run.
        try:
            diag = await page.evaluate("""() => {
                const lists = Array.from(document.querySelectorAll('music-list-item'));
                const items = Array.from(document.querySelectorAll('[role="menuitem"]'));
                const opts  = Array.from(document.querySelectorAll('[role="option"]'));
                const visible = el => {
                    if (!el || !el.offsetParent) return false;
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                };
                return {
                    listCount: lists.length,
                    listVisibleCount: lists.filter(visible).length,
                    listPrimaryTexts: lists.slice(0, 30).map(el => ({
                        primary: el.getAttribute('primary-text') || '',
                        visible: visible(el),
                        text: (el.textContent || '').trim().slice(0, 60),
                    })),
                    menuitemCount: items.length,
                    menuitemTexts: items.slice(0, 30).map(el => ({
                        text: (el.textContent || '').trim().slice(0, 60),
                        label: (el.getAttribute('aria-label') || '').slice(0, 60),
                        visible: visible(el),
                    })),
                    optionCount: opts.length,
                    // Any element whose visible text mentions "playlist"?
                    playlistMentions: Array.from(document.querySelectorAll('*'))
                        .filter(el => {
                            const t = (el.textContent || '').toLowerCase();
                            return t.length < 80 && t.includes('add to playlist') && visible(el);
                        })
                        .slice(0, 10)
                        .map(el => ({
                            tag: el.tagName.toLowerCase(),
                            text: (el.textContent || '').trim().slice(0, 80),
                            primary: el.getAttribute('primary-text') || '',
                            label: el.getAttribute('aria-label') || '',
                        })),
                };
            }""")
            logger.warning(
                "  diag(menu): music-list-items=%d (visible=%d) "
                "role=menuitem=%d role=option=%d 'add to playlist' "
                "mentions=%d",
                diag.get("listCount", -1),
                diag.get("listVisibleCount", -1),
                diag.get("menuitemCount", -1),
                diag.get("optionCount", -1),
                len(diag.get("playlistMentions") or []),
            )
            for entry in (diag.get("listPrimaryTexts") or [])[:15]:
                logger.warning(
                    "    music-list-item primary=%r visible=%s text=%r",
                    entry.get("primary"), entry.get("visible"),
                    entry.get("text"),
                )
            for entry in (diag.get("playlistMentions") or [])[:5]:
                logger.warning(
                    "    playlistMention tag=%s primary=%r label=%r text=%r",
                    entry.get("tag"), entry.get("primary"),
                    entry.get("label"), entry.get("text"),
                )
        except Exception:
            logger.exception("diag(menu): collection failed (non-fatal)")
        await screenshot_on_error(page, f"no_add_to_playlist_{tag}")
        await page.keyboard.press("Escape")
        return "menu_hidden"

    logger.info("Clicked 'Add to playlist': %s", add_clicked)

    # Wait for the playlist picker to fully load — new playlists may take
    # a moment to appear in the list
    await page.wait_for_timeout(4000)

    # Take screenshot of playlist picker
    await screenshot_on_error(page, "playlist_picker")

    # Now a playlist picker should appear — find and click the target playlist.
    # The picker contains music-image-row elements with primary-text attributes
    # for each playlist name.

    for attempt in range(8):
        # Find the playlist row and click it using Playwright's native click
        # (which properly dispatches mouse events) instead of JS .click()
        playlist_clicked = False
        rows = await page.query_selector_all('music-image-row[primary-text]')
        for row in rows:
            pt = await row.get_attribute('primary-text')
            if not pt:
                continue
            if pt.strip().lower() == playlist_name.strip().lower():
                # Check this isn't a search result row (picker rows have no secondary-text-1)
                st1 = await row.get_attribute('secondary-text-1')
                if st1:
                    continue
                # Check it's visible
                if not await row.is_visible():
                    continue
                try:
                    await row.click(timeout=3000)
                    logger.info("Clicked playlist row '%s' via Playwright click", pt)
                    playlist_clicked = True
                except Exception:
                    # Try clicking the music-link child instead
                    link = await row.query_selector('music-link')
                    if link:
                        try:
                            await link.click(timeout=3000)
                            logger.info("Clicked playlist link '%s' via Playwright click", pt)
                            playlist_clicked = True
                        except Exception as e2:
                            logger.debug("Link click also failed: %s", e2)
                break

        if playlist_clicked:
            logger.info("Added song to playlist '%s'", playlist_name)
            await page.wait_for_timeout(2000)
            return ""

        # Scroll down in the picker in case the playlist is below the fold
        await page.evaluate("""() => {
            const dialogs = document.querySelectorAll('[role="dialog"], [aria-modal="true"], [class*="modal"], [class*="dialog"]');
            for (const d of dialogs) {
                const scrollable = d.querySelector('[style*="overflow"], [class*="scroll"]');
                if (scrollable) {
                    scrollable.scrollTop += 300;
                    return;
                }
                // Try scrolling the dialog itself
                d.scrollTop += 300;
            }
        }""")
        await page.wait_for_timeout(1000)

    logger.warning("Playlist '%s' not found in picker", playlist_name)
    await screenshot_on_error(page, "playlist_not_in_picker")
    await page.keyboard.press("Escape")
    await page.wait_for_timeout(500)
    await page.keyboard.press("Escape")
    return "picker_failed"


async def _click_more_button(page: Page, row_handle) -> bool:
    """Try to find and click the 3-dot menu button within a row element."""

    # Diagnostic: which row are we actually operating on right now? On the
    # 2026-06-14 false-positive run mvt III's 3-dot click didn't open a
    # menu, and we suspect the row_handle had drifted to a different row
    # (e.g. focus moved to mvt II after auto-play kicked in). Log the
    # row's primary-text so a follow-up run can confirm.
    try:
        row_info = await page.evaluate("""(row) => {
            return {
                primary: row.getAttribute('primary-text') || '',
                focused: row.hasAttribute('focused'),
                attached: row.isConnected,
            };
        }""", row_handle)
        logger.info(
            "_click_more_button: row primary=%r focused=%s attached=%s",
            row_info.get("primary"), row_info.get("focused"),
            row_info.get("attached"),
        )
    except Exception:
        logger.exception("_click_more_button: row introspection failed")

    # Strategy 1: Look for music-button with icon-name="more" or similar inside the row.
    # IMPORTANT: filter to actual button tags. <div role="button"> elements
    # are row-click handlers whose aria-label is the track title — for any
    # movement titled "Menuetto", "More than this", etc. their label
    # contains the substring "menu"/"more" and would false-match the
    # label fallback (the 2026-06-14 Beethoven 8 mvt III bug).
    clicked = await page.evaluate("""(row) => {
        const btns = row.querySelectorAll('music-button, button');
        const wordBoundary = /\\b(more|menu|options)\\b/;
        for (const btn of btns) {
            const icon = btn.getAttribute('icon-name') || '';
            const label = (btn.getAttribute('aria-label') || '').toLowerCase();
            // Primary: icon-name="more" is the canonical 3-dot. The label
            // fallback exists for tooltipped buttons whose icon attribute
            // wasn't populated yet; require word-boundary match so
            // "menuetto" / "more than this" track titles don't slip
            // through.
            if (icon === 'more' || icon === 'contextMenu' ||
                wordBoundary.test(label) ||
                label === 'opens menu') {
                btn.click();
                return 'found_direct:icon=' + (icon || '_') +
                       '/label=' + (label || '_').slice(0, 40) +
                       '/tag=' + btn.tagName.toLowerCase();
            }
        }
        // If no "more" button found, look for ANY music-button that isn't the play button
        for (const btn of btns) {
            const icon = btn.getAttribute('icon-name') || '';
            if (icon && icon !== 'play') {
                btn.click();
                return 'found_non_play: ' + icon;
            }
        }
        return null;
    }""", row_handle)

    if clicked:
        logger.info("_click_more_button: strategy 1 (%s)", clicked)
        return True

    # Strategy 2: Check shadow DOMs of child music-buttons
    clicked = await page.evaluate("""(row) => {
        const musicBtns = row.querySelectorAll('music-button');
        for (const mb of musicBtns) {
            if (mb.shadowRoot) {
                const innerBtn = mb.shadowRoot.querySelector('button');
                if (innerBtn) {
                    const label = (innerBtn.getAttribute('aria-label') || '').toLowerCase();
                    if (label.includes('more') || label.includes('menu') || label.includes('opens menu')) {
                        innerBtn.click();
                        return 'found_in_shadow';
                    }
                }
            }
        }
        return null;
    }""", row_handle)

    if clicked:
        logger.info("_click_more_button: strategy 2 (shadow DOM, %s)", clicked)
        return True

    # Strategy 3: Find any button with the "more" SVG icon near this row
    box = await row_handle.bounding_box()
    if box:
        # Click at the far right of the row where the 3-dot button typically is
        await page.mouse.click(box["x"] + box["width"] - 20, box["y"] + box["height"] / 2)
        await page.wait_for_timeout(500)

        # Check if a menu appeared
        menu_appeared = await page.evaluate("""() => {
            const menus = document.querySelectorAll(
                '[role="menu"], [class*="context"], music-list-item'
            );
            return menus.length > 0;
        }""")

        if menu_appeared:
            logger.info("_click_more_button: strategy 3 (mouse coord at "
                        "%.0f,%.0f)", box["x"] + box["width"] - 20,
                        box["y"] + box["height"] / 2)
            return True

    return False
