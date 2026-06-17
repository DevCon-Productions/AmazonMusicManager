"""Manage metadata tags stored in a hidden sheet within .xlsx files."""

from datetime import datetime

from openpyxl import load_workbook

TAG_SHEET = "_metadata"
TAG_AMAZON = "Amazon Music"
TAG_LOCAL = "Local"

_TS_FORMAT = "%Y-%m-%d %H:%M:%S"


def _open_or_create_sheet(wb):
    if TAG_SHEET in wb.sheetnames:
        ws = wb[TAG_SHEET]
        # Backfill source-info headers on older sheets that predate them.
        if not ws["C1"].value:
            ws["C1"] = "source_url"
        if not ws["D1"].value:
            ws["D1"] = "source_name"
    else:
        ws = wb.create_sheet(TAG_SHEET)
        ws.sheet_state = "hidden"
        ws["A1"] = "tag"
        ws["B1"] = "updated"
        ws["C1"] = "source_url"
        ws["D1"] = "source_name"
    return ws


def get_tag(xlsx_path: str) -> str:
    """Read the tag from a spreadsheet. Returns '' if none set."""
    try:
        wb = load_workbook(xlsx_path)
        if TAG_SHEET in wb.sheetnames:
            ws = wb[TAG_SHEET]
            return str(ws["A2"].value or "")
        return ""
    except Exception:
        return ""


def set_tag(xlsx_path: str, tag: str):
    """Write a tag into the spreadsheet's hidden metadata sheet."""
    wb = load_workbook(xlsx_path)
    ws = _open_or_create_sheet(wb)
    ws["A2"] = tag
    wb.save(xlsx_path)


def get_updated(xlsx_path: str) -> str:
    """Read the 'last updated' ISO timestamp. Returns '' if not recorded."""
    try:
        wb = load_workbook(xlsx_path)
        if TAG_SHEET in wb.sheetnames:
            ws = wb[TAG_SHEET]
            return str(ws["B2"].value or "")
        return ""
    except Exception:
        return ""


def set_updated(xlsx_path: str, when: datetime | None = None):
    """Record the current time (or *when*) as the last-updated timestamp."""
    wb = load_workbook(xlsx_path)
    ws = _open_or_create_sheet(wb)
    ts = (when or datetime.now()).strftime(_TS_FORMAT)
    ws["B2"] = ts
    wb.save(xlsx_path)


def get_source(xlsx_path: str) -> dict:
    """Return {'url': ..., 'name': ...} for the source Amazon Music playlist.

    Either key may be ''. Returns an empty dict if the sheet doesn't have
    a _metadata sheet at all.
    """
    try:
        wb = load_workbook(xlsx_path)
        if TAG_SHEET not in wb.sheetnames:
            return {}
        ws = wb[TAG_SHEET]
        return {
            "url": str(ws["C2"].value or ""),
            "name": str(ws["D2"].value or ""),
        }
    except Exception:
        return {}


def set_source(xlsx_path: str, url: str = "", name: str = ""):
    """Record the Amazon Music playlist this spreadsheet came from."""
    wb = load_workbook(xlsx_path)
    ws = _open_or_create_sheet(wb)
    if url:
        ws["C2"] = url
    if name:
        ws["D2"] = name
    wb.save(xlsx_path)


def get_tag_display(xlsx_path: str) -> str:
    """Return a short display string for the tag.

    For Amazon Music sheets with a recorded update time, includes the date:
    '[Amazon Music · 2026-04-22]'. Otherwise just '[Tag]'.
    """
    tag = get_tag(xlsx_path)
    if not tag:
        return ""
    if tag == TAG_AMAZON:
        ts = get_updated(xlsx_path)
        if ts:
            date_part = ts.split(" ", 1)[0]
            return f"[{tag} · {date_part}]"
    return f"[{tag}]"
