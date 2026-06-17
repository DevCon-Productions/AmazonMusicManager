import os

from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, PatternFill

from models import Playlist
from utils import logger


def export_to_xlsx(
    playlist: Playlist,
    output_path: str,
    source_url: str = "",
    source_name: str = "",
) -> str:
    """Write a Playlist to a styled .xlsx workbook and return the file path.

    *source_url* / *source_name* are persisted in the hidden _metadata sheet
    so that later `--update` runs can re-target the same Amazon Music
    playlist without asking the user.
    """
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    wb = Workbook()
    ws = wb.active
    ws.title = playlist.name[:31]  # Excel sheet name limit

    # --- Header row ---
    headers = ["#", "Song Name", "Artist", "Album", "Release Date", "Genre", "Duration", "Track ASIN", "Album ASIN"]
    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")

    for col_idx, header in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col_idx, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center")

    # --- Data rows ---
    for row_idx, song in enumerate(playlist.songs, start=2):
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
        ws.cell(row=row_idx, column=8, value=song.asin)
        ws.cell(row=row_idx, column=9, value=song.album_asin)

    # --- Auto-fit column widths ---
    for col in ws.columns:
        max_len = 0
        col_letter = col[0].column_letter
        for cell in col:
            if cell.value:
                max_len = max(max_len, len(str(cell.value)))
        ws.column_dimensions[col_letter].width = min(max_len + 4, 50)

    # Freeze the header row
    ws.freeze_panes = "A2"

    wb.save(output_path)
    logger.info("Exported %d songs to %s", len(playlist.songs), output_path)

    # Tag as Amazon Music playlist, record the update time, and remember
    # the source so later updates don't have to ask.
    from spreadsheet_tags import set_tag, set_updated, set_source, TAG_AMAZON
    set_tag(output_path, TAG_AMAZON)
    set_updated(output_path)
    if source_url or source_name:
        set_source(output_path, url=source_url, name=source_name or playlist.name)

    return output_path
