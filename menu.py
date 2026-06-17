"""Interactive menu for Amazon Music Playlist Manager."""

import asyncio
import glob
import os
import sys

from organization import from_stored_path, to_stored_path
from utils import setup_logging


def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')


def pause():
    input("\nPress Enter to continue...")


def paged_list(items, formatter, page_size=20):
    """Print a list of items with pagination.

    Args:
        items: iterable of items to display
        formatter: function(index, item) -> str for each line
        page_size: lines to show before pausing
    """
    for i, item in enumerate(items, 1):
        print(formatter(i, item))
        if i % page_size == 0 and i < len(items):
            resp = input(f"    — {i}/{len(items)} shown. Press Enter for more, 'q' to stop — ").strip()
            if resp.lower() in ("q", "quit"):
                print(f"    (skipped remaining {len(items) - i})")
                return
    print()


# Sentinel returned by input helpers when the user wants to go back one step.
# `None` means "quit to main menu", `BACK` means "previous step".
BACK = object()


def pick(prompt, options):
    """Show numbered options and return the chosen value.

    Returns:
        The chosen value, `None` to quit to main menu, or `BACK` to go back one step.
    """
    for i, (label, _) in enumerate(options, 1):
        print(f"  {i}. {label}")
    print()
    while True:
        raw = input(prompt).strip()
        if raw.lower() in ("q", "quit", "exit"):
            return None
        if raw.lower() in ("b", "back"):
            return BACK
        try:
            idx = int(raw)
            if 1 <= idx <= len(options):
                return options[idx - 1][1]
        except ValueError:
            pass
        print(f"  Enter 1-{len(options)}, 'b' to go back, or 'q' for main menu.")


def list_spreadsheets_with_tags():
    """Return list of (filename, path, tag_display) for all .xlsx in ./output/."""
    from spreadsheet_tags import get_tag_display
    files = sorted(glob.glob("./output/*.xlsx"))
    result = []
    for f in files:
        name = os.path.basename(f)
        tag = get_tag_display(f)
        result.append((name, f, tag))
    return result


def print_spreadsheet_list(items):
    """Print a numbered list of spreadsheets with tags."""
    for i, (name, _, tag) in enumerate(items, 1):
        if tag:
            print(f"    {i}. {name}  {tag}")
        else:
            print(f"    {i}. {name}")
    print()


def pick_spreadsheet():
    """Let the user pick an existing spreadsheet from ./output/."""
    items = list_spreadsheets_with_tags()
    if not items:
        print("  No spreadsheets found in ./output/")
        return None
    options = [(f"{name}  {tag}" if tag else name, path) for name, path, tag in items]
    return pick("  Select file: ", options)


def ask(prompt, default=""):
    """Prompt for input with an optional default.

    Returns:
        The entered string (or default), `None` to quit to main menu, or `BACK` to go back one step.
    """
    if default:
        raw = input(f"{prompt} [{default}]: ").strip()
    else:
        raw = input(f"{prompt}: ").strip()
    if raw.lower() in ("q", "quit"):
        return None
    if raw.lower() in ("b", "back"):
        return BACK
    if default and not raw:
        return default
    return raw


def yes_no(prompt, default=True):
    """Ask a yes/no question.

    Returns True, False, None (quit to main), or BACK (previous step).
    """
    hint = "Y/n" if default else "y/N"
    raw = input(f"{prompt} ({hint}): ").strip().lower()
    if raw in ("q", "quit"):
        return None
    if raw in ("b", "back"):
        return BACK
    if not raw:
        return default
    return raw in ("y", "yes")


def menu_export_new():
    """Export a playlist to a new spreadsheet."""
    step = 0
    method = None
    playlist_name = None
    playlist_url = None
    output = None
    metadata = None

    while True:
        if step == 0:
            print("\n  How do you want to identify the playlist?\n")
            method = pick("  Choose: ", [
                ("Browse my playlists (pick from a list)", "list"),
                ("Enter playlist name", "name"),
                ("Enter playlist URL", "url"),
            ])
            if method is None or method is BACK:
                return
            playlist_name = None
            playlist_url = None
            step = 1

        elif step == 1:
            if method == "list":
                playlist_name = _browse_and_pick_playlist()
                if playlist_name is None or playlist_name is BACK:
                    step = 0
                    continue
                if not playlist_name:
                    step = 0
                    continue
            elif method == "name":
                playlist_name = ask("  Playlist name")
                if playlist_name is None:
                    return
                if playlist_name is BACK or not playlist_name:
                    step = 0
                    continue
            elif method == "url":
                playlist_url = ask("  Playlist URL")
                if playlist_url is None:
                    return
                if playlist_url is BACK or not playlist_url:
                    step = 0
                    continue
            step = 2

        elif step == 2:
            if playlist_name:
                safe_name = playlist_name.lower().replace(" ", "_")
                safe_name = "".join(c for c in safe_name if c.isalnum() or c == "_")
                default_output = f"./output/{safe_name}.xlsx"
            else:
                default_output = "./output/playlist.xlsx"

            output = ask("  Output file", default_output)
            if output is None:
                return
            if output is BACK:
                step = 1
                continue
            step = 3

        elif step == 3:
            metadata = yes_no("  Look up release year and genre via MusicBrainz?")
            if metadata is None:
                return
            if metadata is BACK:
                step = 2
                continue
            step = 4

        elif step == 4:
            args = _make_args(
                command="export",
                playlist=playlist_name,
                url=playlist_url,
                output=output,
                metadata=metadata,
                update=False,
                list_playlists=False,
            )
            from main import cmd_export
            asyncio.run(cmd_export(args))
            pause()
            return


def menu_update_existing():
    """Update an existing spreadsheet with new songs."""
    step = 0
    xlsx = None
    method = None
    playlist_name = None
    playlist_url = None
    metadata = None

    while True:
        if step == 0:
            print("\n  Select the spreadsheet to update:\n")
            xlsx = pick_spreadsheet()
            if xlsx is None or xlsx is BACK:
                return
            step = 1

        elif step == 1:
            print(f"\n  Updating: {xlsx}")

            # If this spreadsheet remembers which Amazon playlist it came from,
            # offer to just use that and skip the method/name/URL prompts.
            from spreadsheet_tags import get_source
            source = get_source(xlsx)
            if source.get("url") or source.get("name"):
                shown = source.get("name") or source.get("url")
                print(f"\n  Source on record: {shown}")
                use = yes_no("  Update from this source?", default=True)
                if use is None:
                    return
                if use is BACK:
                    step = 0
                    continue
                if use:
                    playlist_url = source.get("url") or None
                    playlist_name = source.get("name") or None if not playlist_url else None
                    method = "stored"
                    step = 3
                    continue

            print("  How do you want to identify the playlist?\n")
            method = pick("  Choose: ", [
                ("Browse my playlists (pick from a list)", "list"),
                ("Enter playlist name", "name"),
                ("Enter playlist URL", "url"),
            ])
            if method is None:
                return
            if method is BACK:
                step = 0
                continue
            playlist_name = None
            playlist_url = None
            step = 2

        elif step == 2:
            if method == "list":
                playlist_name = _browse_and_pick_playlist()
                if playlist_name is None or playlist_name is BACK or not playlist_name:
                    step = 1
                    continue
            elif method == "name":
                playlist_name = ask("  Playlist name")
                if playlist_name is None:
                    return
                if playlist_name is BACK or not playlist_name:
                    step = 1
                    continue
            elif method == "url":
                playlist_url = ask("  Playlist URL")
                if playlist_url is None:
                    return
                if playlist_url is BACK or not playlist_url:
                    step = 1
                    continue
            step = 3

        elif step == 3:
            metadata = yes_no("  Look up metadata for new songs?")
            if metadata is None:
                return
            if metadata is BACK:
                step = 2
                continue
            step = 4

        elif step == 4:
            args = _make_args(
                command="export",
                playlist=playlist_name,
                url=playlist_url,
                output=xlsx,
                metadata=metadata,
                update=True,
                list_playlists=False,
            )
            from main import cmd_export
            asyncio.run(cmd_export(args))
            pause()
            return


def menu_metadata():
    """Add/update metadata on an existing spreadsheet."""
    step = 0
    xlsx = None
    force_dates = None

    while True:
        if step == 0:
            print("\n  Select a spreadsheet to enrich with metadata:\n")
            xlsx = pick_spreadsheet()
            if xlsx is None or xlsx is BACK:
                return
            step = 1

        elif step == 1:
            force_dates = yes_no("  Re-lookup all release dates? (use if dates look wrong)", default=False)
            if force_dates is None:
                return
            if force_dates is BACK:
                step = 0
                continue
            step = 2

        elif step == 2:
            args = _make_args(command="metadata", file=xlsx, force_dates=force_dates)
            from main import cmd_metadata
            cmd_metadata(args)
            pause()
            return


def menu_check():
    """Check a spreadsheet for missing metadata."""
    print("\n  Select a spreadsheet to check:\n")
    xlsx = pick_spreadsheet()
    if xlsx is None or xlsx is BACK:
        return

    args = _make_args(command="check", file=xlsx)
    from main import cmd_check
    cmd_check(args)
    pause()


def menu_duplicates():
    """Find and remove duplicate songs."""
    step = 0
    xlsx = None
    reset = None
    amazon = None
    method = None
    playlist_name = None
    playlist_url = None

    while True:
        if step == 0:
            print("\n  Select a spreadsheet to scan for duplicates:\n")
            xlsx = pick_spreadsheet()
            if xlsx is None or xlsx is BACK:
                return
            step = 1

        elif step == 1:
            reset = yes_no("  Reset previously dismissed duplicates?", default=False)
            if reset is None:
                return
            if reset is BACK:
                step = 0
                continue
            step = 2

        elif step == 2:
            amazon = yes_no("  Also remove duplicates from Amazon Music playlist?", default=False)
            if amazon is None:
                return
            if amazon is BACK:
                step = 1
                continue
            playlist_name = None
            playlist_url = None
            step = 3 if amazon else 6

        elif step == 3:
            from playlist_cache import load_cached_playlists
            cached = load_cached_playlists()
            options = []
            if cached:
                options.append(("Choose from cached playlists", "cache"))
            options.append(("Enter playlist name", "name"))
            options.append(("Enter playlist URL", "url"))

            method = pick("\n  Identify the Amazon Music playlist: ", options)
            if method is None:
                return
            if method is BACK:
                step = 2
                continue
            step = 4

        elif step == 4:
            from playlist_cache import load_cached_playlists
            cached = load_cached_playlists()
            if method == "cache" and cached:
                print()
                for i, pl in enumerate(cached, 1):
                    print(f"    {i}. {pl['name']}")
                print()
                while True:
                    choice = ask("  Playlist number")
                    if choice is None:
                        return
                    if choice is BACK:
                        step = 3
                        break
                    try:
                        idx = int(choice) - 1
                        if 0 <= idx < len(cached):
                            playlist_url = cached[idx]["url"]
                            print(f"  Selected: {cached[idx]['name']}")
                            step = 6
                            break
                        else:
                            print(f"  Enter 1-{len(cached)}.")
                    except ValueError:
                        print(f"  Enter a number 1-{len(cached)}.")
                continue
            elif method == "name":
                playlist_name = ask("  Playlist name")
                if playlist_name is None:
                    return
                if playlist_name is BACK:
                    step = 3
                    continue
                step = 6
            elif method == "url":
                playlist_url = ask("  Playlist URL")
                if playlist_url is None:
                    return
                if playlist_url is BACK:
                    step = 3
                    continue
                step = 6

        elif step == 6:
            args = _make_args(
                command="duplicates",
                file=xlsx,
                threshold=0.85,
                reset=reset,
                playlist=playlist_name,
                url=playlist_url,
            )
            from main import cmd_duplicates
            cmd_duplicates(args)
            pause()
            return


def menu_filter():
    """Filter a spreadsheet by criteria and create a new playlist."""
    step = 0
    xlsx = None
    songs = []
    genres = []
    years = []
    year_input = ""
    genre_input = ""
    artist_input = ""
    keyword_input = ""
    filtered = []
    action = None

    while True:
        if step == 0:
            print("\n  Select a spreadsheet to filter:\n")
            xlsx = pick_spreadsheet()
            if xlsx is None or xlsx is BACK:
                return

            from openpyxl import load_workbook
            wb = load_workbook(xlsx)
            ws = wb.active
            songs = []
            for row in ws.iter_rows(min_row=2, values_only=False):
                title = row[1].value or ""
                if not title:
                    continue
                songs.append({
                    "row": row[0].row,
                    "title": str(title),
                    "artist": str(row[2].value or ""),
                    "album": str(row[3].value or ""),
                    "year": str(row[4].value or ""),
                    "genre": str(row[5].value or "").lower(),
                })

            print(f"\n  Loaded {len(songs)} songs from {os.path.basename(xlsx)}")

            genres = sorted(set(s["genre"] for s in songs if s["genre"]))
            years = [int(s["year"]) for s in songs if s["year"].isdigit()]
            if years:
                print(f"  Year range: {min(years)} - {max(years)}")
            if genres:
                print(f"  Genres: {', '.join(genres[:15])}")
                if len(genres) > 15:
                    print(f"          ... and {len(genres) - 15} more")
            print("\n  Add filters (leave blank to skip):\n")
            step = 1

        elif step == 1:
            year_input = ask("  Year or range (e.g., '1990', '1990-1999', '1990s')", "")
            if year_input is None:
                return
            if year_input is BACK:
                step = 0
                continue
            step = 2

        elif step == 2:
            genre_input = ask("  Genre (e.g., 'rock', 'pop, rock')", "")
            if genre_input is None:
                return
            if genre_input is BACK:
                step = 1
                continue
            step = 3

        elif step == 3:
            artist_input = ask("  Artist (e.g., 'Taylor Swift')", "")
            if artist_input is None:
                return
            if artist_input is BACK:
                step = 2
                continue
            step = 4

        elif step == 4:
            keyword_input = ask("  Keyword in title/artist/album", "")
            if keyword_input is None:
                return
            if keyword_input is BACK:
                step = 3
                continue

            # Apply filters
            filtered = songs[:]
            if year_input:
                year_start, year_end = _parse_year_range(year_input)
                if year_start and year_end:
                    before = len(filtered)
                    filtered = [s for s in filtered if s["year"].isdigit() and year_start <= int(s["year"]) <= year_end]
                    print(f"    Year filter: {before} -> {len(filtered)} songs")
            if genre_input:
                genre_terms = [g.strip().lower() for g in genre_input.split(",")]
                before = len(filtered)
                filtered = [s for s in filtered if any(term in s["genre"] for term in genre_terms)]
                print(f"    Genre filter: {before} -> {len(filtered)} songs")
            if artist_input:
                artist_lower = artist_input.lower()
                before = len(filtered)
                filtered = [s for s in filtered if artist_lower in s["artist"].lower()]
                print(f"    Artist filter: {before} -> {len(filtered)} songs")
            if keyword_input:
                kw = keyword_input.lower()
                before = len(filtered)
                filtered = [s for s in filtered if kw in s["title"].lower() or kw in s["artist"].lower() or kw in s["album"].lower()]
                print(f"    Keyword filter: {before} -> {len(filtered)} songs")

            if not filtered:
                print("\n  No songs match your filters.")
                if yes_no("  Go back and change filters?"):
                    step = 1
                    continue
                return

            print(f"\n  {len(filtered)} songs match your filters.")
            step = 5

        elif step == 5:
            preview = yes_no("  Preview the results?")
            if preview is None:
                return
            if preview is BACK:
                step = 4
                continue
            if preview:
                print()
                for i, s in enumerate(filtered[:30], 1):
                    print(f"    {i:3}. {s['title']} — {s['artist']} ({s['year']}, {s['genre']})")
                if len(filtered) > 30:
                    print(f"    ... and {len(filtered) - 30} more")
                print()
            step = 6

        elif step == 6:
            print("  What would you like to do with these songs?\n")
            action = pick("  Choose: ", [
                ("Save as a new spreadsheet", "spreadsheet"),
                ("Append to an existing spreadsheet", "append"),
                ("Create as Amazon Music playlist", "playlist"),
                ("Both (new spreadsheet + Amazon playlist)", "both"),
            ])
            if action is None:
                return
            if action is BACK:
                step = 5
                continue
            break  # exit step loop to run action code below
    # --- Action execution (unchanged below) ---

    # Append to existing spreadsheet
    if action == "append":
        # List xlsx files in output directory
        items = list_spreadsheets_with_tags()
        xlsx_files = [name for name, _, _ in items]
        if items:
            print("\n  Available spreadsheets:")
            print_spreadsheet_list(items)

        target_name = ask("  Spreadsheet to append to")
        if target_name is None or target_name is BACK or not target_name:
            return
        # Auto-complete path and extension
        if not os.path.sep in target_name and "/" not in target_name:
            if not target_name.endswith(".xlsx"):
                target_name += ".xlsx"
            target_path = os.path.join("./output", target_name)
        else:
            target_path = target_name
            if not target_path.endswith(".xlsx"):
                target_path += ".xlsx"

        if not os.path.exists(target_path):
            print(f"\n  File not found: {target_path}")
            pause()
            return

        from openpyxl import load_workbook
        from utils import normalize

        wb = load_workbook(target_path)
        ws = wb.active

        # Read existing songs to avoid duplicates
        existing = set()
        for row in ws.iter_rows(min_row=2, values_only=False):
            title = row[1].value or ""
            artist = row[2].value or ""
            if title:
                existing.add((normalize(str(title)), normalize(str(artist))))

        # Find new songs not already in the spreadsheet
        new_songs = []
        for s in filtered:
            key = (normalize(s["title"]), normalize(s["artist"]))
            if key not in existing:
                new_songs.append(s)

        if not new_songs:
            print(f"\n  All {len(filtered)} songs already exist in the spreadsheet — nothing to add.")
            pause()
            return

        # Append new songs
        next_row = ws.max_row + 1
        for i, s in enumerate(new_songs):
            row_idx = next_row + i
            ws.cell(row=row_idx, column=1, value=row_idx - 1)
            ws.cell(row=row_idx, column=2, value=s["title"])
            ws.cell(row=row_idx, column=3, value=s["artist"])
            ws.cell(row=row_idx, column=4, value=s["album"])
            ws.cell(row=row_idx, column=5, value=s["year"])
            ws.cell(row=row_idx, column=6, value=s["genre"])

        # Renumber all rows
        for i, row in enumerate(ws.iter_rows(min_row=2, max_col=1), start=1):
            row[0].value = i

        wb.save(target_path)
        print(f"\n  Appended {len(new_songs)} new songs to {os.path.abspath(target_path)}")
        print(f"  ({len(filtered) - len(new_songs)} duplicates skipped, {ws.max_row - 1} total songs)")

    # Save as new spreadsheet
    if action in ("spreadsheet", "both"):
        default_name = "filtered_playlist"
        out_name = ask("  Output file name", default_name)
        if out_name is None or out_name is BACK or not out_name:
            return
        # Auto-complete path and extension if user just gives a name
        if not os.path.sep in out_name and "/" not in out_name:
            if not out_name.endswith(".xlsx"):
                out_name += ".xlsx"
            out_path = os.path.join("./output", out_name)
        else:
            out_path = out_name
            if not out_path.endswith(".xlsx"):
                out_path += ".xlsx"

        from openpyxl import Workbook
        from openpyxl.styles import Font

        new_wb = Workbook()
        new_ws = new_wb.active
        new_ws.title = "Filtered Playlist"

        headers = ["#", "Song Name", "Artist", "Album", "Release Year", "Genre"]
        bold = Font(bold=True)
        for col, h in enumerate(headers, 1):
            cell = new_ws.cell(row=1, column=col, value=h)
            cell.font = bold

        for i, s in enumerate(filtered, 1):
            new_ws.cell(row=i + 1, column=1, value=i)
            new_ws.cell(row=i + 1, column=2, value=s["title"])
            new_ws.cell(row=i + 1, column=3, value=s["artist"])
            new_ws.cell(row=i + 1, column=4, value=s["album"])
            new_ws.cell(row=i + 1, column=5, value=s["year"])
            new_ws.cell(row=i + 1, column=6, value=s["genre"])

        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        new_wb.save(out_path)
        print(f"\n  Saved {len(filtered)} songs to {os.path.abspath(out_path)}")
        from spreadsheet_tags import set_tag, TAG_LOCAL
        set_tag(out_path, TAG_LOCAL)

    # Create Amazon Music playlist
    if action in ("playlist", "both"):
        pl_name = ask("  Amazon Music playlist name")
        if pl_name is None or pl_name is BACK:
            return
        if pl_name:
            print(f"\n  Creating Amazon Music playlist '{pl_name}' with {len(filtered)} songs...")

            from models import Song
            from browser import BrowserManager
            from auth import ensure_logged_in
            from importer import create_playlist_from_songs

            song_objs = [Song(title=s["title"], artist=s["artist"], album=s.get("album", ""))
                         for s in filtered]

            async def _create():
                async with BrowserManager(headless=False, slow_mo=100) as bm:
                    await ensure_logged_in(bm.page)
                    await create_playlist_from_songs(bm.page, song_objs, pl_name, auto_threshold=90)

            asyncio.run(_create())

            # If we also saved a spreadsheet, update its tag to Amazon Music
            if action == "both" and out_path and os.path.exists(out_path):
                from spreadsheet_tags import set_tag, TAG_AMAZON
                set_tag(out_path, TAG_AMAZON)

    pause()


def _parse_year_range(text: str) -> tuple[int | None, int | None]:
    """Parse user input into a year range.

    Supports: '1990', '1990-1999', '1990s', '90s', 'before 2000', 'after 1980'
    """
    text = text.strip().lower()

    # "1990s" or "90s" -> 1990-1999
    if text.endswith("s") and text[:-1].isdigit():
        decade = int(text[:-1])
        if decade < 100:
            decade = 1900 + decade if decade >= 30 else 2000 + decade
        return decade, decade + 9

    # "1990-1999" or "1990 - 1999"
    if "-" in text:
        parts = text.split("-")
        if len(parts) == 2:
            try:
                return int(parts[0].strip()), int(parts[1].strip())
            except ValueError:
                pass

    # "before 2000"
    if text.startswith("before "):
        try:
            return 0, int(text[7:].strip()) - 1
        except ValueError:
            pass

    # "after 1980"
    if text.startswith("after "):
        try:
            return int(text[6:].strip()) + 1, 9999
        except ValueError:
            pass

    # Single year "1990"
    if text.isdigit():
        y = int(text)
        return y, y

    return None, None


def menu_create():
    """Create a new Amazon Music playlist from a file or spreadsheet."""
    step = 0
    source = None
    input_path = None
    pl_option = None
    skip_create = False
    name = None

    while True:
        if step == 0:
            print("\n  Select input source:\n")
            source = pick("  Choose: ", [
                ("Pick an existing spreadsheet", "xlsx"),
                ("Enter path to CSV file", "csv"),
            ])
            if source is None or source is BACK:
                return
            step = 1

        elif step == 1:
            if source == "xlsx":
                print()
                input_path = pick_spreadsheet()
                if input_path is None or input_path is BACK:
                    step = 0
                    continue
            else:
                input_path = ask("  Path to CSV file")
                if input_path is None:
                    return
                if input_path is BACK:
                    step = 0
                    continue
                if not input_path or not os.path.exists(input_path):
                    print(f"  File not found: {input_path}")
                    continue
            step = 2

        elif step == 2:
            print("\n  Playlist options:\n")
            pl_option = pick("  Choose: ", [
                ("Create a new playlist", "new"),
                ("Add to an existing playlist", "existing"),
            ])
            if pl_option is None:
                return
            if pl_option is BACK:
                step = 1
                continue
            skip_create = (pl_option == "existing")
            step = 3

        elif step == 3:
            if pl_option == "existing":
                from playlist_cache import load_cached_playlists
                cached = load_cached_playlists()
                if cached:
                    print("\n  Select playlist:\n")
                    options = [(pl["name"], pl["name"]) for pl in cached]
                    name = pick("  Choose: ", options)
                    if name is None:
                        return
                    if name is BACK:
                        step = 2
                        continue
                else:
                    name = ask("  Playlist name (must already exist in Amazon Music)")
                    if name is None:
                        return
                    if name is BACK:
                        step = 2
                        continue
                    if not name:
                        continue
            else:
                name = ask("  Name for the new playlist")
                if name is None:
                    return
                if name is BACK:
                    step = 2
                    continue
                if not name:
                    continue
            break  # exit loop and run

    # Load songs and run
    from models import Song
    if input_path.lower().endswith(".xlsx"):
        from openpyxl import load_workbook
        wb = load_workbook(input_path)
        ws = wb.active
        songs = []
        for row in ws.iter_rows(min_row=2, values_only=False):
            title = row[1].value or ""
            artist = row[2].value or ""
            if title and artist:
                songs.append(Song(
                    title=str(title),
                    artist=str(artist),
                    album=str(row[3].value or ""),
                ))
        if not songs:
            print("  No songs found in spreadsheet.")
            pause()
            return
        print(f"\n  Loaded {len(songs)} songs from {input_path}")
    else:
        from importer import read_csv
        songs = read_csv(input_path)
        if not songs:
            print("  No songs found in CSV.")
            pause()
            return
        print(f"\n  Loaded {len(songs)} songs from {input_path}")

    from browser import BrowserManager
    from auth import ensure_logged_in
    from importer import create_playlist_from_songs

    async def _run():
        async with BrowserManager() as bm:
            await ensure_logged_in(bm.page)
            await create_playlist_from_songs(
                bm.page,
                songs=songs,
                playlist_name=name,
                auto_threshold=90,
                skip_create=skip_create,
            )

    asyncio.run(_run())

    # Tag the source spreadsheet as Amazon Music and record the update time.
    if input_path.lower().endswith(".xlsx") and os.path.exists(input_path):
        from spreadsheet_tags import set_tag, set_updated, TAG_AMAZON
        set_tag(input_path, TAG_AMAZON)
        set_updated(input_path)

    pause()


def menu_list_playlists():
    """List all playlists — from cache or by fetching."""
    from playlist_cache import load_cached_playlists
    cached = load_cached_playlists()

    if not cached:
        print("\n  No cached playlists.")
        _refresh_playlists()
        pause()
        return

    sort_alpha = False
    while True:
        display = sorted(cached, key=lambda p: p["name"].lower()) if sort_alpha else cached
        order_label = "alphabetical" if sort_alpha else "Amazon Music order"
        print(f"\n  {len(display)} playlists (cached, {order_label}):\n")
        for i, pl in enumerate(display, 1):
            print(f"    {i}. {pl['name']}")
        print()

        choice = pick("  Options: ", [
            (f"Sort {'by Amazon Music order' if sort_alpha else 'alphabetically'}", "toggle"),
            ("Refresh from Amazon Music", "refresh"),
            ("Back to main menu", "back"),
        ])
        if choice is None or choice is BACK or choice == "back":
            return
        if choice == "toggle":
            sort_alpha = not sort_alpha
        elif choice == "refresh":
            _refresh_playlists()
            cached = load_cached_playlists()
            if not cached:
                return


def menu_merge_spreadsheets():
    """Append one spreadsheet's songs to another, skipping duplicates."""
    print("\n  Merge spreadsheets: append songs from one file into another.\n")

    # List available spreadsheets
    items = list_spreadsheets_with_tags()
    xlsx_files = [name for name, _, _ in items]

    if len(xlsx_files) < 2:
        print("  Need at least 2 spreadsheets in ./output to merge.")
        pause()
        return

    print("  Available spreadsheets:")
    print_spreadsheet_list(items)

    def _resolve_spreadsheet(prompt):
        """Accept a number from the list or a filename. Returns path, None, or BACK."""
        raw = ask(prompt)
        if raw is None:
            return None
        if raw is BACK:
            return BACK
        if not raw:
            return None
        # Check if it's a number referencing the list
        try:
            idx = int(raw)
            if 1 <= idx <= len(items):
                return items[idx - 1][1]  # return path
        except ValueError:
            pass
        # Otherwise treat as a filename
        if not os.path.sep in raw and "/" not in raw:
            if not raw.endswith(".xlsx"):
                raw += ".xlsx"
            return os.path.join("./output", raw)
        return raw

    # Step-based flow so user can go back
    step = 0
    target_path = None
    source_path = None
    while True:
        if step == 0:
            target_path = _resolve_spreadsheet("  Target spreadsheet (to add songs INTO)")
            if target_path is None or target_path is BACK:
                return
            if not os.path.exists(target_path):
                print(f"\n  File not found: {target_path}")
                continue
            step = 1
        elif step == 1:
            source_path = _resolve_spreadsheet("  Source spreadsheet (to copy songs FROM)")
            if source_path is None:
                return
            if source_path is BACK:
                step = 0
                continue
            if not os.path.exists(source_path):
                print(f"\n  File not found: {source_path}")
                continue
            if os.path.abspath(target_path) == os.path.abspath(source_path):
                print("\n  Source and target are the same file!")
                continue
            break

    from openpyxl import load_workbook
    from utils import normalize

    # Read target songs
    target_wb = load_workbook(target_path)
    target_ws = target_wb.active

    existing = set()
    for row in target_ws.iter_rows(min_row=2, values_only=False):
        title = row[1].value or ""
        artist = row[2].value or ""
        if title:
            existing.add((normalize(str(title)), normalize(str(artist))))

    # Read source songs
    source_wb = load_workbook(source_path)
    source_ws = source_wb.active

    source_songs = []
    for row in source_ws.iter_rows(min_row=2, values_only=False):
        title = row[1].value or ""
        artist = row[2].value or ""
        if not title:
            continue
        source_songs.append({
            "title": str(title),
            "artist": str(artist),
            "album": str(row[3].value or ""),
            "year": str(row[4].value or ""),
            "genre": str(row[5].value or ""),
        })

    # Find new songs
    new_songs = []
    for s in source_songs:
        key = (normalize(s["title"]), normalize(s["artist"]))
        if key not in existing:
            new_songs.append(s)

    if not new_songs:
        print(f"\n  All {len(source_songs)} songs from source already exist in target — nothing to add.")
        pause()
        return

    print(f"\n  Source: {len(source_songs)} songs")
    print(f"  Target: {len(existing)} songs")
    print(f"  New songs to add: {len(new_songs)} ({len(source_songs) - len(new_songs)} duplicates skipped)")

    proceed = yes_no("\n  Proceed?")
    if proceed is None or proceed is BACK or not proceed:
        return

    # Append
    next_row = target_ws.max_row + 1
    for i, s in enumerate(new_songs):
        row_idx = next_row + i
        target_ws.cell(row=row_idx, column=1, value=row_idx - 1)
        target_ws.cell(row=row_idx, column=2, value=s["title"])
        target_ws.cell(row=row_idx, column=3, value=s["artist"])
        target_ws.cell(row=row_idx, column=4, value=s["album"])
        target_ws.cell(row=row_idx, column=5, value=s["year"])
        target_ws.cell(row=row_idx, column=6, value=s["genre"])

    # Renumber
    for i, row in enumerate(target_ws.iter_rows(min_row=2, max_col=1), start=1):
        row[0].value = i

    target_wb.save(target_path)
    print(f"\n  Appended {len(new_songs)} songs to {os.path.abspath(target_path)}")
    print(f"  Total: {target_ws.max_row - 1} songs")
    pause()


def menu_combine_spreadsheets():
    """Combine two spreadsheets into a brand new spreadsheet."""
    print("\n  Combine spreadsheets: merge two files into a new one.\n")

    items = list_spreadsheets_with_tags()
    xlsx_files = [name for name, _, _ in items]

    if len(xlsx_files) < 2:
        print("  Need at least 2 spreadsheets in ./output to combine.")
        pause()
        return

    print("  Available spreadsheets:")
    print_spreadsheet_list(items)

    def _resolve(prompt):
        raw = ask(prompt)
        if raw is None:
            return None
        if raw is BACK:
            return BACK
        if not raw:
            return None
        try:
            idx = int(raw)
            if 1 <= idx <= len(items):
                return items[idx - 1][1]  # return path
        except ValueError:
            pass
        if not os.path.sep in raw and "/" not in raw:
            if not raw.endswith(".xlsx"):
                raw += ".xlsx"
            return os.path.join("./output", raw)
        return raw

    # Step-based flow
    step = 0
    path1 = None
    path2 = None
    out_path = None
    while True:
        if step == 0:
            path1 = _resolve("  First spreadsheet")
            if path1 is None or path1 is BACK:
                return
            if not os.path.exists(path1):
                print(f"\n  File not found: {path1}")
                continue
            step = 1
        elif step == 1:
            path2 = _resolve("  Second spreadsheet")
            if path2 is None:
                return
            if path2 is BACK:
                step = 0
                continue
            if not os.path.exists(path2):
                print(f"\n  File not found: {path2}")
                continue
            step = 2
        elif step == 2:
            out_name = ask("  Name for the new combined spreadsheet")
            if out_name is None:
                return
            if out_name is BACK:
                step = 1
                continue
            if not out_name:
                continue
            if not os.path.sep in out_name and "/" not in out_name:
                if not out_name.endswith(".xlsx"):
                    out_name += ".xlsx"
                out_path = os.path.join("./output", out_name)
            else:
                out_path = out_name
                if not out_path.endswith(".xlsx"):
                    out_path += ".xlsx"
            break

    from openpyxl import load_workbook, Workbook
    from openpyxl.styles import Font
    from utils import normalize

    # Read songs from both files
    all_songs = []
    seen = set()

    for path in [path1, path2]:
        wb = load_workbook(path)
        ws = wb.active
        count = 0
        for row in ws.iter_rows(min_row=2, values_only=False):
            title = row[1].value or ""
            artist = row[2].value or ""
            if not title:
                continue
            key = (normalize(str(title)), normalize(str(artist)))
            if key not in seen:
                seen.add(key)
                all_songs.append({
                    "title": str(title),
                    "artist": str(artist),
                    "album": str(row[3].value or ""),
                    "year": str(row[4].value or ""),
                    "genre": str(row[5].value or ""),
                })
                count += 1
        print(f"  Read {count} unique songs from {os.path.basename(path)}")

    if not all_songs:
        print("\n  No songs found in either file.")
        pause()
        return

    # Create new workbook
    new_wb = Workbook()
    new_ws = new_wb.active
    new_ws.title = "Combined Playlist"

    headers = ["#", "Song Name", "Artist", "Album", "Release Year", "Genre"]
    bold = Font(bold=True)
    for col, h in enumerate(headers, 1):
        cell = new_ws.cell(row=1, column=col, value=h)
        cell.font = bold

    for i, s in enumerate(all_songs, 1):
        new_ws.cell(row=i + 1, column=1, value=i)
        new_ws.cell(row=i + 1, column=2, value=s["title"])
        new_ws.cell(row=i + 1, column=3, value=s["artist"])
        new_ws.cell(row=i + 1, column=4, value=s["album"])
        new_ws.cell(row=i + 1, column=5, value=s["year"])
        new_ws.cell(row=i + 1, column=6, value=s["genre"])

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    new_wb.save(out_path)
    print(f"\n  Combined {len(all_songs)} unique songs into {os.path.abspath(out_path)}")
    from spreadsheet_tags import set_tag, TAG_LOCAL
    set_tag(out_path, TAG_LOCAL)
    pause()


def menu_curate_songs():
    """Browse an existing spreadsheet and hand-pick songs into a new/existing file."""
    step = 0
    source_path = None
    action = None
    dest_path = None
    dest_is_new = False

    while True:
        if step == 0:
            print("\n  Select the source spreadsheet to browse:\n")
            source_path = pick_spreadsheet()
            if source_path is None or source_path is BACK:
                return
            step = 1

        elif step == 1:
            print("\n  Where should selected songs go?\n")
            action = pick("  Choose: ", [
                ("Save to a new spreadsheet", "new"),
                ("Append to an existing spreadsheet", "append"),
            ])
            if action is None:
                return
            if action is BACK:
                step = 0
                continue
            step = 2

        elif step == 2:
            if action == "new":
                out_name = ask("  Name for the new spreadsheet")
                if out_name is None:
                    return
                if out_name is BACK:
                    step = 1
                    continue
                if not out_name:
                    continue
                if not os.path.sep in out_name and "/" not in out_name:
                    if not out_name.endswith(".xlsx"):
                        out_name += ".xlsx"
                    dest_path = os.path.join("./output", out_name)
                else:
                    dest_path = out_name
                    if not dest_path.endswith(".xlsx"):
                        dest_path += ".xlsx"
                dest_is_new = True
            else:
                print("\n  Select the spreadsheet to append to:\n")
                dest_path = pick_spreadsheet()
                if dest_path is None:
                    return
                if dest_path is BACK:
                    step = 1
                    continue
                if dest_path == source_path:
                    print("  Destination must be different from source.")
                    continue
                dest_is_new = False
            break

    # Run the curator
    _curate_from_source(source_path, dest_path, dest_is_new)
    pause()


def _curate_from_source(source_path, dest_path, dest_is_new):
    """Interactive pagination through source songs; write selected ones to dest."""
    from openpyxl import load_workbook, Workbook
    from openpyxl.styles import Font
    from utils import normalize
    from spreadsheet_tags import set_tag, TAG_LOCAL

    # Load source songs
    wb = load_workbook(source_path)
    ws = wb.active
    songs = []
    for row in ws.iter_rows(min_row=2, values_only=False):
        title = row[1].value or ""
        if not title:
            continue
        songs.append({
            "title": str(title),
            "artist": str(row[2].value or "") if len(row) > 2 else "",
            "album": str(row[3].value or "") if len(row) > 3 else "",
            "year": str(row[4].value or "") if len(row) > 4 else "",
            "genre": str(row[5].value or "") if len(row) > 5 else "",
        })

    if not songs:
        print(f"  {os.path.basename(source_path)} has no songs.")
        return

    # If appending, load existing keys to skip duplicates automatically
    existing_keys = set()
    if not dest_is_new and os.path.exists(dest_path):
        dwb = load_workbook(dest_path)
        dws = dwb.active
        for r in dws.iter_rows(min_row=2, values_only=False):
            t = str(r[1].value or "")
            a = str(r[2].value or "") if len(r) > 2 else ""
            if t:
                existing_keys.add((normalize(t), normalize(a)))

    selected = []     # list of song dicts
    selected_keys = set()  # normalized keys to dedupe selections
    page_size = 20
    page = 0
    total_pages = (len(songs) + page_size - 1) // page_size

    while True:
        start = page * page_size
        end = min(start + page_size, len(songs))

        print(f"\n  {os.path.basename(source_path)} — page {page + 1}/{total_pages}")
        print(f"  Selected so far: {len(selected)}\n")
        for i in range(start, end):
            s = songs[i]
            marker = "*" if (normalize(s["title"]), normalize(s["artist"])) in selected_keys else " "
            dup = " [already in dest]" if (normalize(s["title"]), normalize(s["artist"])) in existing_keys else ""
            extras = []
            if s["year"]: extras.append(s["year"])
            if s["genre"]: extras.append(s["genre"])
            tail = f"  ({', '.join(extras)})" if extras else ""
            print(f"    {marker} {i + 1:4}. {s['title']} — {s['artist']}{tail}{dup}")

        print()
        print("  Commands:")
        print("    <numbers>  — toggle songs (e.g. '1,3,5-8')")
        print("    n / p      — next / previous page")
        print("    f          — finish and save selections")
        print("    c          — cancel without saving")
        print("    a          — select ALL on this page")
        print("    u          — unselect ALL on this page")
        print()

        raw = input("  > ").strip().lower()

        if raw in ("q", "quit"):
            return
        if raw in ("c", "cancel"):
            return
        if raw in ("f", "finish", "done"):
            break
        if raw in ("n", "next"):
            if page + 1 < total_pages:
                page += 1
            else:
                print("  Last page.")
            continue
        if raw in ("p", "prev", "previous", "b", "back"):
            if page > 0:
                page -= 1
            else:
                print("  First page.")
            continue
        if raw == "a":
            for i in range(start, end):
                s = songs[i]
                k = (normalize(s["title"]), normalize(s["artist"]))
                if k not in selected_keys:
                    selected_keys.add(k)
                    selected.append(s)
            continue
        if raw == "u":
            for i in range(start, end):
                s = songs[i]
                k = (normalize(s["title"]), normalize(s["artist"]))
                if k in selected_keys:
                    selected_keys.discard(k)
                    selected = [x for x in selected if
                                (normalize(x["title"]), normalize(x["artist"])) != k]
            continue

        # Parse number list (support ranges like 5-8)
        indices = set()
        parse_error = False
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                try:
                    a, b = part.split("-", 1)
                    a_idx, b_idx = int(a), int(b)
                    if a_idx > b_idx:
                        a_idx, b_idx = b_idx, a_idx
                    for n in range(a_idx, b_idx + 1):
                        indices.add(n - 1)
                except ValueError:
                    parse_error = True
                    break
            else:
                try:
                    indices.add(int(part) - 1)
                except ValueError:
                    parse_error = True
                    break

        if parse_error or not indices:
            print("  Invalid input. Try '1,3,5-8' or a command letter.")
            continue

        for i in indices:
            if not (0 <= i < len(songs)):
                continue
            s = songs[i]
            k = (normalize(s["title"]), normalize(s["artist"]))
            if k in selected_keys:
                selected_keys.discard(k)
                selected = [x for x in selected if
                            (normalize(x["title"]), normalize(x["artist"])) != k]
            else:
                selected_keys.add(k)
                selected.append(s)

    if not selected:
        print("\n  Nothing selected — done.")
        return

    # Filter out duplicates against destination
    to_write = [s for s in selected
                if (normalize(s["title"]), normalize(s["artist"])) not in existing_keys]
    skipped_dups = len(selected) - len(to_write)

    if not to_write:
        print(f"\n  All {len(selected)} selected songs already exist in the destination.")
        return

    if dest_is_new:
        new_wb = Workbook()
        new_ws = new_wb.active
        new_ws.title = "Curated Playlist"[:31]
        headers = ["#", "Song Name", "Artist", "Album", "Release Year", "Genre"]
        bold = Font(bold=True)
        for col, h in enumerate(headers, 1):
            cell = new_ws.cell(row=1, column=col, value=h)
            cell.font = bold
        for i, s in enumerate(to_write, 1):
            new_ws.cell(row=i + 1, column=1, value=i)
            new_ws.cell(row=i + 1, column=2, value=s["title"])
            new_ws.cell(row=i + 1, column=3, value=s["artist"])
            new_ws.cell(row=i + 1, column=4, value=s["album"])
            new_ws.cell(row=i + 1, column=5, value=s["year"])
            new_ws.cell(row=i + 1, column=6, value=s["genre"])
        os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
        new_wb.save(dest_path)
        set_tag(dest_path, TAG_LOCAL)
        print(f"\n  Saved {len(to_write)} song(s) to {os.path.abspath(dest_path)}")
    else:
        dwb = load_workbook(dest_path)
        dws = dwb.active
        next_row = dws.max_row + 1
        for i, s in enumerate(to_write):
            r = next_row + i
            dws.cell(row=r, column=1, value=r - 1)
            dws.cell(row=r, column=2, value=s["title"])
            dws.cell(row=r, column=3, value=s["artist"])
            dws.cell(row=r, column=4, value=s["album"])
            dws.cell(row=r, column=5, value=s["year"])
            dws.cell(row=r, column=6, value=s["genre"])
        for i, row in enumerate(dws.iter_rows(min_row=2, max_col=1), start=1):
            row[0].value = i
        dwb.save(dest_path)
        print(f"\n  Appended {len(to_write)} song(s) to {os.path.abspath(dest_path)}")

    if skipped_dups:
        print(f"  ({skipped_dups} duplicate(s) already in destination were skipped)")


def menu_search_spreadsheets():
    """Search for songs/artists in one spreadsheet or all spreadsheets."""
    step = 0
    scope = None
    target_paths = []
    field = None
    query = None

    while True:
        if step == 0:
            print("\n  Search scope:\n")
            scope = pick("  Choose: ", [
                ("Search a specific spreadsheet", "one"),
                ("Search all spreadsheets", "all"),
            ])
            if scope is None or scope is BACK:
                return
            step = 1

        elif step == 1:
            if scope == "one":
                print("\n  Select spreadsheet to search:\n")
                path = pick_spreadsheet()
                if path is None or path is BACK:
                    step = 0
                    continue
                target_paths = [path]
            else:
                items = list_spreadsheets_with_tags()
                if not items:
                    print("\n  No spreadsheets found in ./output/")
                    pause()
                    return
                target_paths = [p for _, p, _ in items]
            step = 2

        elif step == 2:
            print("\n  Search in which field?\n")
            field = pick("  Choose: ", [
                ("Title", "title"),
                ("Artist", "artist"),
                ("Album", "album"),
                ("Genre", "genre"),
                ("Any field", "any"),
            ])
            if field is None:
                return
            if field is BACK:
                step = 1
                continue
            step = 3

        elif step == 3:
            query = ask("  Search for")
            if query is None:
                return
            if query is BACK:
                step = 2
                continue
            if not query:
                print("  Empty search — try again.")
                continue
            step = 4

        elif step == 4:
            _run_search(target_paths, field, query)
            pause()
            return


def _run_search(paths, field, query):
    """Search for `query` in `field` across the given spreadsheet paths."""
    from openpyxl import load_workbook

    q = query.lower().strip()
    total_matches = 0
    all_results = []  # (path, row_num, song_dict)

    for path in paths:
        try:
            wb = load_workbook(path)
        except Exception as e:
            print(f"  Could not read {path}: {e}")
            continue
        ws = wb.active

        for row in ws.iter_rows(min_row=2, values_only=False):
            title = str(row[1].value or "")
            artist = str(row[2].value or "") if len(row) > 2 else ""
            album = str(row[3].value or "") if len(row) > 3 else ""
            year = str(row[4].value or "") if len(row) > 4 else ""
            genre = str(row[5].value or "") if len(row) > 5 else ""
            if not title:
                continue

            haystack = {
                "title": title.lower(),
                "artist": artist.lower(),
                "album": album.lower(),
                "genre": genre.lower(),
            }

            matched = False
            if field == "any":
                matched = any(q in v for v in haystack.values())
            else:
                matched = q in haystack.get(field, "")

            if matched:
                all_results.append((path, row[0].row, {
                    "title": title,
                    "artist": artist,
                    "album": album,
                    "year": year,
                    "genre": genre,
                }))
                total_matches += 1

    if not all_results:
        print(f"\n  No matches found for '{query}' in {field}.")
        return

    print(f"\n  {total_matches} match(es) found:\n")

    # Group by spreadsheet
    current_path = None
    display_items = []
    for path, row_num, s in all_results:
        display_items.append((path, row_num, s))

    def formatter(i, entry):
        path, row_num, s = entry
        basename = os.path.basename(path)
        extras = []
        if s["year"]:
            extras.append(s["year"])
        if s["genre"]:
            extras.append(s["genre"])
        tail = f"  ({', '.join(extras)})" if extras else ""
        return f"    {i:4}. [{basename} row {row_num}] {s['title']} — {s['artist']}{tail}"

    paged_list(display_items, formatter)


def menu_view_spreadsheets():
    """View the list of spreadsheets and drill into one to see its contents."""
    while True:
        items = list_spreadsheets_with_tags()
        if not items:
            print("\n  No spreadsheets found in ./output/")
            pause()
            return

        print(f"\n  {len(items)} spreadsheet(s):\n")
        print_spreadsheet_list(items)

        raw = ask("  Enter number to view contents (or 'b'/'q' to go back)")
        if raw is None or raw is BACK:
            return
        if not raw:
            return

        # Resolve to path
        path = None
        try:
            idx = int(raw)
            if 1 <= idx <= len(items):
                path = items[idx - 1][1]
        except ValueError:
            pass
        if not path:
            if not os.path.sep in raw and "/" not in raw:
                if not raw.endswith(".xlsx"):
                    raw += ".xlsx"
                path = os.path.join("./output", raw)
            else:
                path = raw

        if not os.path.exists(path):
            print(f"  File not found: {path}\n")
            continue

        _view_spreadsheet_contents(path)


def _view_spreadsheet_contents(path):
    """Show the songs in a spreadsheet with pagination."""
    from openpyxl import load_workbook
    from spreadsheet_tags import get_tag_display

    wb = load_workbook(path)
    ws = wb.active

    name = os.path.basename(path)
    tag = get_tag_display(path)

    songs = []
    for row in ws.iter_rows(min_row=2, values_only=False):
        title = row[1].value or ""
        if not title:
            continue
        songs.append({
            "title": str(title),
            "artist": str(row[2].value or ""),
            "album": str(row[3].value or "") if len(row) > 3 else "",
            "year": str(row[4].value or "") if len(row) > 4 else "",
            "genre": str(row[5].value or "") if len(row) > 5 else "",
        })

    header = f"  {name}  {tag}" if tag else f"  {name}"
    print(f"\n{header}")
    print(f"  {len(songs)} songs\n")

    if not songs:
        pause()
        return

    def formatter(i, s):
        extras = []
        if s["year"]:
            extras.append(s["year"])
        if s["genre"]:
            extras.append(s["genre"])
        tail = f"  ({', '.join(extras)})" if extras else ""
        return f"    {i:4}. {s['title']} — {s['artist']}{tail}"

    paged_list(songs, formatter)
    pause()


def menu_delete_spreadsheet():
    """Delete one or more spreadsheets from ./output/."""
    print("\n  Delete spreadsheets\n")

    items = list_spreadsheets_with_tags()
    if not items:
        print("  No spreadsheets found in ./output/")
        pause()
        return

    print("  Available spreadsheets:")
    print_spreadsheet_list(items)
    print("  ('b' or 'back' to return, 'q' for main menu)\n")

    while True:
        raw = ask("  Spreadsheet to delete (number or name)")
        if raw is None or raw is BACK:
            return

        # Resolve to path
        path = None
        try:
            idx = int(raw)
            if 1 <= idx <= len(items):
                path = items[idx - 1][1]
        except ValueError:
            pass

        if not path:
            if not os.path.sep in raw and "/" not in raw:
                if not raw.endswith(".xlsx"):
                    raw += ".xlsx"
                path = os.path.join("./output", raw)
            else:
                path = raw

        if not os.path.exists(path):
            print(f"  File not found: {path}")
            continue

        name = os.path.basename(path)
        confirm = yes_no(f"  Delete '{name}'? This cannot be undone.", default=False)
        if confirm is None:
            return
        if confirm is BACK:
            continue
        if confirm:
            os.remove(path)
            print(f"  Deleted '{name}'.\n")
            # Refresh the list
            items = list_spreadsheets_with_tags()
            if not items:
                print("  No spreadsheets remaining.")
                break
            print("  Remaining spreadsheets:")
            print_spreadsheet_list(items)
        else:
            print("  Cancelled.\n")

        another = yes_no("  Delete another?", default=False)
        if another is None or another is BACK or not another:
            break

    pause()


def menu_compare_spreadsheets():
    """Compare two spreadsheets and show common/unique songs."""
    print("\n  Compare two spreadsheets side by side.\n")

    items = list_spreadsheets_with_tags()
    xlsx_files = [name for name, _, _ in items]

    if len(xlsx_files) < 2:
        print("  Need at least 2 spreadsheets in ./output to compare.")
        pause()
        return

    print("  Available spreadsheets:")
    print_spreadsheet_list(items)

    def _resolve(prompt):
        raw = ask(prompt)
        if raw is None:
            return None
        if raw is BACK:
            return BACK
        if not raw:
            return None
        try:
            idx = int(raw)
            if 1 <= idx <= len(items):
                return items[idx - 1][1]  # return path
        except ValueError:
            pass
        if not os.path.sep in raw and "/" not in raw:
            if not raw.endswith(".xlsx"):
                raw += ".xlsx"
            return os.path.join("./output", raw)
        return raw

    # Step-based flow
    step = 0
    path1 = None
    path2 = None
    while True:
        if step == 0:
            path1 = _resolve("  First spreadsheet")
            if path1 is None or path1 is BACK:
                return
            if not os.path.exists(path1):
                print(f"\n  File not found: {path1}")
                continue
            step = 1
        elif step == 1:
            path2 = _resolve("  Second spreadsheet")
            if path2 is None:
                return
            if path2 is BACK:
                step = 0
                continue
            if not os.path.exists(path2):
                print(f"\n  File not found: {path2}")
                continue
            break

    from openpyxl import load_workbook
    from utils import normalize

    def _read_songs(path):
        wb = load_workbook(path)
        ws = wb.active
        songs = []
        for row in ws.iter_rows(min_row=2, values_only=False):
            title = str(row[1].value or "")
            artist = str(row[2].value or "")
            album = str(row[3].value or "") if len(row) > 3 else ""
            year = str(row[4].value or "") if len(row) > 4 else ""
            genre = str(row[5].value or "") if len(row) > 5 else ""
            if title:
                songs.append({
                    "title": title,
                    "artist": artist,
                    "album": album,
                    "year": year,
                    "genre": genre,
                    "key": (normalize(title), normalize(artist)),
                })
        return songs

    songs1 = _read_songs(path1)
    songs2 = _read_songs(path2)

    name1 = os.path.basename(path1)
    name2 = os.path.basename(path2)

    keys1 = {s["key"] for s in songs1}
    keys2 = {s["key"] for s in songs2}

    common_keys = keys1 & keys2
    only1_keys = keys1 - keys2
    only2_keys = keys2 - keys1

    # Build lookup for display
    lookup1 = {s["key"]: s for s in songs1}
    lookup2 = {s["key"]: s for s in songs2}

    print(f"\n  {name1}: {len(songs1)} songs")
    print(f"  {name2}: {len(songs2)} songs")
    print()
    print(f"  In common:       {len(common_keys)}")
    print(f"  Only in {name1}: {len(only1_keys)}")
    print(f"  Only in {name2}: {len(only2_keys)}")

    # Show details with pagination
    def _fmt(lookup):
        def formatter(i, key):
            s = lookup[key]
            return f"    {i:3}. {s['title']} — {s['artist']}"
        return formatter

    common_sorted = sorted(common_keys)
    only1_sorted = sorted(only1_keys)
    only2_sorted = sorted(only2_keys)

    if common_keys:
        if yes_no(f"\n  Show {len(common_keys)} songs in common?"):
            print()
            paged_list(common_sorted, _fmt(lookup1))

    if only1_keys:
        if yes_no(f"  Show {len(only1_keys)} songs only in {name1}?"):
            print()
            paged_list(only1_sorted, _fmt(lookup1))

    if only2_keys:
        if yes_no(f"  Show {len(only2_keys)} songs only in {name2}?"):
            print()
            paged_list(only2_sorted, _fmt(lookup2))

    pause()


def menu_play_playlist():
    """Open a playlist in the browser and start playback (straight or shuffle)."""
    print("\n  Play a playlist in the browser.\n")

    name = _browse_and_pick_playlist()
    if name is None or name is BACK:
        return

    from playlist_cache import get_playlist_url
    url = get_playlist_url(name)
    if not url:
        print(f"  Could not find a cached URL for '{name}'. Try refreshing the playlist list.")
        pause()
        return

    mode = pick("  Playback mode: ", [
        ("Straight (playlist order)", "straight"),
        ("Shuffle", "shuffle"),
    ])
    if mode is None or mode is BACK:
        return
    shuffle = (mode == "shuffle")

    from browser import BrowserManager
    from auth import ensure_logged_in
    from scraper import play_playlist

    async def run():
        async with BrowserManager(headless=False, slow_mo=100) as bm:
            await ensure_logged_in(bm.page)
            try:
                await play_playlist(bm.page, playlist_url=url, shuffle=shuffle)
                print(f"\n  Now playing '{name}' ({'shuffle' if shuffle else 'straight'}).")
            except RuntimeError as e:
                print(f"\n  Playback didn't start automatically: {e}")
                print("  The browser is open — you can click Play manually.")
            print("  The browser window must stay open for playback.")
            print("  Press Enter here to stop and close the browser.")
            # Run blocking input in a thread so the event loop (and page) stays alive.
            await asyncio.get_event_loop().run_in_executor(None, input)

    asyncio.run(run())


def menu_play_queue():
    """Queue several playlists and play them back-to-back."""
    print("\n  Play a queue of playlists back-to-back.")
    print("  Each playlist plays to completion; we auto-advance when Amazon Music")
    print("  starts playing a track that's not in the current playlist.\n")

    from playlist_cache import load_cached_playlists, get_playlist_url

    cached = load_cached_playlists()
    if not cached:
        print("  No cached playlists. Refresh the playlist list first.")
        pause()
        return

    queue: list[dict] = []
    name_options = sorted([(pl["name"], pl["name"]) for pl in cached],
                          key=lambda o: o[0].lower())

    while True:
        if queue:
            print("\n  Queue so far:")
            for i, item in enumerate(queue, 1):
                print(f"    {i}. {item['name']}")
        n = len(queue) + 1
        prompt_label = f"  Pick playlist #{n} (or Done): "

        options = list(name_options)
        if len(queue) >= 2:
            options.append(("Done — start playback", "__done__"))

        choice = pick(prompt_label, options)
        if choice is None:
            return
        if choice is BACK:
            if queue:
                removed = queue.pop()
                print(f"  Removed '{removed['name']}' from queue.")
                continue
            return
        if choice == "__done__":
            break

        url = get_playlist_url(choice)
        if not url:
            print(f"  No cached URL for '{choice}' — refresh the playlist list.")
            continue
        queue.append({"name": choice, "url": url})

    mode = pick("\n  Playback mode for the whole queue: ", [
        ("Straight (playlist order)", "straight"),
        ("Shuffle", "shuffle"),
    ])
    if mode is None or mode is BACK:
        return
    shuffle = (mode == "shuffle")

    _play_playlist_queue(queue, shuffle)
    pause()


def _play_playlist_queue(queue: list[dict], shuffle: bool):
    """Open a browser, play each playlist in *queue* to completion, advance."""
    from browser import BrowserManager
    from auth import ensure_logged_in
    from scraper import play_playlist, scrape_playlist, wait_until_off_playlist

    async def run():
        async with BrowserManager(headless=False, slow_mo=100) as bm:
            await ensure_logged_in(bm.page)

            for i, item in enumerate(queue, 1):
                print(f"\n  [{i}/{len(queue)}] '{item['name']}' — scraping tracks...")
                try:
                    playlist = await scrape_playlist(bm.page, playlist_url=item["url"])
                except Exception as e:
                    print(f"  Could not scrape '{item['name']}': {e} — skipping.")
                    continue
                track_set = {
                    (s.title.lower().strip(), s.artist.lower().strip())
                    for s in playlist.songs
                }
                print(f"  {len(track_set)} tracks. Starting playback...")

                try:
                    await play_playlist(bm.page, playlist_url=item["url"], shuffle=shuffle)
                except RuntimeError as e:
                    print(f"  Couldn't start playback for '{item['name']}': {e}")
                    print("  The browser is open — click Play manually, then press Enter here.")
                    await asyncio.get_event_loop().run_in_executor(None, input)

                if i < len(queue):
                    print(f"  Waiting for '{item['name']}' to finish (auto-advance)...")
                    reason = await wait_until_off_playlist(bm.page, track_set)
                    print(f"  Advancing (reason: {reason}).")
                else:
                    print(f"\n  Last playlist in queue. Press Enter here to stop.")
                    await asyncio.get_event_loop().run_in_executor(None, input)

    asyncio.run(run())


def menu_organize():
    """Browse and edit the virtual folder tree of playlists + spreadsheets."""
    import organization as org_mod

    path: list[str] = []  # current folder (list of folder names from root)

    while True:
        org = org_mod.load_org()
        node = org_mod.get_node(org, path)
        if node is None:
            # Parent was deleted mid-navigation; pop to root.
            path = []
            continue

        clear_screen()
        print("=" * 50)
        print("  Organize playlists & spreadsheets")
        print("=" * 50)
        print(f"\n  Folder: {org_mod.path_to_str(path)}\n")

        folders = org_mod.list_folders(org, path)
        items = org_mod.list_items(org, path)

        rows: list[tuple[str, str, object]] = []  # (kind, label, payload)
        for name in folders:
            rows.append(("folder", name, name))
        for item in items:
            rows.append(("item", _format_item_label(item), item))

        if rows:
            print("  Contents:")
            for i, (kind, label, _) in enumerate(rows, 1):
                prefix = "[folder]" if kind == "folder" else "        "
                print(f"    {i:>2}. {prefix} {label}")
            print()
        else:
            print("  (empty)\n")

        print("  Commands:")
        print("    <num>  open folder / act on item")
        print("    n      new subfolder here")
        print("    a      add item (playlist or spreadsheet)")
        if items:
            print("    rm     remove items (multi-select: 1,3,5-8 or 'a')")
        if path:
            print("    r      rename this folder")
            print("    d      delete this folder")
            print("    u      go up one level")
        if any(it.get("kind") == "playlist" for it in items) or folders:
            print("    p      play all playlists in this folder")
            print("    pr     play all playlists here + subfolders")
        print("    q      back to main menu\n")

        raw = input("  > ").strip()
        if not raw:
            continue
        cmd = raw.lower()

        if cmd in ("q", "quit", "exit", "back", "b"):
            return
        if cmd in ("u", "up"):
            if path:
                path.pop()
            continue
        if cmd == "n":
            name = ask("  New folder name: ")
            if not name or name is BACK:
                continue
            try:
                org_mod.create_folder(org, path, name)
                org_mod.save_org(org)
            except ValueError as e:
                print(f"  {e}")
                pause()
            continue
        if cmd == "a":
            _organize_add_item(org, path)
            continue
        if cmd == "rm":
            if not items:
                print("  No items to remove in this folder.")
                pause()
                continue
            # Indices (into `rows`) of each item row, so the user's "3" maps
            # to the same number they see on screen.
            item_row_idxs = [i for i, (k, _, _) in enumerate(rows) if k == "item"]
            print("\n  Enter item numbers to remove (e.g. 1,3,5-8 or 'a' for all).")
            print("  'c' to cancel. Folders are not affected — use 'd' to delete a folder.")
            sel = input("  Remove: ").strip().lower()
            if not sel or sel in ("c", "cancel", "q"):
                continue
            to_remove_rows: set[int] = set()
            if sel == "a":
                to_remove_rows = set(item_row_idxs)
            else:
                try:
                    for part in sel.split(","):
                        part = part.strip()
                        if "-" in part:
                            a_, b_ = part.split("-", 1)
                            lo, hi = int(a_), int(b_)
                            if lo > hi:
                                lo, hi = hi, lo
                            for n in range(lo, hi + 1):
                                to_remove_rows.add(n - 1)
                        elif part:
                            to_remove_rows.add(int(part) - 1)
                except ValueError:
                    print("  Invalid selection.")
                    pause()
                    continue
            # Keep only rows that actually refer to items in the current folder
            to_remove_rows &= set(item_row_idxs)
            if not to_remove_rows:
                print("  Nothing to remove (did you pick folders?).")
                pause()
                continue
            print(f"\n  Will remove {len(to_remove_rows)} item(s):")
            for i in sorted(to_remove_rows):
                print(f"    - {rows[i][1]}")
            confirm = yes_no("  Proceed?", default=True)
            if confirm is not True:
                continue
            # Translate row indices to item-list indices, then pop descending
            # so earlier removals don't shift later indices.
            target_items = [rows[i][2] for i in to_remove_rows]
            removed = 0
            for it in target_items:
                try:
                    idx = node["items"].index(it)
                except ValueError:
                    continue
                node["items"].pop(idx)
                removed += 1
            org_mod.save_org(org)
            print(f"  Removed {removed} item(s).")
            pause()
            continue
        if cmd == "r" and path:
            new_name = ask(f"  Rename '{path[-1]}' to: ")
            if not new_name or new_name is BACK:
                continue
            try:
                org_mod.rename_folder(org, path, new_name)
                org_mod.save_org(org)
                path[-1] = new_name.strip()
            except ValueError as e:
                print(f"  {e}")
                pause()
            continue
        if cmd == "d" and path:
            sub_count = len(org_mod.list_folders(org, path))
            it_count = len(org_mod.list_items(org, path))
            warn = f" (contains {sub_count} subfolders, {it_count} items)" if sub_count or it_count else ""
            confirm = yes_no(f"  Delete '{path[-1]}'{warn}?", default=False)
            if confirm is True:
                try:
                    org_mod.delete_folder(org, path)
                    org_mod.save_org(org)
                    path.pop()
                except ValueError as e:
                    print(f"  {e}")
                    pause()
            continue
        if cmd in ("p", "pr"):
            recursive = (cmd == "pr")
            playlists = (list(org_mod.iter_playlists_recursive(org, path))
                         if recursive
                         else [it for it in items if it.get("kind") == "playlist"])
            if not playlists:
                print("  No playlists to play here.")
                pause()
                continue
            print(f"\n  Found {len(playlists)} playlists:")
            for pl in playlists:
                print(f"    - {pl.get('name')}")
            mode = pick("\n  Playback mode: ", [
                ("Straight (in order)", "straight"),
                ("Shuffle", "shuffle"),
            ])
            if mode is None or mode is BACK:
                continue
            queue = [{"name": pl["name"], "url": pl["url"]} for pl in playlists]
            _play_playlist_queue(queue, shuffle=(mode == "shuffle"))
            pause()
            continue

        if cmd.isdigit():
            idx = int(cmd) - 1
            if 0 <= idx < len(rows):
                kind, _, payload = rows[idx]
                if kind == "folder":
                    path.append(payload)
                else:
                    _organize_item_actions(org, path, payload)
                continue

        print("  Unknown command.")
        pause()


def _format_item_label(item: dict) -> str:
    kind = item.get("kind")
    name = item.get("name", "")
    if kind == "playlist":
        return f"(playlist) {name}"
    if kind == "spreadsheet":
        from spreadsheet_tags import get_tag_display
        path = from_stored_path(item.get("path", ""))
        tag = get_tag_display(path) if path and os.path.exists(path) else ""
        missing = " [missing]" if path and not os.path.exists(path) else ""
        return f"(sheet)    {name or os.path.basename(path)}  {tag}{missing}".rstrip()
    return f"(?)        {name}"


def _organize_add_item(org: dict, path: list[str]):
    """Prompt the user to add a playlist or spreadsheet item to *path*."""
    import organization as org_mod

    kind = pick("\n  Add what? ", [
        ("A playlist (from my cached list)", "playlist"),
        ("A spreadsheet (from ./output/)", "spreadsheet"),
    ])
    if kind is None or kind is BACK:
        return

    if kind == "playlist":
        from playlist_cache import load_cached_playlists
        playlists = load_cached_playlists()
        if not playlists:
            print("  No cached playlists. Run 'Refresh playlist list' first.")
            pause()
            return
        options = sorted([(pl["name"], pl) for pl in playlists], key=lambda o: o[0].lower())
        chosen = pick("  Pick a playlist: ", options)
        if chosen is None or chosen is BACK:
            return
        org_mod.add_item(org, path, {
            "kind": "playlist",
            "url": chosen["url"],
            "name": chosen["name"],
        })
        org_mod.save_org(org)
        print(f"  Added '{chosen['name']}' to {org_mod.path_to_str(path)}")
        pause()
        return

    # spreadsheet
    items = list_spreadsheets_with_tags()
    if not items:
        print("  No spreadsheets in ./output/.")
        pause()
        return
    options = [(f"{name}  {tag}" if tag else name, (name, path_))
               for name, path_, tag in items]
    chosen = pick("  Pick a spreadsheet: ", options)
    if chosen is None or chosen is BACK:
        return
    name, fpath = chosen
    org_mod.add_item(org, path, {
        "kind": "spreadsheet",
        "path": to_stored_path(fpath),
        "name": name,
    })
    org_mod.save_org(org)
    print(f"  Added '{name}' to {org_mod.path_to_str(path)}")
    pause()


def _organize_item_actions(org: dict, path: list[str], item: dict):
    """Show actions for a single item (play / remove / ...)."""
    import organization as org_mod

    label = _format_item_label(item)
    kind = item.get("kind")

    while True:
        print(f"\n  {label}\n")
        options = []
        if kind == "playlist":
            options.append(("Play (straight)", "play-straight"))
            options.append(("Play (shuffle)", "play-shuffle"))
        options.append(("Remove from this folder", "remove"))
        options.append(("Back", "back"))

        choice = pick("  Action: ", options)
        if choice in (None, BACK, "back"):
            return
        if choice == "remove":
            # Find item's current index (user may have moved things between refreshes)
            items = org_mod.list_items(org, path)
            try:
                idx = items.index(item)
            except ValueError:
                print("  Item not found (was it already removed?).")
                pause()
                return
            org_mod.remove_item(org, path, idx)
            org_mod.save_org(org)
            print("  Removed.")
            pause()
            return
        if choice in ("play-straight", "play-shuffle"):
            shuffle = (choice == "play-shuffle")
            from browser import BrowserManager
            from auth import ensure_logged_in
            from scraper import play_playlist

            async def run():
                async with BrowserManager(headless=False, slow_mo=100) as bm:
                    await ensure_logged_in(bm.page)
                    try:
                        await play_playlist(bm.page, playlist_url=item["url"], shuffle=shuffle)
                        print(f"\n  Now playing '{item['name']}' ({'shuffle' if shuffle else 'straight'}).")
                    except RuntimeError as e:
                        print(f"\n  Playback didn't start automatically: {e}")
                        print("  The browser is open — you can click Play manually.")
                    print("  Press Enter here to stop and close the browser.")
                    await asyncio.get_event_loop().run_in_executor(None, input)

            asyncio.run(run())
            pause()
            return


def menu_refresh_playlists():
    """Refresh the cached playlist list from Amazon Music."""
    _refresh_playlists()
    pause()


def _refresh_playlists() -> list[dict]:
    """Open browser, fetch playlists, save to cache, and display."""
    from browser import BrowserManager
    from auth import ensure_logged_in
    from scraper import list_playlists

    print("\n  Opening browser to fetch your playlists...")

    playlists = []

    async def fetch():
        nonlocal playlists
        async with BrowserManager(headless=False, slow_mo=100) as bm:
            await ensure_logged_in(bm.page)
            playlists = await list_playlists(bm.page)

    asyncio.run(fetch())

    if playlists:
        print(f"\n  Found {len(playlists)} playlists:\n")
        for i, pl in enumerate(playlists, 1):
            print(f"    {i}. {pl['name']}")
        print()
    else:
        print("  No playlists found.")

    return playlists


def _browse_and_pick_playlist() -> str | None:
    """Pick a playlist — use cache if available, otherwise open browser."""
    from playlist_cache import load_cached_playlists

    playlists = load_cached_playlists()

    if not playlists:
        print("\n  No cached playlists — fetching from Amazon Music...")
        playlists = _refresh_playlists()

    if not playlists:
        print("  No playlists found.")
        return None

    print(f"\n  {len(playlists)} playlists:\n")
    options = [(pl["name"], pl["name"]) for pl in playlists]
    return pick("  Select playlist: ", options)


class _Args:
    """Fake args namespace to pass to cmd_* functions."""
    def __init__(self, **kwargs):
        # Set defaults for common flags
        self.verbose = False
        self.headless = False
        self.slow_mo = 100
        self.debug = False
        self.output = None  # cmd_metadata falls back to the input path when None
        for k, v in kwargs.items():
            setattr(self, k, v)


def _make_args(**kwargs) -> _Args:
    return _Args(**kwargs)


def main_menu():
    setup_logging(verbose=False)

    while True:
        clear_screen()
        print("=" * 50)
        print("  Amazon Music Playlist Manager")
        print("=" * 50)
        print()
        print("  EXPORT")
        print("    1. Export playlist to spreadsheet")
        print("    2. Update existing spreadsheet (add new songs)")
        print()
        print("  METADATA")
        print("    3. Add year/genre to a spreadsheet")
        print("    4. Check spreadsheet for missing data")
        print()
        print("  MANAGE")
        print("    5. View spreadsheets (list + contents)")
        print("    6. Search spreadsheets")
        print("    7. Find & remove duplicates")
        print("    8. Filter songs & create new playlist")
        print("    9. Curate songs (pick by hand from a spreadsheet)")
        print("   10. Merge spreadsheets (append into existing)")
        print("   11. Combine spreadsheets (into new file)")
        print("   12. Compare two spreadsheets")
        print("   13. Delete spreadsheets")
        print("   14. Create playlist from spreadsheet/CSV")
        print()
        print("  PLAYLISTS")
        print("   15. View my playlists")
        print("   16. Refresh playlist list from Amazon Music")
        print("   17. Play a playlist (straight or shuffle)")
        print("   18. Play a queue of playlists (auto-advance at end)")
        print("   19. Organize playlists & spreadsheets (virtual folders)")
        print()
        print("    0. Exit")
        print("  (At any prompt: 'b'/'back' = previous step, 'q'/'quit' = main menu)")
        print()

        choice = input("  Choose an option: ").strip()

        if choice == "1":
            menu_export_new()
        elif choice == "2":
            menu_update_existing()
        elif choice == "3":
            menu_metadata()
        elif choice == "4":
            menu_check()
        elif choice == "5":
            menu_view_spreadsheets()
        elif choice == "6":
            menu_search_spreadsheets()
        elif choice == "7":
            menu_duplicates()
        elif choice == "8":
            menu_filter()
        elif choice == "9":
            menu_curate_songs()
        elif choice == "10":
            menu_merge_spreadsheets()
        elif choice == "11":
            menu_combine_spreadsheets()
        elif choice == "12":
            menu_compare_spreadsheets()
        elif choice == "13":
            menu_delete_spreadsheet()
        elif choice == "14":
            menu_create()
        elif choice == "15":
            menu_list_playlists()
        elif choice == "16":
            menu_refresh_playlists()
        elif choice == "17":
            menu_play_playlist()
        elif choice == "18":
            menu_play_queue()
        elif choice == "19":
            menu_organize()
        elif choice in ("0", "q", "quit", "exit"):
            print("\n  Goodbye!\n")
            break
        else:
            print("  Invalid option.")
            pause()


if __name__ == "__main__":
    main_menu()
