"""Amazon Music Playlist Manager — CLI entry point.

Usage:
    python main.py export --playlist "My Favorites" --output ./output/favorites.xlsx
    python main.py export --url "https://music.amazon.com/playlists/..." --output ./output/pl.xlsx
    python main.py export --list
    python main.py create --input ./input/songs.csv --name "Road Trip Mix"
    python main.py create --input ./input/songs.csv --name "Road Trip Mix" --threshold 80
    python main.py metadata --file ./output/playlist.xlsx
"""

import argparse
import asyncio
import os
import sys

from utils import setup_logging


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Amazon Music Playlist Manager — export playlists to XLSX or create playlists from CSV.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    parser.add_argument("--headless", action="store_true", help="Run browser headless (skip for first login)")
    parser.add_argument("--slow-mo", type=int, default=100, help="Delay between Playwright actions in ms")
    parser.add_argument("--debug", action="store_true", help="Pause browser for selector inspection")

    sub = parser.add_subparsers(dest="command", required=True)

    # --- export ---
    exp = sub.add_parser("export", help="Export a playlist to XLSX")
    exp_group = exp.add_mutually_exclusive_group(required=True)
    exp_group.add_argument("--playlist", metavar="NAME", help="Playlist name to export")
    exp_group.add_argument("--url", metavar="URL", help="Direct playlist URL")
    exp_group.add_argument("--list", action="store_true", dest="list_playlists", help="List all your playlists")
    exp.add_argument("--output", "-o", metavar="PATH", default="./output/playlist.xlsx",
                     help="Output .xlsx file path (default: ./output/playlist.xlsx)")
    exp.add_argument("--update", action="store_true",
                     help="Update existing spreadsheet — only add new songs, preserve metadata")
    exp.add_argument("--metadata", action="store_true",
                     help="Look up release date and genre via MusicBrainz (~1 sec/song)")

    # --- metadata ---
    meta = sub.add_parser("metadata", help="Add release date and genre to an existing XLSX file")
    meta.add_argument("--file", "-f", metavar="XLSX", required=True,
                      help="Path to existing playlist .xlsx file")
    meta.add_argument("--output", "-o", metavar="PATH",
                      help="Output path (defaults to overwriting the input file)")
    meta.add_argument("--force-dates", action="store_true",
                      help="Re-lookup all release dates (use to fix reissue/remaster dates)")

    # --- check ---
    chk = sub.add_parser("check", help="Check a spreadsheet for missing metadata")
    chk.add_argument("--file", "-f", metavar="XLSX", required=True,
                     help="Path to playlist .xlsx file")

    # --- duplicates ---
    dup = sub.add_parser("duplicates", help="Find and remove duplicate songs")
    dup.add_argument("--file", "-f", metavar="XLSX", required=True,
                     help="Path to playlist .xlsx file")
    dup.add_argument("--url", metavar="URL",
                     help="Amazon Music playlist URL (enables deletion from Amazon Music)")
    dup.add_argument("--playlist", metavar="NAME",
                     help="Amazon Music playlist name (enables deletion from Amazon Music)")
    dup.add_argument("--threshold", type=float, default=0.85,
                     help="Fuzzy match threshold 0.0-1.0 (default: 0.85)")
    dup.add_argument("--reset", action="store_true",
                     help="Clear dismissed duplicates and scan fresh")

    # --- create ---
    crt = sub.add_parser("create", help="Create a playlist from CSV or XLSX")
    crt.add_argument("--input", "-i", metavar="FILE", required=True,
                     help="Path to input file (CSV or XLSX)")
    crt.add_argument("--name", "-n", metavar="NAME", required=True, help="Name for the new playlist")
    crt.add_argument("--threshold", type=int, default=90,
                     help="Auto-select confidence threshold 0-100 (default: 90)")

    return parser


async def cmd_export(args):
    from browser import BrowserManager
    from auth import ensure_logged_in
    from scraper import list_playlists, scrape_playlist
    from exporter import export_to_xlsx

    async with BrowserManager(headless=args.headless, slow_mo=args.slow_mo) as bm:
        await ensure_logged_in(bm.page)

        if args.list_playlists:
            playlists = await list_playlists(bm.page, debug=args.debug)
            if not playlists:
                print("No playlists found.")
                return
            print(f"\nFound {len(playlists)} playlists:\n")
            for i, pl in enumerate(playlists, 1):
                print(f"  {i}. {pl['name']}")
                print(f"     {pl['url']}")
            print()
            return

        playlist = await scrape_playlist(
            bm.page,
            playlist_url=args.url,
            playlist_name=args.playlist,
        )

        # The page is now on the playlist — capture its canonical URL + name
        # so we can persist them in the spreadsheet's source metadata.
        source_url = args.url or bm.page.url
        source_name = playlist.name

        if not playlist.songs:
            print("No songs found in playlist — nothing to export.")
            return

        # --update: merge with existing spreadsheet, only add new songs
        if args.update and os.path.exists(args.output):
            from openpyxl import load_workbook
            from models import Song

            wb = load_workbook(args.output)
            ws = wb.active

            # Read existing songs with their metadata
            existing = {}  # (norm_title, norm_artist, norm_album) -> {row data}
            from utils import normalize
            for row in ws.iter_rows(min_row=2, values_only=False):
                title = row[1].value or ""
                artist = row[2].value or ""
                album = row[3].value or ""
                if not title:
                    continue
                key = (normalize(str(title)), normalize(str(artist)), normalize(str(album)))
                existing[key] = {
                    "title": str(title),
                    "artist": str(artist),
                    "album": str(row[3].value or ""),
                    "release_date": str(row[4].value or ""),
                    "genre": str(row[5].value or ""),
                    "duration": str(row[6].value or "") if row[6].value else "",
                }

            # Find new songs not in the existing spreadsheet
            new_songs = []
            for song in playlist.songs:
                key = (normalize(song.title), normalize(song.artist), normalize(song.album))
                if key not in existing:
                    new_songs.append(song)

            if not new_songs:
                # Nothing to add, but we just verified the sheet matches the
                # playlist — bump the timestamp so listings reflect that.
                from spreadsheet_tags import set_tag, set_updated, set_source, TAG_AMAZON
                set_tag(args.output, TAG_AMAZON)
                set_updated(args.output)
                set_source(args.output, url=source_url, name=source_name)
                print(f"\nNo new songs found — spreadsheet is up to date ({len(existing)} songs).")
                return

            print(f"\nFound {len(new_songs)} new song(s) (existing: {len(existing)}).")

            # Look up metadata for new songs only
            if args.metadata:
                from metadata import enrich_songs
                print(f"Looking up metadata for {len(new_songs)} new songs via MusicBrainz...\n")

                def progress(current, total, enriched):
                    if current % 25 == 0 or current == total:
                        print(f"  {current}/{total} looked up ({enriched} enriched)")

                enrich_songs(new_songs, progress_callback=progress)

            # Append new songs to the spreadsheet
            next_row = ws.max_row + 1
            for i, song in enumerate(new_songs):
                row_idx = next_row + i
                ws.cell(row=row_idx, column=1, value=row_idx - 1)
                ws.cell(row=row_idx, column=2, value=song.title)
                ws.cell(row=row_idx, column=3, value=song.artist)
                ws.cell(row=row_idx, column=4, value=song.album)
                date_val = str(song.release_date) if song.release_date else ""
                if len(date_val) > 4:
                    date_val = date_val[:4]
                ws.cell(row=row_idx, column=5, value=date_val)
                ws.cell(row=row_idx, column=6, value=song.genre)
                ws.cell(row=row_idx, column=7, value=song.duration)

            wb.save(args.output)
            # Refresh the Amazon Music tag, timestamp, and source pointer so
            # listings stay current and future updates can skip the prompt.
            from spreadsheet_tags import set_tag, set_updated, set_source, TAG_AMAZON
            set_tag(args.output, TAG_AMAZON)
            set_updated(args.output)
            set_source(args.output, url=source_url, name=source_name)
            print(f"\nAdded {len(new_songs)} new song(s) to: {os.path.abspath(args.output)}")
            print(f"Total: {len(existing) + len(new_songs)} songs.")
            return

        if args.metadata:
            from metadata import enrich_songs
            print(f"\nLooking up metadata for {len(playlist.songs)} songs via MusicBrainz...")
            print("(~1 request/sec — this will take a while for large playlists)\n")

            def progress(current, total, enriched):
                if current % 25 == 0 or current == total:
                    print(f"  {current}/{total} looked up ({enriched} enriched)")

            enrich_songs(playlist.songs, progress_callback=progress)

        path = export_to_xlsx(
            playlist, args.output,
            source_url=source_url, source_name=source_name,
        )
        print(f"\nExported {len(playlist.songs)} songs to: {os.path.abspath(path)}")


async def cmd_create(args):
    from browser import BrowserManager
    from auth import ensure_logged_in
    from models import Song

    input_path = args.input

    # Determine input type and load songs
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
            print("No songs found in spreadsheet.")
            return
        print(f"Loaded {len(songs)} songs from {input_path}")

        from importer import create_playlist_from_songs
        async with BrowserManager(headless=args.headless, slow_mo=args.slow_mo) as bm:
            await ensure_logged_in(bm.page)
            await create_playlist_from_songs(
                bm.page,
                songs=songs,
                playlist_name=args.name,
                auto_threshold=args.threshold,
            )
    else:
        from importer import create_playlist_from_csv
        async with BrowserManager(headless=args.headless, slow_mo=args.slow_mo) as bm:
            await ensure_logged_in(bm.page)
            await create_playlist_from_csv(
                bm.page,
                csv_path=input_path,
                playlist_name=args.name,
                auto_threshold=args.threshold,
            )


def cmd_metadata(args):
    from openpyxl import load_workbook
    from models import Song
    from metadata import enrich_songs

    xlsx_path = args.file
    output_path = args.output or xlsx_path

    wb = load_workbook(xlsx_path)
    ws = wb.active

    # Read songs from spreadsheet (expecting columns: #, Song Name, Artist, Album, Release Date, Genre, Duration)
    songs = []
    for row in ws.iter_rows(min_row=2, values_only=False):
        title = row[1].value or ""
        artist = row[2].value or ""
        album = row[3].value or ""
        release_date = row[4].value or ""
        genre = row[5].value or ""
        if title:
            songs.append(Song(
                title=str(title),
                artist=str(artist),
                album=str(album),
                release_date=str(release_date),
                genre=str(genre),
            ))

    # If --force-dates, clear all existing dates so they get re-looked up
    if args.force_dates:
        for s in songs:
            s.release_date = ""
        print("  --force-dates: cleared all existing release dates for re-lookup")

    # Count how many already have metadata
    need_enrichment = [s for s in songs if not s.release_date or not s.genre]
    print(f"\nLoaded {len(songs)} songs from {xlsx_path}")
    print(f"  {len(songs) - len(need_enrichment)} already have both release date and genre")
    print(f"  {len(need_enrichment)} need metadata lookup\n")

    if not need_enrichment:
        print("All songs already have metadata — nothing to do.")
        return

    def progress(current, total, enriched):
        if current % 25 == 0 or current == total:
            print(f"  {current}/{total} looked up ({enriched} enriched)")

    enrich_songs(songs, progress_callback=progress)

    # Write results back to the spreadsheet (normalize dates to year-only)
    for i, song in enumerate(songs):
        row_idx = i + 2  # row 1 is header
        date_val = str(song.release_date) if song.release_date else ""
        if len(date_val) > 4:
            date_val = date_val[:4]
        ws.cell(row=row_idx, column=5, value=date_val)
        ws.cell(row=row_idx, column=6, value=song.genre)

    wb.save(output_path)
    print(f"\nUpdated metadata in: {os.path.abspath(output_path)}")


def cmd_duplicates(args):
    from duplicates import find_duplicates, _load_dismissed, _save_dismissed, _group_key, _DISMISSED_FILE

    if args.reset:
        if os.path.exists(_DISMISSED_FILE):
            os.remove(_DISMISSED_FILE)
            print("Cleared dismissed duplicates.")

    groups = find_duplicates(args.file, threshold=args.threshold)
    dismissed = _load_dismissed()

    if not groups:
        print("\nNo potential duplicates found.")
        return

    # Filter out previously dismissed groups
    new_groups = []
    for group in groups:
        key = _group_key([(s["title"], s["artist"]) for s in group])
        if key not in dismissed:
            new_groups.append((key, group))

    if not new_groups:
        print(f"\nFound {len(groups)} duplicate group(s), but all were previously dismissed.")
        print("Use --reset to clear dismissed duplicates and scan again.")
        return

    print(f"\nFound {len(new_groups)} potential duplicate group(s) to review:\n")

    from openpyxl import load_workbook
    wb = load_workbook(args.file)
    ws = wb.active
    rows_to_delete = []
    songs_to_delete_from_amazon = []  # (title, artist, row_index_in_playlist)

    for idx, (key, group) in enumerate(new_groups, 1):
        print(f"--- Group {idx}/{len(new_groups)} ---")
        for i, song in enumerate(group, 1):
            print(f"  [{i}] Row {song['row']}: {song['title']}")
            print(f"      Artist: {song['artist']}")
            print(f"      Album:  {song['album']}")
        print()

        print("  Options:")
        print("    Enter number(s) to DELETE (e.g., '2' or '1,3')")
        print("    'skip'  — not duplicates, don't ask again")
        print("    'later' — skip for now, ask again next time")
        print("    'quit'  — stop reviewing, apply changes so far")
        print()

        while True:
            choice = input("  Your choice: ").strip().lower()

            if choice == "quit":
                break
            elif choice == "later":
                break
            elif choice == "skip":
                dismissed.add(key)
                _save_dismissed(dismissed)
                print("  Marked as not duplicates — won't ask again.\n")
                break
            else:
                try:
                    nums = [int(x.strip()) for x in choice.split(",")]
                    valid = all(1 <= n <= len(group) for n in nums)
                    if not valid:
                        print(f"  Invalid number(s). Enter 1-{len(group)}, 'skip', 'later', or 'quit'.")
                        continue

                    to_delete = [group[n - 1] for n in nums]
                    titles = ", ".join(f"'{s['title']}' (row {s['row']})" for s in to_delete)
                    confirm = input(f"  Delete {titles}? (y/n): ").strip().lower()
                    if confirm == "y":
                        for s in to_delete:
                            rows_to_delete.append(s["row"])
                            songs_to_delete_from_amazon.append({
                                "title": s["title"],
                                "artist": s["artist"],
                                "row": s["row"],
                            })
                        dismissed.add(key)
                        _save_dismissed(dismissed)
                        print("  Queued for deletion.\n")
                    else:
                        print("  Cancelled.\n")
                    break
                except ValueError:
                    print(f"  Invalid input. Enter number(s), 'skip', 'later', or 'quit'.")
                    continue

        if choice == "quit":
            break

    if not songs_to_delete_from_amazon:
        print("\nNo changes made.")
        return

    # Step 1: Determine which Amazon Music playlist to delete from
    playlist_url = getattr(args, 'url', None)
    playlist_name = getattr(args, 'playlist', None)
    skip_amazon = False

    if not playlist_url and not playlist_name:
        from playlist_cache import load_cached_playlists
        cached = load_cached_playlists()

        if cached:
            print("\nAlso remove from Amazon Music? Choose a playlist:")
            for i, pl in enumerate(cached, 1):
                print(f"  {i}. {pl['name']}")
            print(f"  0. Skip (spreadsheet only)")
            print()

            while True:
                choice = input("  Playlist number: ").strip()
                if choice == "0" or choice.lower() == "skip":
                    skip_amazon = True
                    break
                try:
                    idx = int(choice) - 1
                    if 0 <= idx < len(cached):
                        playlist_url = cached[idx]["url"]
                        print(f"  Selected: {cached[idx]['name']}")
                        break
                    else:
                        print(f"  Enter 0-{len(cached)}.")
                except ValueError:
                    print(f"  Enter a number 0-{len(cached)}.")
        else:
            print("\nNo cached playlists. Use 'Refresh playlist list' first,")
            print("or re-run with --url or --playlist.")
            print("Removing from spreadsheet only.")
            skip_amazon = True

    # Step 2: Delete from Amazon Music first (if requested)
    successfully_deleted = set()  # rows that were removed from Amazon

    if not skip_amazon and (playlist_url or playlist_name):
        print(f"\nRemoving {len(songs_to_delete_from_amazon)} song(s) from Amazon Music...")
        successfully_deleted = asyncio.run(
            _delete_from_amazon_with_url(args, songs_to_delete_from_amazon,
                                         playlist_url, playlist_name)
        )
        if not successfully_deleted:
            successfully_deleted = set()
    else:
        # If skipping Amazon, treat all as "ready to delete from spreadsheet"
        successfully_deleted = {s["row"] for s in songs_to_delete_from_amazon}

    # Step 3: Only delete from spreadsheet the songs that were actually removed
    # from Amazon Music (or all of them if Amazon deletion was skipped)
    final_rows = [r for r in rows_to_delete if r in successfully_deleted]

    if final_rows:
        final_rows.sort(reverse=True)
        for row_num in final_rows:
            ws.delete_rows(row_num)

        # Renumber the # column
        for i, row in enumerate(ws.iter_rows(min_row=2, max_col=1), start=1):
            row[0].value = i

        wb.save(args.file)
        print(f"\nRemoved {len(final_rows)} duplicate(s) from spreadsheet.")
    else:
        print("\nNo spreadsheet changes (Amazon Music deletion did not succeed).")


async def _delete_from_amazon_with_url(args, songs_to_delete, playlist_url=None, playlist_name=None) -> set[int]:
    """Delete songs from Amazon Music. Returns set of row numbers successfully deleted."""
    from browser import BrowserManager
    from auth import ensure_logged_in
    from scraper import delete_songs_from_playlist

    async with BrowserManager(headless=args.headless, slow_mo=args.slow_mo) as bm:
        await ensure_logged_in(bm.page)
        deleted_rows = await delete_songs_from_playlist(
            bm.page,
            playlist_url=playlist_url,
            playlist_name=playlist_name,
            songs=songs_to_delete,
        )
        return deleted_rows or set()


def cmd_check(args):
    from openpyxl import load_workbook

    wb = load_workbook(args.file)
    ws = wb.active

    missing_date = []
    missing_genre = []
    missing_both = []

    for row in ws.iter_rows(min_row=2, values_only=False):
        title = row[1].value or ""
        if not title:
            continue
        artist = row[2].value or ""
        release_date = str(row[4].value or "").strip()
        genre = str(row[5].value or "").strip()
        row_num = row[0].row

        no_date = not release_date
        no_genre = not genre

        if no_date and no_genre:
            missing_both.append((row_num, title, artist))
        elif no_date:
            missing_date.append((row_num, title, artist))
        elif no_genre:
            missing_genre.append((row_num, title, artist))

    total = ws.max_row - 1
    complete = total - len(missing_date) - len(missing_genre) - len(missing_both)

    print(f"\n  Total songs: {total}")
    print(f"  Complete:    {complete}")
    print(f"  Missing year only:  {len(missing_date)}")
    print(f"  Missing genre only: {len(missing_genre)}")
    print(f"  Missing both:       {len(missing_both)}")

    if missing_both:
        print(f"\n  --- Missing both year and genre ---")
        for row_num, title, artist in missing_both:
            print(f"    Row {row_num}: {title} — {artist}")

    if missing_date:
        print(f"\n  --- Missing year only ---")
        for row_num, title, artist in missing_date:
            print(f"    Row {row_num}: {title} — {artist}")

    if missing_genre:
        print(f"\n  --- Missing genre only ---")
        for row_num, title, artist in missing_genre:
            print(f"    Row {row_num}: {title} — {artist}")

    if not missing_date and not missing_genre and not missing_both:
        print("\n  All songs have both year and genre!")

    print()


def main():
    parser = build_parser()
    args = parser.parse_args()
    setup_logging(verbose=args.verbose)

    if args.command == "export":
        asyncio.run(cmd_export(args))
    elif args.command == "create":
        asyncio.run(cmd_create(args))
    elif args.command == "metadata":
        cmd_metadata(args)
    elif args.command == "check":
        cmd_check(args)
    elif args.command == "duplicates":
        cmd_duplicates(args)


if __name__ == "__main__":
    main()
