"""Amazon Music Manager — PyQt6 desktop GUI.

Step 1 of the GUI build: main window shell with Chrome connect/disconnect.
See memory/project_gui_plan.md for the full roadmap.
"""
import asyncio
import copy as _copy
import glob
import json
import logging
import os
import random
import re
import sys
import time
import zipfile
from collections import deque
from datetime import datetime
from typing import NamedTuple

from PyQt6.QtCore import (
    Qt, QMimeData, QObject, QPoint, QRunnable, QSettings, QThread, QThreadPool, QTimer,
    pyqtSignal, pyqtSlot,
)
from PyQt6.QtGui import QAction, QBrush, QColor, QDrag, QFontMetrics, QIcon, QKeySequence, QPixmap, QShortcut, QTransform
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDockWidget,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QSplashScreen,
    QSplitter,
    QStackedWidget,
    QStatusBar,
    QStyle,
    QSystemTrayIcon,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QToolBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from openpyxl import load_workbook

from browser import BrowserManager, minimize_process_windows
from organization import (
    add_item,
    create_folder,
    delete_folder,
    rename_folder,
    from_stored_path,
    get_node,
    iter_playlists_recursive,
    load_org,
    remove_item,
    save_org,
    to_stored_path,
)

# Custom MIME types used for drag-and-drop within the GUI.
MIME_ORG = "application/x-amm-org-node"
MIME_SPREADSHEET = "application/x-amm-spreadsheet-path"
MIME_PLAYLIST = "application/x-amm-playlist"
from spreadsheet_tags import TAG_AMAZON, TAG_SHEET, get_source, get_tag_display
from utils import logger, setup_logging
from config import OUTPUT_DIR  # frozen build → per-user data dir; dev → ./output

_HERE = os.path.dirname(os.path.abspath(__file__))


def _resource_path(rel: str) -> str:
    """Resolve a resource path — works both in dev and PyInstaller bundles."""
    base = getattr(sys, "_MEIPASS", _HERE)
    return os.path.join(base, rel)


def _ensure_tray_ico(png_path: str) -> str | None:
    """Build (or reuse) a multi-size .ico next to the PNG. Returns the .ico
    path, or None if conversion failed. Windows tray rendering is much more
    reliable from a real .ico than from a scaled-down PNG."""
    if not os.path.exists(png_path):
        return None
    ico_path = os.path.splitext(png_path)[0] + ".ico"
    try:
        if (
            os.path.exists(ico_path)
            and os.path.getmtime(ico_path) >= os.path.getmtime(png_path)
        ):
            return ico_path
    except OSError:
        pass
    try:
        from PyQt6.QtGui import QImage
        src = QImage(png_path)
        if src.isNull():
            return None
        # Write at 256 px — Qt's ICO writer embeds the single resolution, and
        # Windows down-scales for 16/24/32 tray sizes with high quality.
        scaled = src.scaled(
            256, 256,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        if scaled.save(ico_path, "ICO"):
            return ico_path
    except Exception as e:  # noqa: BLE001
        logger.warning("tray ico build failed: %s", e)
    return None


# ---------------------------------------------------------------------------
# Logging bridge: pipe `logger` records into the GUI log pane via a signal.
# ---------------------------------------------------------------------------
class QtLogEmitter(QObject):
    message = pyqtSignal(str)


class QtLogHandler(logging.Handler):
    def __init__(self, emitter: QtLogEmitter):
        super().__init__()
        self._emitter = emitter

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._emitter.message.emit(self.format(record))
        except Exception:
            self.handleError(record)


class _SpreadsheetTable(QTableWidget):
    """QTableWidget subclass that advertises the selected row's path on drag."""

    # (path, op): emitted when the user chooses Cut/Copy from the context menu.
    clipboard_requested = pyqtSignal(str, str)
    # Emitted when the user chooses "Delete file" from the context menu.
    delete_requested = pyqtSignal(str)
    # Emitted when the user chooses "Rename…" from the context menu.
    rename_requested = pyqtSignal(str)
    # Bulk variants: (paths, op) and (paths,).
    bulk_clipboard_requested = pyqtSignal(list, str)
    bulk_delete_requested = pyqtSignal(list)

    def mimeTypes(self):
        return [MIME_SPREADSHEET]

    def mimeData(self, items):
        # Qt calls us with every selected cell across the row; pick the Name
        # column, since that carries the path in ROLE_USER. Multi-row drag is
        # refused — use cut/copy + paste for bulk filing.
        rows_selected = {it.row() for it in items}
        if len(rows_selected) > 1:
            return None
        for it in items:
            if it.column() == SpreadsheetListWidget.COL_NAME:
                path = it.data(Qt.ItemDataRole.UserRole)
                if path:
                    md = QMimeData()
                    md.setData(MIME_SPREADSHEET, str(path).encode("utf-8"))
                    return md
        return None

    def startDrag(self, supported_actions):
        # Build a drag whose ghost is just the Name cell, not the full row
        # (which would also include the Tag and Modified columns).
        items = self.selectedItems()
        if not items:
            return
        md = self.mimeData(items)
        if md is None:
            return
        name_item = None
        for it in items:
            if it.column() == SpreadsheetListWidget.COL_NAME:
                name_item = it
                break
        drag = QDrag(self)
        drag.setMimeData(md)
        if name_item is not None:
            rect = self.visualItemRect(name_item)
            # Crop pixmap so the ghost hugs the text instead of spanning the
            # full Name-column cell (Stretch-sized). All measurements below
            # are in logical pixels; the grabbed pixmap is in physical pixels
            # (devicePixelRatio), so the crop must be scaled by DPR or the
            # ghost shows only the top-left corner on high-DPI displays.
            fm = self.fontMetrics()
            text_w = fm.horizontalAdvance(name_item.text())
            ghost_w = min(rect.width(), text_w + 32)  # 32 ≈ icon + padding
            ghost_h = rect.height()
            pixmap = self.viewport().grab(rect)
            dpr = pixmap.devicePixelRatioF() or 1.0
            cropped = pixmap.copy(
                0, 0, int(round(ghost_w * dpr)), int(round(ghost_h * dpr))
            )
            cropped.setDevicePixelRatio(dpr)
            drag.setPixmap(cropped)
            drag.setHotSpot(QPoint(ghost_w // 2, ghost_h // 2))
        drag.exec(supported_actions)

    def _selected_paths(self) -> list[str]:
        """Distinct paths from name-column UserRole, in row order."""
        seen: set[str] = set()
        paths: list[str] = []
        for it in self.selectedItems():
            if it.column() != SpreadsheetListWidget.COL_NAME:
                continue
            p = it.data(Qt.ItemDataRole.UserRole)
            if not p or p in seen:
                continue
            seen.add(p)
            paths.append(str(p))
        return paths

    def contextMenuEvent(self, event):
        clicked = self.itemAt(event.pos())
        if clicked is None:
            return
        # If the right-click landed on a row that isn't part of the current
        # selection, treat it as a single-target click (mirrors common
        # file-manager UX). Otherwise use the full multi-selection.
        clicked_name = self.item(clicked.row(), SpreadsheetListWidget.COL_NAME)
        if clicked_name is None:
            return
        if not clicked_name.isSelected():
            self.clearSelection()
            self.selectRow(clicked.row())
        paths = self._selected_paths()
        if not paths:
            return
        menu = QMenu(self)
        if len(paths) == 1:
            path = paths[0]
            label = clicked_name.text()
            act_cut = menu.addAction(f"Cut '{label}'")
            act_copy = menu.addAction(f"Copy '{label}'")
            menu.addSeparator()
            act_rename = menu.addAction(f"Rename '{label}'…")
            act_delete = menu.addAction(f"Delete file '{label}'…")
            chosen = menu.exec(event.globalPos())
            if chosen is act_cut:
                self.clipboard_requested.emit(path, "cut")
            elif chosen is act_copy:
                self.clipboard_requested.emit(path, "copy")
            elif chosen is act_rename:
                self.rename_requested.emit(path)
            elif chosen is act_delete:
                self.delete_requested.emit(path)
        else:
            n = len(paths)
            act_cut = menu.addAction(f"Cut {n} items")
            act_copy = menu.addAction(f"Copy {n} items")
            menu.addSeparator()
            act_delete = menu.addAction(f"Delete {n} files…")
            chosen = menu.exec(event.globalPos())
            if chosen is act_cut:
                self.bulk_clipboard_requested.emit(paths, "cut")
            elif chosen is act_copy:
                self.bulk_clipboard_requested.emit(paths, "copy")
            elif chosen is act_delete:
                self.bulk_delete_requested.emit(paths)


# ---------------------------------------------------------------------------
# Spreadsheet list: read-only view of ./output/*.xlsx with metadata tags.
# ---------------------------------------------------------------------------
class SpreadsheetListWidget(QWidget):
    COL_NAME = 0
    COL_TAG = 1
    COL_MODIFIED = 2

    spreadsheet_activated = pyqtSignal(str)  # emits full path on double-click
    clipboard_requested = pyqtSignal(str, str)  # forwarded from the table (path, op)
    delete_requested = pyqtSignal(str)          # forwarded from the table (path)
    rename_requested = pyqtSignal(str)          # forwarded from the table (path)
    # Bulk variants forwarded from the table.
    bulk_clipboard_requested = pyqtSignal(list, str)  # (paths, op)
    bulk_delete_requested = pyqtSignal(list)          # (paths,)

    def __init__(self) -> None:
        super().__init__()

        # Filter of normalized paths; None = show everything on disk.
        self._path_filter: set[str] | None = None
        self._filter_label_text: str = ""
        # Normalized paths marked as "cut" for italic+gray styling.
        # Empty set = nothing marked (back-compat: a single path is just a
        # one-element set).
        self._cut_paths_norm: set[str] = set()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        header = QHBoxLayout()
        self._count_label = QLabel("0 spreadsheets")
        self._filter_label = QLabel("")
        self._filter_label.setStyleSheet("color: gray;")
        self._refresh_btn = QPushButton("Refresh")
        self._refresh_btn.clicked.connect(self.refresh)
        header.addWidget(self._count_label)
        header.addSpacing(8)
        header.addWidget(self._filter_label)
        header.addStretch(1)
        header.addWidget(self._refresh_btn)
        layout.addLayout(header)

        self._table = _SpreadsheetTable(0, 3)
        self._table.setHorizontalHeaderLabels(["Name", "Tag", "Modified"])
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._table.setSortingEnabled(True)
        self._table.sortByColumn(self.COL_NAME, Qt.SortOrder.AscendingOrder)
        self._table.verticalHeader().setVisible(False)
        self._table.setDragEnabled(True)
        self._table.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        h = self._table.horizontalHeader()
        h.setSectionResizeMode(self.COL_NAME, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(self.COL_TAG, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(self.COL_MODIFIED, QHeaderView.ResizeMode.ResizeToContents)
        self._table.itemDoubleClicked.connect(self._on_double_click)
        self._table.clipboard_requested.connect(self.clipboard_requested)
        self._table.delete_requested.connect(self.delete_requested)
        self._table.rename_requested.connect(self.rename_requested)
        self._table.bulk_clipboard_requested.connect(self.bulk_clipboard_requested)
        self._table.bulk_delete_requested.connect(self.bulk_delete_requested)
        layout.addWidget(self._table)

        self.refresh()

    def mark_cut_paths(self, paths: set[str] | str | None) -> None:
        """Italic + gray the rows whose paths match. Accepts a set, a single
        path (back-compat), or None to clear."""
        if paths is None or (isinstance(paths, str) and not paths):
            self._cut_paths_norm = set()
        elif isinstance(paths, str):
            self._cut_paths_norm = {_norm_path(paths)}
        else:
            self._cut_paths_norm = {_norm_path(p) for p in paths if p}
        self._apply_cut_style()

    # Back-compat alias for the single-path call site.
    mark_cut_path = mark_cut_paths

    def set_path_filter(
        self, paths: set[str] | None, label: str = ""
    ) -> None:
        """Restrict the list to these normalized paths. None = no filter."""
        self._path_filter = paths
        self._filter_label_text = label
        self.refresh()

    @pyqtSlot()
    def refresh(self) -> None:
        all_rows = _scan_output_dir()
        if self._path_filter is None:
            rows = all_rows
        else:
            rows = [r for r in all_rows if _norm_path(r["path"]) in self._path_filter]
        self._table.setSortingEnabled(False)
        self._table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            name_item = QTableWidgetItem(row.get("display_name") or row["name"])
            name_item.setData(Qt.ItemDataRole.UserRole, row["path"])
            name_item.setToolTip(row["path"])
            tag_item = QTableWidgetItem(row["tag"])
            modified_item = QTableWidgetItem(row["modified"])
            self._table.setItem(i, self.COL_NAME, name_item)
            self._table.setItem(i, self.COL_TAG, tag_item)
            self._table.setItem(i, self.COL_MODIFIED, modified_item)
        self._table.setSortingEnabled(True)
        self._count_label.setText(
            f"{len(rows)} spreadsheet{'s' if len(rows) != 1 else ''}"
        )
        self._filter_label.setText(
            f"· {self._filter_label_text}" if self._filter_label_text else ""
        )
        self._apply_cut_style()

    def _apply_cut_style(self) -> None:
        """Italic + gray every row whose path is in the cut set."""
        targets = self._cut_paths_norm
        for row in range(self._table.rowCount()):
            name_item = self._table.item(row, self.COL_NAME)
            if name_item is None:
                continue
            path = name_item.data(Qt.ItemDataRole.UserRole) or ""
            is_cut = bool(targets) and _norm_path(path) in targets
            for col in range(self._table.columnCount()):
                cell = self._table.item(row, col)
                if cell is None:
                    continue
                font = cell.font()
                font.setItalic(is_cut)
                cell.setFont(font)
                if is_cut:
                    cell.setForeground(Qt.GlobalColor.gray)

    @pyqtSlot(QTableWidgetItem)
    def _on_double_click(self, item: QTableWidgetItem) -> None:
        name_item = self._table.item(item.row(), self.COL_NAME)
        if name_item is None:
            return
        path = name_item.data(Qt.ItemDataRole.UserRole)
        if path:
            self.spreadsheet_activated.emit(str(path))


def _walk_playlist_entries(node: dict):
    """Yield every playlist entry anywhere under this org node."""
    for entry in node.get("items", []):
        if entry.get("kind") == "playlist":
            yield entry
    for sub in node.get("folders", {}).values():
        yield from _walk_playlist_entries(sub)


# ---------------------------------------------------------------------------
# Playlist list: flat view of every playlist entry in organization.json,
# deduplicated by URL. Shown when "All Playlists" is selected in the sidebar.
# ---------------------------------------------------------------------------
class _PlaylistTable(QTableWidget):
    """QTableWidget subclass that advertises the selected row's playlist
    (URL + name) on drag, mirroring _SpreadsheetTable."""

    def mimeTypes(self):
        return [MIME_PLAYLIST]

    def mimeData(self, items):
        # Multi-row drag is refused — use the right-click "Add N to folder…"
        # bulk action instead.
        rows_selected = {it.row() for it in items}
        if len(rows_selected) > 1:
            return None
        for it in items:
            if it.column() == PlaylistListWidget.COL_NAME:
                url = it.data(Qt.ItemDataRole.UserRole)
                name = it.text() or ""
                if url:
                    payload = json.dumps(
                        {"url": str(url), "name": str(name)}
                    ).encode("utf-8")
                    md = QMimeData()
                    md.setData(MIME_PLAYLIST, payload)
                    return md
        return None

    def startDrag(self, supported_actions):
        items = self.selectedItems()
        if not items:
            return
        md = self.mimeData(items)
        if md is None:
            return
        name_item = None
        for it in items:
            if it.column() == PlaylistListWidget.COL_NAME:
                name_item = it
                break
        drag = QDrag(self)
        drag.setMimeData(md)
        if name_item is not None:
            rect = self.visualItemRect(name_item)
            # Crop the ghost to the Name cell's text width (DPR-scaled so it
            # renders correctly on high-DPI displays — see _SpreadsheetTable
            # for the same pattern + reasoning).
            fm = self.fontMetrics()
            text_w = fm.horizontalAdvance(name_item.text())
            ghost_w = min(rect.width(), text_w + 32)
            ghost_h = rect.height()
            pixmap = self.viewport().grab(rect)
            dpr = pixmap.devicePixelRatioF() or 1.0
            cropped = pixmap.copy(
                0, 0, int(round(ghost_w * dpr)), int(round(ghost_h * dpr))
            )
            cropped.setDevicePixelRatio(dpr)
            drag.setPixmap(cropped)
            drag.setHotSpot(QPoint(ghost_w // 2, ghost_h // 2))
        drag.exec(supported_actions)


class PlaylistListWidget(QWidget):
    COL_NAME = 0
    COL_SOURCE = 1
    COL_URL = 2

    playlist_activated = pyqtSignal(str, str)  # (url, name)
    fetch_requested = pyqtSignal()             # user clicked "Fetch from Amazon"
    export_requested = pyqtSignal(str, str)    # (url, name) — context-menu Export
    # (url, name) — user picked "Add to folder…"; MainWindow opens the picker.
    add_to_folder_requested = pyqtSignal(str, str)
    delete_requested = pyqtSignal(str, str)    # (url, name) — context-menu Delete
    # Bulk variants: payloads is a list of {"url", "name"} dicts.
    bulk_add_to_folder_requested = pyqtSignal(list)
    bulk_delete_requested = pyqtSignal(list)

    def __init__(self) -> None:
        super().__init__()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        header = QHBoxLayout()
        self._count_label = QLabel("0 playlists")
        self._refresh_btn = QPushButton("Refresh")
        self._refresh_btn.clicked.connect(self.refresh)
        self._fetch_btn = QPushButton("Fetch from Amazon")
        self._fetch_btn.setToolTip(
            "Scrape your full playlist list from Amazon Music "
            "(requires a connected browser and that you're signed in)."
        )
        self._fetch_btn.clicked.connect(self.fetch_requested)
        header.addWidget(self._count_label)
        header.addStretch(1)
        header.addWidget(self._fetch_btn)
        header.addWidget(self._refresh_btn)
        layout.addLayout(header)

        self._table = _PlaylistTable(0, 3)
        self._table.setHorizontalHeaderLabels(["Name", "Source", "URL"])
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._table.setSortingEnabled(True)
        self._table.sortByColumn(self.COL_NAME, Qt.SortOrder.AscendingOrder)
        self._table.verticalHeader().setVisible(False)
        # Drag a row onto a sidebar folder to file the playlist there.
        self._table.setDragEnabled(True)
        self._table.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        # Interactive on every column = the user can drag the dividers. Default
        # widths (80/10/10) are applied once after first show, when the
        # viewport finally has a real width.
        h = self._table.horizontalHeader()
        for c in (self.COL_NAME, self.COL_SOURCE, self.COL_URL):
            h.setSectionResizeMode(c, QHeaderView.ResizeMode.Interactive)
        h.setStretchLastSection(False)
        self._initial_widths_set = False
        self._table.itemDoubleClicked.connect(self._on_double_click)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_context_menu)
        layout.addWidget(self._table)

        self.refresh()

    def showEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        super().showEvent(event)
        if not self._initial_widths_set:
            QTimer.singleShot(0, self._apply_default_widths)

    def _apply_default_widths(self) -> None:
        total = self._table.viewport().width()
        if total <= 0:
            QTimer.singleShot(0, self._apply_default_widths)
            return
        self._initial_widths_set = True
        self._table.setColumnWidth(self.COL_NAME, int(total * 0.80))
        self._table.setColumnWidth(self.COL_SOURCE, int(total * 0.10))
        self._table.setColumnWidth(self.COL_URL, int(total * 0.10))

    def set_fetch_enabled(self, enabled: bool) -> None:
        self._fetch_btn.setEnabled(enabled)

    @pyqtSlot()
    def refresh(self) -> None:
        try:
            org = load_org()
        except Exception:
            logger.exception("Could not load organization.json for playlists")
            org = {"root": {"folders": {}, "items": []}}
        # by_url[url] = {url, name, sources: set("Organized" / "Amazon")}
        by_url: dict[str, dict] = {}
        for entry in _walk_playlist_entries(org.get("root", {})):
            url = entry.get("url") or ""
            if not url:
                continue
            row = by_url.setdefault(
                url,
                {"url": url, "name": entry.get("name") or "(unnamed)",
                 "sources": set()},
            )
            row["sources"].add("Organized")

        cache_updated = ""
        try:
            from playlist_cache import _CACHE_FILE
            import json as _json
            if os.path.exists(_CACHE_FILE):
                with open(_CACHE_FILE, "r", encoding="utf-8") as f:
                    data = _json.load(f)
                cache_updated = data.get("updated", "") or ""
                for pl in data.get("playlists", []) or []:
                    url = pl.get("url") or ""
                    if not url:
                        continue
                    row = by_url.setdefault(
                        url,
                        {"url": url, "name": pl.get("name") or "(unnamed)",
                         "sources": set()},
                    )
                    if pl.get("name"):
                        row["name"] = pl["name"]
                    row["sources"].add("Amazon")
        except Exception:
            logger.exception("Could not read playlist cache")

        rows = sorted(by_url.values(), key=lambda r: r["name"].lower())
        self._table.setSortingEnabled(False)
        self._table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            name_item = QTableWidgetItem(row["name"])
            name_item.setData(Qt.ItemDataRole.UserRole, row["url"])
            src_item = QTableWidgetItem(", ".join(sorted(row["sources"])))
            url_item = QTableWidgetItem(row["url"])
            url_item.setToolTip(row["url"])
            self._table.setItem(i, self.COL_NAME, name_item)
            self._table.setItem(i, self.COL_SOURCE, src_item)
            self._table.setItem(i, self.COL_URL, url_item)
        self._table.setSortingEnabled(True)
        suffix = (
            f"  ·  Amazon list updated {cache_updated}" if cache_updated else ""
        )
        self._count_label.setText(
            f"{len(rows)} playlist{'s' if len(rows) != 1 else ''}{suffix}"
        )

    @pyqtSlot(QTableWidgetItem)
    def _on_double_click(self, item: QTableWidgetItem) -> None:
        name_item = self._table.item(item.row(), self.COL_NAME)
        if name_item is None:
            return
        url = name_item.data(Qt.ItemDataRole.UserRole) or ""
        name = name_item.text() or ""
        if url:
            self.playlist_activated.emit(str(url), str(name))

    def _selected_playlists(self) -> list[dict]:
        """Distinct (url, name) pairs from the selection, in row order."""
        seen: set[str] = set()
        result: list[dict] = []
        for it in self._table.selectedItems():
            if it.column() != self.COL_NAME:
                continue
            url = str(it.data(Qt.ItemDataRole.UserRole) or "")
            if not url or url in seen:
                continue
            seen.add(url)
            result.append({"url": url, "name": it.text() or ""})
        return result

    @pyqtSlot(QPoint)
    def _on_context_menu(self, pos: QPoint) -> None:
        clicked = self._table.itemAt(pos)
        if clicked is None:
            return
        clicked_name = self._table.item(clicked.row(), self.COL_NAME)
        if clicked_name is None:
            return
        # Right-clicking a row outside the selection retargets to that row,
        # matching standard file-manager UX.
        if not clicked_name.isSelected():
            self._table.clearSelection()
            self._table.selectRow(clicked.row())
        items = self._selected_playlists()
        if not items:
            return
        menu = QMenu(self)
        global_pos = self._table.viewport().mapToGlobal(pos)
        if len(items) == 1:
            url = items[0]["url"]
            name = items[0]["name"]
            play_act = menu.addAction("Play")
            export_act = menu.addAction("Export to spreadsheet…")
            add_folder_act = menu.addAction("Add to folder…")
            menu.addSeparator()
            copy_url_act = menu.addAction("Copy URL")
            menu.addSeparator()
            delete_act = menu.addAction("Delete playlist…")
            chosen = menu.exec(global_pos)
            if chosen is play_act:
                self.playlist_activated.emit(url, name)
            elif chosen is export_act:
                self.export_requested.emit(url, name)
            elif chosen is add_folder_act:
                self.add_to_folder_requested.emit(url, name)
            elif chosen is copy_url_act:
                QApplication.clipboard().setText(url)
            elif chosen is delete_act:
                self.delete_requested.emit(url, name)
        else:
            n = len(items)
            add_folder_act = menu.addAction(f"Add {n} playlists to folder…")
            menu.addSeparator()
            copy_urls_act = menu.addAction(f"Copy {n} URLs")
            menu.addSeparator()
            delete_act = menu.addAction(f"Delete {n} playlists from Amazon Music…")
            chosen = menu.exec(global_pos)
            if chosen is add_folder_act:
                self.bulk_add_to_folder_requested.emit(items)
            elif chosen is copy_urls_act:
                QApplication.clipboard().setText(
                    "\n".join(it["url"] for it in items)
                )
            elif chosen is delete_act:
                self.bulk_delete_requested.emit(items)


# ---------------------------------------------------------------------------
# Global search across every .xlsx in ./output/.
# ---------------------------------------------------------------------------
_TITLE_HEADER_ALIASES = ("song name", "title", "song", "track name", "track")
_ARTIST_HEADER_ALIASES = ("artist", "artists", "performer")
_ALBUM_HEADER_ALIASES = ("album", "release")


def _pick_header_index(
    headers: list[str], aliases: tuple[str, ...]
) -> int | None:
    norm = [h.strip().lower() for h in headers]
    for alias in aliases:
        if alias in norm:
            return norm.index(alias)
    # Fallback: substring (e.g. "Song Title").
    for alias in aliases:
        for i, h in enumerate(norm):
            if alias in h:
                return i
    return None


class SearchWidget(QWidget):
    """Search every spreadsheet in ./output/ for a song title and/or artist.

    Results show one row per matching track with a pointer back to its
    spreadsheet (and the spreadsheet's tag, e.g. "Amazon Music · Radiohead
    Select"). Cache keyed on file mtime so repeated searches are instant
    until a sheet is rewritten.
    """

    COL_TITLE = 0
    COL_ARTIST = 1
    COL_ALBUM = 2
    COL_SPREADSHEET = 3
    COL_TAG = 4

    spreadsheet_activated = pyqtSignal(str)  # path

    def __init__(self) -> None:
        super().__init__()
        # path -> {mtime, tag, rows: list[{title, artist, album}]}
        self._cache: dict[str, dict] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        bar = QHBoxLayout()
        self._query = QLineEdit()
        self._query.setPlaceholderText(
            "Search title and/or artist across all spreadsheets…"
        )
        self._query.returnPressed.connect(self._do_search)
        self._scope = QButtonGroup(self)
        self._title_rb = QRadioButton("Title")
        self._artist_rb = QRadioButton("Artist")
        self._both_rb = QRadioButton("Both")
        self._both_rb.setChecked(True)
        for rb in (self._title_rb, self._artist_rb, self._both_rb):
            self._scope.addButton(rb)
        self._search_btn = QPushButton("Search")
        self._search_btn.clicked.connect(self._do_search)

        bar.addWidget(QLabel("Find:"))
        bar.addWidget(self._query, 1)
        bar.addSpacing(8)
        bar.addWidget(self._title_rb)
        bar.addWidget(self._artist_rb)
        bar.addWidget(self._both_rb)
        bar.addWidget(self._search_btn)
        layout.addLayout(bar)

        self._summary_label = QLabel("Type a query and press Enter.")
        self._summary_label.setStyleSheet("color: #888;")
        layout.addWidget(self._summary_label)

        self._table = QTableWidget(0, 5)
        self._table.setHorizontalHeaderLabels(
            ["Title", "Artist", "Album", "Spreadsheet", "Tag"]
        )
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setSortingEnabled(True)
        self._table.verticalHeader().setVisible(False)
        h = self._table.horizontalHeader()
        for c in range(5):
            h.setSectionResizeMode(c, QHeaderView.ResizeMode.Interactive)
        h.setStretchLastSection(False)
        self._initial_widths_set = False
        self._table.itemDoubleClicked.connect(self._on_double_click)
        layout.addWidget(self._table)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if not self._initial_widths_set:
            QTimer.singleShot(0, self._apply_default_widths)
        # Drop the user straight into the search box.
        self._query.setFocus()

    def _apply_default_widths(self) -> None:
        total = self._table.viewport().width()
        if total <= 0:
            QTimer.singleShot(0, self._apply_default_widths)
            return
        self._initial_widths_set = True
        # Title 30 / Artist 20 / Album 20 / Spreadsheet 20 / Tag 10
        for c, frac in (
            (self.COL_TITLE, 0.30),
            (self.COL_ARTIST, 0.20),
            (self.COL_ALBUM, 0.20),
            (self.COL_SPREADSHEET, 0.20),
            (self.COL_TAG, 0.10),
        ):
            self._table.setColumnWidth(c, int(total * frac))

    def _scan_path(self, path: str) -> dict | None:
        """Read + index a single spreadsheet, with mtime-keyed caching."""
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return None
        entry = self._cache.get(path)
        if entry is not None and entry.get("mtime") == mtime:
            return entry
        try:
            headers, rows = _read_tracks(path)
        except Exception:
            logger.exception("Search: could not read %s", path)
            return None
        try:
            tag = get_tag_display(path)
        except Exception:
            tag = ""
        ti = _pick_header_index(headers, _TITLE_HEADER_ALIASES)
        ai = _pick_header_index(headers, _ARTIST_HEADER_ALIASES)
        bi = _pick_header_index(headers, _ALBUM_HEADER_ALIASES)
        indexed = []
        for row in rows:
            def _cell(idx: int | None) -> str:
                if idx is None or idx >= len(row):
                    return ""
                v = row[idx]
                return "" if v is None else str(v)
            t = _cell(ti).strip()
            a = _cell(ai).strip()
            al = _cell(bi).strip()
            if not (t or a):
                continue
            indexed.append({
                "title": t,
                "artist": a,
                "album": al,
                "title_lc": t.lower(),
                "artist_lc": a.lower(),
            })
        entry = {"mtime": mtime, "tag": tag, "rows": indexed}
        self._cache[path] = entry
        return entry

    @pyqtSlot()
    def _do_search(self) -> None:
        query = self._query.text().strip().lower()
        if not query:
            self._table.setRowCount(0)
            self._summary_label.setText("Type a query and press Enter.")
            return
        if self._title_rb.isChecked():
            scope = "title"
        elif self._artist_rb.isChecked():
            scope = "artist"
        else:
            scope = "both"
        # Tokenize on whitespace so "radiohead bends" matches a row whose
        # combined fields contain both words (in any order).
        tokens = [t for t in query.split() if t]

        results = []
        scanned = 0
        pattern = os.path.join(OUTPUT_DIR, "**", "*.xlsx")
        for path in sorted(glob.glob(pattern, recursive=True)):
            entry = self._scan_path(path)
            if entry is None:
                continue
            scanned += 1
            for r in entry["rows"]:
                if scope == "title":
                    haystack = r["title_lc"]
                elif scope == "artist":
                    haystack = r["artist_lc"]
                else:
                    haystack = r["title_lc"] + " " + r["artist_lc"]
                if all(tok in haystack for tok in tokens):
                    results.append({
                        "path": path,
                        "tag": entry["tag"],
                        "title": r["title"],
                        "artist": r["artist"],
                        "album": r["album"],
                    })
        results.sort(
            key=lambda r: (r["title"].lower(), r["artist"].lower(),
                           r["path"].lower())
        )

        self._table.setSortingEnabled(False)
        self._table.setRowCount(len(results))
        for i, r in enumerate(results):
            title_item = QTableWidgetItem(r["title"])
            title_item.setData(Qt.ItemDataRole.UserRole, r["path"])
            artist_item = QTableWidgetItem(r["artist"])
            album_item = QTableWidgetItem(r["album"])
            sheet_item = QTableWidgetItem(os.path.basename(r["path"]))
            sheet_item.setToolTip(r["path"])
            tag_item = QTableWidgetItem(r["tag"])
            tag_item.setToolTip(r["tag"])
            self._table.setItem(i, self.COL_TITLE, title_item)
            self._table.setItem(i, self.COL_ARTIST, artist_item)
            self._table.setItem(i, self.COL_ALBUM, album_item)
            self._table.setItem(i, self.COL_SPREADSHEET, sheet_item)
            self._table.setItem(i, self.COL_TAG, tag_item)
        self._table.setSortingEnabled(True)
        sheet_count = len({r["path"] for r in results})
        self._summary_label.setText(
            f"{len(results)} match{'es' if len(results) != 1 else ''} "
            f"across {sheet_count} spreadsheet{'s' if sheet_count != 1 else ''} "
            f"(scanned {scanned})"
        )

    @pyqtSlot(QTableWidgetItem)
    def _on_double_click(self, item: QTableWidgetItem) -> None:
        title_item = self._table.item(item.row(), self.COL_TITLE)
        if title_item is None:
            return
        path = str(title_item.data(Qt.ItemDataRole.UserRole) or "")
        if path and os.path.exists(path):
            self.spreadsheet_activated.emit(path)


# ---------------------------------------------------------------------------
# Spreadsheet detail: tracks table for a single .xlsx.
# ---------------------------------------------------------------------------
class SpreadsheetDetailWidget(QWidget):
    # Stored on each QTableWidgetItem so sorting doesn't mislead delete.
    ROLE_DATA_ROW = Qt.ItemDataRole.UserRole + 10

    back_requested = pyqtSignal()
    update_requested = pyqtSignal(str)      # path
    metadata_requested = pyqtSignal(str)    # path
    filter_requested = pyqtSignal(str)      # path
    duplicates_requested = pyqtSignal(str)  # path
    curate_requested = pyqtSignal(str)      # path
    create_requested = pyqtSignal(str)      # path — push spreadsheet to a new Amazon playlist
    rows_deleted = pyqtSignal(str, int)     # (path, count) — emitted after successful delete
    # (path, [{title, artist}]) — delete selected songs from the Amazon Music playlist
    delete_from_amazon_requested = pyqtSignal(str, object)
    # Same as above but user already confirmed — skip the second confirmation dialog
    delete_from_amazon_confirmed = pyqtSignal(str, object)
    # Reconcile: make xlsx match playlist (playlist is source of truth)
    reconcile_from_playlist_requested = pyqtSignal(str)
    # Reconcile: make playlist match xlsx (xlsx is source of truth)
    reconcile_to_playlist_requested = pyqtSignal(str)
    # Reorder: rewrite xlsx rows to match the playlist's current track order
    reorder_xlsx_from_playlist_requested = pyqtSignal(str)
    # Reorder: drag-and-drop the Amazon playlist tracks to match the xlsx row order
    reorder_playlist_from_xlsx_requested = pyqtSignal(str)

    def __init__(self) -> None:
        super().__init__()

        self._current_path: str | None = None
        # (table_row, table_col) -> new text. Populated on itemChanged.
        self._dirty_cells: dict[tuple[int, int], str] = {}
        self._loading = False
        self._actions_enabled = True
        self._headers: list[str] = []
        self._total_rows: int = 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        header = QHBoxLayout()
        self._back_btn = QPushButton("\u2190 Back")
        self._back_btn.clicked.connect(self._on_back_clicked)
        self._title_label = QLabel("")
        title_font = self._title_label.font()
        title_font.setBold(True)
        self._title_label.setFont(title_font)
        self._tag_label = QLabel("")
        self._count_label = QLabel("")
        self._save_btn = QPushButton("Save")
        self._save_btn.clicked.connect(self._on_save_clicked)
        self._save_btn.setEnabled(False)
        self._open_excel_btn = QPushButton("Open in Excel")
        self._open_excel_btn.clicked.connect(self._on_open_excel_clicked)
        header.addWidget(self._back_btn)
        header.addSpacing(12)
        header.addWidget(self._title_label)
        header.addSpacing(8)
        header.addWidget(self._tag_label)
        header.addStretch(1)
        header.addWidget(self._count_label)
        header.addSpacing(12)
        header.addWidget(self._save_btn)
        header.addWidget(self._open_excel_btn)
        layout.addLayout(header)

        actions_row = QHBoxLayout()
        actions_row.addWidget(QLabel("Actions:"))
        self._update_btn = QPushButton("Update")
        self._update_btn.setToolTip(
            "Re-scrape this playlist on Amazon Music and add new songs"
        )
        self._update_btn.clicked.connect(self._on_update_clicked)
        self._metadata_btn = QPushButton("Metadata")
        self._metadata_btn.setToolTip(
            "Look up missing release year / genre via MusicBrainz"
        )
        self._metadata_btn.clicked.connect(self._on_metadata_clicked)
        self._filter_btn = QPushButton("Filter")
        self._filter_btn.setToolTip(
            "Filter songs by year / genre / artist / keyword into a new spreadsheet"
        )
        self._filter_btn.clicked.connect(self._on_filter_clicked)
        self._duplicates_btn = QPushButton("Duplicates")
        self._duplicates_btn.setToolTip(
            "Find and review potential duplicate songs (fuzzy title+artist match)"
        )
        self._duplicates_btn.clicked.connect(self._on_duplicates_clicked)
        self._curate_btn = QPushButton("Curate")
        self._curate_btn.setToolTip(
            "Hand-pick songs from this spreadsheet into a new or existing one"
        )
        self._curate_btn.clicked.connect(self._on_curate_clicked)
        self._create_btn = QPushButton("Create Playlist")
        self._create_btn.setToolTip(
            "Push these songs into a new Amazon Music playlist (requires Chrome connected)"
        )
        self._create_btn.clicked.connect(self._on_create_clicked)
        self._delete_rows_btn = QPushButton("Delete Selected")
        self._delete_rows_btn.setToolTip(
            "Remove the selected song rows from this spreadsheet"
        )
        self._delete_rows_btn.clicked.connect(self._on_delete_rows_clicked)
        self._delete_amazon_btn = QPushButton("Delete from Amazon")
        self._delete_amazon_btn.setToolTip(
            "Remove the selected songs from the Amazon Music playlist "
            "(requires Chrome connected and a playlist source URL on this spreadsheet)"
        )
        self._delete_amazon_btn.clicked.connect(self._on_delete_from_amazon_clicked)
        self._reconcile_btn = QPushButton("Reconcile ▾")
        self._reconcile_btn.setToolTip(
            "Synchronise this spreadsheet and its linked Amazon Music playlist\n"
            "(requires Chrome connected and a playlist source URL)"
        )
        self._reconcile_btn.clicked.connect(self._on_reconcile_btn_clicked)
        actions_row.addWidget(self._update_btn)
        actions_row.addWidget(self._metadata_btn)
        actions_row.addWidget(self._filter_btn)
        actions_row.addWidget(self._duplicates_btn)
        actions_row.addWidget(self._curate_btn)
        actions_row.addWidget(self._create_btn)
        actions_row.addWidget(self._delete_rows_btn)
        actions_row.addWidget(self._delete_amazon_btn)
        actions_row.addWidget(self._reconcile_btn)
        actions_row.addStretch(1)
        layout.addLayout(actions_row)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Search:"))
        self._filter_edit = QLineEdit()
        self._filter_edit.setPlaceholderText("Type to filter rows by any column…")
        self._filter_edit.textChanged.connect(self._on_filter_text_changed)
        self._filter_clear_btn = QPushButton("Clear filter")
        self._filter_clear_btn.setEnabled(False)
        self._filter_clear_btn.clicked.connect(self._filter_edit.clear)
        filter_row.addWidget(self._filter_edit, 1)
        filter_row.addWidget(self._filter_clear_btn)
        layout.addLayout(filter_row)

        self._table = QTableWidget(0, 0)
        self._table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self._table.setSortingEnabled(True)
        self._table.verticalHeader().setVisible(False)
        self._table.itemChanged.connect(self._on_item_changed)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(
            self._on_table_context_menu
        )
        layout.addWidget(self._table)

    def load(self, path: str) -> None:
        is_new_file = path != self._current_path
        self._current_path = path
        self._dirty_cells.clear()
        self._save_btn.setEnabled(False)
        # Only reset the filter when opening a different file.  Reloads of the
        # same file (e.g. after a row deletion) preserve the active filter so
        # the user stays in their filtered view.
        if is_new_file:
            self._filter_edit.blockSignals(True)
            self._filter_edit.clear()
            self._filter_edit.blockSignals(False)

        self._title_label.setText(os.path.basename(path))
        try:
            self._tag_label.setText(get_tag_display(path))
        except Exception:
            self._tag_label.setText("")

        headers, rows = _read_tracks(path)
        self._headers = headers
        self._loading = True

        # Look up web-restricted entries for this spreadsheet's source playlist,
        # then build a set of (title_lower, artist_lower) keys we'll match
        # against during row population to apply the visual flag. Match by
        # title+artist (not row index) because row indices drift when the
        # playlist is re-exported.
        restricted_keys: set[tuple[str, str]] = set()
        restricted_tooltip = ""
        try:
            import web_restricted
            src = get_source(path)
            url = (src.get("url") or "").strip() if isinstance(src, dict) else ""
            if url:
                for e in web_restricted.get_for_playlist(url):
                    t = (e.get("title") or "").strip().lower()
                    a = (e.get("artist") or "").strip().lower()
                    if t and a:
                        restricted_keys.add((t, a))
                if restricted_keys:
                    restricted_tooltip = (
                        "Web-restricted: Amazon plays a different track on web.\n"
                        "The same row plays correctly in the Windows desktop app.\n"
                        "Use Tools → Review web-restricted tracks to remove."
                    )
        except Exception:
            logger.exception(
                "web_restricted lookup failed for %s (non-fatal)", path,
            )

        # Find which columns hold the title and artist text so we can build
        # a per-row (title, artist) key for the restricted-row match.
        def _idx_for(aliases):
            lh = [h.strip().lower() for h in headers]
            for alias in aliases:
                if alias in lh:
                    return lh.index(alias)
            return None
        title_col = _idx_for(_TITLE_HEADER_ALIASES)
        artist_col = _idx_for(_ARTIST_HEADER_ALIASES)

        # Visual treatment for restricted rows — a soft amber tint that's
        # visible without dominating the table. Picked to remain legible in
        # both light and dark themes; toned down vs alert-red so the table
        # doesn't shout when a single row is flagged.
        restricted_bg = QColor(255, 230, 180, 90)

        # ResizeToContents on a 1000+ row table forces Qt to measure every
        # cell on every column and freezes the UI for many seconds — we use
        # Interactive with sensible defaults instead, stretch the song title.
        # blockSignals + setUpdatesEnabled skip per-cell itemChanged fan-out
        # and repaint churn while we populate.
        self._table.setSortingEnabled(False)
        self._table.blockSignals(True)
        self._table.setUpdatesEnabled(False)
        try:
            self._table.setColumnCount(len(headers))
            self._table.setHorizontalHeaderLabels(headers)
            self._table.setRowCount(len(rows))
            for r, row in enumerate(rows):
                # Determine if this row is web-restricted (cheap: set lookup).
                is_restricted = False
                if restricted_keys and title_col is not None and artist_col is not None:
                    t = (str(row[title_col]) if row[title_col] is not None else "").strip().lower()
                    a = (str(row[artist_col]) if row[artist_col] is not None else "").strip().lower()
                    if (t, a) in restricted_keys:
                        is_restricted = True
                for c, value in enumerate(row):
                    item = QTableWidgetItem()
                    if value is None:
                        item.setText("")
                    elif isinstance(value, bool):
                        item.setText(str(value))
                    elif isinstance(value, (int, float)):
                        item.setData(Qt.ItemDataRole.DisplayRole, value)
                    else:
                        item.setText(str(value))
                    # Stamp each cell with its original data-row index so the
                    # delete path survives any user-triggered column sort.
                    item.setData(self.ROLE_DATA_ROW, r)
                    if is_restricted:
                        item.setBackground(restricted_bg)
                        if restricted_tooltip:
                            item.setToolTip(restricted_tooltip)
                    self._table.setItem(r, c, item)
            h = self._table.horizontalHeader()
            h.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
            h.setStretchLastSection(False)
            default_widths = [48, 260, 180, 180, 70, 120, 80]
            for c in range(len(headers)):
                w = default_widths[c] if c < len(default_widths) else 120
                self._table.setColumnWidth(c, w)
        finally:
            self._table.setUpdatesEnabled(True)
            self._table.blockSignals(False)
            self._table.setSortingEnabled(True)
            self._loading = False
        self._total_rows = len(rows)
        # Re-apply any active filter (e.g. after a reload triggered by Update/delete).
        if self._filter_edit.text().strip():
            self._on_filter_text_changed(self._filter_edit.text())
        else:
            self._update_count_label()

    # --- Live filter --------------------------------------------------------
    def _update_count_label(self) -> None:
        query = self._filter_edit.text().strip() if hasattr(self, "_filter_edit") else ""
        if query:
            visible = sum(
                1 for r in range(self._table.rowCount())
                if not self._table.isRowHidden(r)
            )
            self._count_label.setText(
                f"{visible} of {self._total_rows} "
                f"track{'s' if self._total_rows != 1 else ''}"
            )
        else:
            self._count_label.setText(
                f"{self._total_rows} track{'s' if self._total_rows != 1 else ''}"
            )

    @pyqtSlot(str)
    def _on_filter_text_changed(self, text: str) -> None:
        query = text.strip().lower()
        self._filter_clear_btn.setEnabled(bool(query))
        self._table.setUpdatesEnabled(False)
        try:
            for r in range(self._table.rowCount()):
                if not query:
                    self._table.setRowHidden(r, False)
                    continue
                match = False
                for c in range(self._table.columnCount()):
                    item = self._table.item(r, c)
                    if item and query in item.text().lower():
                        match = True
                        break
                self._table.setRowHidden(r, not match)
        finally:
            self._table.setUpdatesEnabled(True)
        self._update_count_label()

    # --- Dirty tracking / save ---------------------------------------------
    @pyqtSlot(QTableWidgetItem)
    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._loading:
            return
        self._dirty_cells[(item.row(), item.column())] = item.text()
        self._save_btn.setEnabled(True)

    @pyqtSlot()
    def _on_save_clicked(self) -> None:
        if not self._current_path or not self._dirty_cells:
            return
        try:
            _write_cell_edits(self._current_path, self._dirty_cells)
        except Exception as e:
            logger.exception("Failed to save spreadsheet edits")
            QMessageBox.critical(self, "Save failed",
                                 f"Could not save: {e}")
            return
        count = len(self._dirty_cells)
        self._dirty_cells.clear()
        self._save_btn.setEnabled(False)
        logger.info("Saved %d edit%s to %s",
                    count, "" if count == 1 else "s",
                    os.path.basename(self._current_path))

    @pyqtSlot()
    def _on_open_excel_clicked(self) -> None:
        if not self._current_path:
            return
        try:
            os.startfile(os.path.abspath(self._current_path))
        except OSError as e:
            logger.exception("Could not open externally")
            QMessageBox.warning(self, "Open failed",
                                f"Could not open in Excel: {e}")

    def set_actions_enabled(self, enabled: bool, *,
                            browser_connected: bool = False) -> None:
        """Called by MainWindow to toggle action buttons during runs."""
        self._actions_enabled = enabled
        has_path = self._current_path is not None
        self._metadata_btn.setEnabled(enabled and has_path)
        self._filter_btn.setEnabled(enabled and has_path)
        self._duplicates_btn.setEnabled(enabled and has_path)
        self._curate_btn.setEnabled(enabled and has_path)
        self._update_btn.setEnabled(enabled and has_path and browser_connected)
        self._create_btn.setEnabled(enabled and has_path and browser_connected)
        self._delete_rows_btn.setEnabled(enabled and has_path)
        self._delete_amazon_btn.setEnabled(enabled and has_path and browser_connected)
        self._reconcile_btn.setEnabled(enabled and has_path and browser_connected)

    @pyqtSlot()
    def _on_update_clicked(self) -> None:
        if self._current_path and self._actions_enabled:
            self.update_requested.emit(self._current_path)

    @pyqtSlot()
    def _on_metadata_clicked(self) -> None:
        if self._current_path and self._actions_enabled:
            self.metadata_requested.emit(self._current_path)

    @pyqtSlot()
    def _on_filter_clicked(self) -> None:
        if self._current_path and self._actions_enabled:
            self.filter_requested.emit(self._current_path)

    @pyqtSlot()
    def _on_duplicates_clicked(self) -> None:
        if self._current_path and self._actions_enabled:
            self.duplicates_requested.emit(self._current_path)

    @pyqtSlot()
    def _on_curate_clicked(self) -> None:
        if self._current_path and self._actions_enabled:
            self.curate_requested.emit(self._current_path)

    @pyqtSlot()
    def _on_create_clicked(self) -> None:
        if self._current_path and self._actions_enabled:
            self.create_requested.emit(self._current_path)

    @pyqtSlot()
    def _on_delete_rows_clicked(self) -> None:
        if not self._current_path or not self._actions_enabled:
            return
        if self._dirty_cells:
            QMessageBox.information(
                self, "Unsaved edits",
                "Save or discard your current edits before deleting rows."
            )
            return
        data_rows = self._selected_data_rows()
        if not data_rows:
            QMessageBox.information(
                self, "No selection", "Select one or more song rows to delete."
            )
            return
        count = len(data_rows)
        resp = QMessageBox.question(
            self,
            "Delete songs",
            f"Delete {count} song{'s' if count != 1 else ''} from "
            f"'{os.path.basename(self._current_path)}'?\n"
            "This modifies the .xlsx file on disk.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if resp != QMessageBox.StandardButton.Yes:
            return
        try:
            _delete_rows_from_xlsx(self._current_path, data_rows)
        except Exception as e:
            logger.exception("Delete rows failed")
            QMessageBox.critical(self, "Delete failed", str(e))
            return
        logger.info("Deleted %d row%s from %s",
                    count, "" if count == 1 else "s",
                    os.path.basename(self._current_path))
        # Reload so the table reflects the new state.
        self.load(self._current_path)
        self.rows_deleted.emit(self._current_path, count)

    def _selected_data_rows(self) -> list[int]:
        """Return the set of original data-row indices in the current selection."""
        seen: set[int] = set()
        for idx in self._table.selectedIndexes():
            item = self._table.item(idx.row(), idx.column())
            if item is None:
                continue
            data_row = item.data(self.ROLE_DATA_ROW)
            if data_row is None:
                seen.add(idx.row())
            else:
                seen.add(int(data_row))
        return sorted(seen)

    def _selected_songs_for_amazon(self) -> list[dict]:
        """Return [{title, artist, row, occurrence_index}] for the current selection.

        Finds title, artist, and # columns by matching against known header aliases.
        occurrence_index is the 0-based count of songs with the same title+artist
        that appear at earlier playlist positions — used by delete_songs_from_playlist
        to target the correct duplicate when the same song appears more than once.
        Returns an empty list if the title column cannot be identified.
        """
        norm = [h.strip().lower() for h in self._headers]

        title_col: int | None = None
        for alias in _TITLE_HEADER_ALIASES:
            if alias in norm:
                title_col = norm.index(alias)
                break
        if title_col is None:
            for i, h in enumerate(norm):
                if any(a in h for a in _TITLE_HEADER_ALIASES):
                    title_col = i
                    break

        artist_col: int | None = None
        for alias in _ARTIST_HEADER_ALIASES:
            if alias in norm:
                artist_col = norm.index(alias)
                break
        if artist_col is None:
            for i, h in enumerate(norm):
                if any(a in h for a in _ARTIST_HEADER_ALIASES):
                    artist_col = i
                    break

        if title_col is None:
            return []

        # Locate the playlist-position (#) column so we can compute occurrence_index.
        num_col: int | None = None
        for i, h in enumerate(norm):
            if h in ("#", "no", "no.", "num", "number", "track #", "track number"):
                num_col = i
                break

        def _read_pos(vr: int) -> int | None:
            """Return the integer playlist position for visual row vr, or None."""
            if num_col is None:
                return None
            it = self._table.item(vr, num_col)
            if it is None:
                return None
            raw = it.data(Qt.ItemDataRole.DisplayRole)
            if raw is None:
                raw = it.text()
            try:
                return int(float(str(raw).strip()))
            except (ValueError, TypeError):
                return None

        def _vis_row_for(data_row: int) -> int:
            for vr in range(self._table.rowCount()):
                it = self._table.item(vr, 0)
                if it is not None and int(it.data(self.ROLE_DATA_ROW) or 0) == data_row:
                    return vr
            return data_row

        # Build a full snapshot of every row's (playlist_pos, norm_title, norm_artist)
        # so we can count preceding occurrences for each selected song.
        all_rows: list[tuple[int | None, str, str]] = []
        for vr in range(self._table.rowCount()):
            t_it = self._table.item(vr, title_col)
            a_it = self._table.item(vr, artist_col) if artist_col is not None else None
            t = t_it.text().strip().lower() if t_it else ""
            a = a_it.text().strip().lower() if a_it else ""
            all_rows.append((_read_pos(vr), t, a))

        songs: list[dict] = []
        for data_row in self._selected_data_rows():
            vis_row = _vis_row_for(data_row)

            title_item = self._table.item(vis_row, title_col)
            title = title_item.text().strip() if title_item else ""
            if not title:
                continue

            artist = ""
            if artist_col is not None:
                a_item = self._table.item(vis_row, artist_col)
                artist = a_item.text().strip() if a_item else ""

            playlist_pos = _read_pos(vis_row)
            occurrence_index = 0
            if playlist_pos is not None:
                nt, na = title.lower(), artist.lower()
                for pos, t, a in all_rows:
                    if pos is not None and pos < playlist_pos and t == nt and a == na:
                        occurrence_index += 1

            songs.append({
                "title": title, "artist": artist, "row": data_row,
                "occurrence_index": occurrence_index,
            })
        return songs

    @pyqtSlot()
    def _on_delete_from_amazon_clicked(self) -> None:
        if not self._current_path or not self._actions_enabled:
            return
        songs = self._selected_songs_for_amazon()
        if not songs:
            if not self._headers:
                QMessageBox.information(
                    self, "No data", "Load a spreadsheet first."
                )
            elif self._selected_data_rows():
                QMessageBox.warning(
                    self, "No title column",
                    "Could not identify a Title column in this spreadsheet."
                )
            else:
                QMessageBox.information(
                    self, "No selection", "Select one or more song rows first."
                )
            return
        self.delete_from_amazon_requested.emit(self._current_path, songs)

    @pyqtSlot()
    def _on_delete_from_both_clicked(self) -> None:
        if not self._current_path or not self._actions_enabled:
            return
        if self._dirty_cells:
            QMessageBox.information(
                self, "Unsaved edits",
                "Save or discard your current edits before deleting rows."
            )
            return
        songs = self._selected_songs_for_amazon()
        data_rows = self._selected_data_rows()
        if not data_rows:
            QMessageBox.information(self, "No selection", "Select one or more song rows first.")
            return
        if not songs:
            QMessageBox.warning(
                self, "No title column",
                "Could not identify a Title column in this spreadsheet."
            )
            return
        n = len(data_rows)
        resp = QMessageBox.warning(
            self,
            "Delete from spreadsheet and Amazon Music",
            f"Delete {n} song{'s' if n != 1 else ''} from both "
            f"'{os.path.basename(self._current_path)}' and the Amazon Music playlist?\n\n"
            "Songs are removed from Amazon Music first; the spreadsheet rows are "
            "only deleted for songs that were successfully removed. Cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if resp != QMessageBox.StandardButton.Yes:
            return
        self.delete_from_amazon_confirmed.emit(self._current_path, songs)

    @pyqtSlot()
    def _on_reconcile_btn_clicked(self) -> None:
        if not self._current_path or not self._actions_enabled:
            return
        menu = QMenu(self._reconcile_btn)
        act_from = menu.addAction("Make spreadsheet match playlist  (Spreadsheet ← Playlist)")
        act_from.setToolTip(
            "Scrape the linked Amazon Music playlist and update this spreadsheet "
            "to match: add songs that are in the playlist but not here, "
            "remove songs that are no longer in the playlist."
        )
        act_to = menu.addAction("Make playlist match spreadsheet  (Playlist ← Spreadsheet)")
        act_to.setToolTip(
            "Update the Amazon Music playlist to match this spreadsheet: "
            "add missing songs, remove surplus songs."
        )
        menu.addSeparator()
        act_reorder_xlsx = menu.addAction("Reorder spreadsheet to match playlist  (Spreadsheet order ← Playlist)")
        act_reorder_xlsx.setToolTip(
            "Rewrite the spreadsheet rows so their order matches the current "
            "Amazon Music playlist order. Songs not in the playlist are moved to the end."
        )
        act_reorder_pl = menu.addAction("Reorder playlist to match spreadsheet  (Playlist order ← Spreadsheet)")
        act_reorder_pl.setToolTip(
            "Drag-and-drop tracks in the Amazon Music playlist to match the "
            "row order of this spreadsheet."
        )
        btn_rect = self._reconcile_btn.rect()
        chosen = menu.exec(
            self._reconcile_btn.mapToGlobal(btn_rect.bottomLeft())
        )
        if chosen is act_from:
            self.reconcile_from_playlist_requested.emit(self._current_path)
        elif chosen is act_to:
            self.reconcile_to_playlist_requested.emit(self._current_path)
        elif chosen is act_reorder_xlsx:
            self.reorder_xlsx_from_playlist_requested.emit(self._current_path)
        elif chosen is act_reorder_pl:
            self.reorder_playlist_from_xlsx_requested.emit(self._current_path)

    @pyqtSlot(QPoint)
    def _on_table_context_menu(self, pos: QPoint) -> None:
        data_rows = self._selected_data_rows()
        if not data_rows:
            return
        n = len(data_rows)
        menu = QMenu(self._table)
        act_delete = menu.addAction(
            f"Delete {n} row{'s' if n != 1 else ''} from spreadsheet"
        )
        act_delete.setEnabled(self._actions_enabled)
        act_delete_amazon = menu.addAction(
            f"Delete {n} song{'s' if n != 1 else ''} from Amazon Music playlist"
        )
        act_delete_amazon.setEnabled(self._actions_enabled and self._delete_amazon_btn.isEnabled())
        act_delete_both = menu.addAction(
            f"Delete {n} song{'s' if n != 1 else ''} from spreadsheet and Amazon Music"
        )
        act_delete_both.setEnabled(self._actions_enabled and self._delete_amazon_btn.isEnabled())
        chosen = menu.exec(self._table.viewport().mapToGlobal(pos))
        if chosen is act_delete:
            self._on_delete_rows_clicked()
        elif chosen is act_delete_amazon:
            self._on_delete_from_amazon_clicked()
        elif chosen is act_delete_both:
            self._on_delete_from_both_clicked()

    @pyqtSlot()
    def _on_back_clicked(self) -> None:
        if self._dirty_cells:
            resp = QMessageBox.question(
                self,
                "Unsaved changes",
                f"You have {len(self._dirty_cells)} unsaved edit"
                f"{'' if len(self._dirty_cells) == 1 else 's'}. Save before leaving?",
                QMessageBox.StandardButton.Save
                | QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Save,
            )
            if resp == QMessageBox.StandardButton.Cancel:
                return
            if resp == QMessageBox.StandardButton.Save:
                self._on_save_clicked()
                if self._dirty_cells:  # save failed
                    return
            else:
                self._dirty_cells.clear()
                self._save_btn.setEnabled(False)
        self.back_requested.emit()


def _coerce_edit_value(text: str, original):
    """Convert an edited cell's text back to a sensible Python type.

    Preserves int/float when the text parses as a number; otherwise stores
    the string as-is. `original` is kept for future use (e.g. preserving
    explicit None/empty distinctions) — unused for now.
    """
    del original
    s = text.strip()
    if s == "":
        return None
    try:
        if "." not in s and "e" not in s.lower():
            return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return text


def _write_cell_edits(path: str, edits: dict[tuple[int, int], str]) -> None:
    """Apply table edits back to the first non-metadata sheet of *path*.

    Table coords are 0-based over the data rows; openpyxl is 1-based and
    the first sheet row is the header, so (r, c) -> (r + 2, c + 1).
    Loads the workbook non-read-only so existing styles and the hidden
    `_metadata` sheet survive.
    """
    wb = load_workbook(path)
    try:
        sheet_name = next(
            (s for s in wb.sheetnames if s != TAG_SHEET),
            wb.sheetnames[0] if wb.sheetnames else None,
        )
        if sheet_name is None:
            raise RuntimeError("Workbook has no data sheet")
        ws = wb[sheet_name]
        for (r, c), text in edits.items():
            cell = ws.cell(row=r + 2, column=c + 1)
            cell.value = _coerce_edit_value(text, cell.value)
        wb.save(path)
    finally:
        wb.close()


def _delete_rows_from_xlsx(path: str, data_rows: list[int]) -> None:
    """Remove *data_rows* (0-based data indices) from the track sheet.

    Keeps the hidden `_metadata` sheet intact, preserves styles (non-read-only
    load), deletes rows bottom-up, then renumbers the '#' column so it stays
    a contiguous 1..N sequence.
    """
    if not data_rows:
        return
    wb = load_workbook(path)
    try:
        sheet_name = next(
            (s for s in wb.sheetnames if s != TAG_SHEET),
            wb.sheetnames[0] if wb.sheetnames else None,
        )
        if sheet_name is None:
            raise RuntimeError("Workbook has no data sheet")
        ws = wb[sheet_name]
        # Delete bottom-up so earlier indices stay valid.
        for dr in sorted(set(data_rows), reverse=True):
            ws.delete_rows(dr + 2)
        # Renumber column 1 ('#'). Only touch rows that already held a value
        # there so we don't clobber extra header/footer content on weird sheets.
        for i, row in enumerate(
            ws.iter_rows(min_row=2, max_col=1), start=1
        ):
            row[0].value = i
        wb.save(path)
    finally:
        wb.close()


def _read_tracks(path: str) -> tuple[list[str], list[list]]:
    """Return (headers, rows) from the first non-metadata sheet in *path*."""
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        sheet_name = next(
            (s for s in wb.sheetnames if s != TAG_SHEET),
            wb.sheetnames[0] if wb.sheetnames else None,
        )
        if sheet_name is None:
            return [], []
        ws = wb[sheet_name]
        rows_iter = ws.iter_rows(values_only=True)
        try:
            header_row = next(rows_iter)
        except StopIteration:
            return [], []
        headers = [str(h) if h is not None else "" for h in header_row]
        rows = [list(r) for r in rows_iter if any(c is not None for c in r)]
        return headers, rows
    finally:
        wb.close()


def _read_xlsx_songs_for_reconcile(path: str) -> list[dict]:
    """Return [{"title": str, "artist": str, "row": int}] from the first data sheet."""
    headers, rows = _read_tracks(path)
    norm = [h.strip().lower() for h in headers]

    title_col: int | None = None
    for alias in _TITLE_HEADER_ALIASES:
        if alias in norm:
            title_col = norm.index(alias)
            break
    if title_col is None:
        for i, h in enumerate(norm):
            if any(a in h for a in _TITLE_HEADER_ALIASES):
                title_col = i
                break

    artist_col: int | None = None
    for alias in _ARTIST_HEADER_ALIASES:
        if alias in norm:
            artist_col = norm.index(alias)
            break
    if artist_col is None:
        for i, h in enumerate(norm):
            if any(a in h for a in _ARTIST_HEADER_ALIASES):
                artist_col = i
                break

    if title_col is None:
        return []

    result = []
    for i, row in enumerate(rows):
        title = str(row[title_col]).strip() if row[title_col] is not None else ""
        artist = (
            str(row[artist_col]).strip()
            if artist_col is not None and row[artist_col] is not None
            else ""
        )
        if title:
            result.append({"title": title, "artist": artist, "row": i})
    return result


# ---------------------------------------------------------------------------
# Classical Shuffle: performance-pool builder
# ---------------------------------------------------------------------------
# A "performance" is a contiguous run of tracks within one playlist that share
# the same (artist, album) — typically one orchestra/conductor's recording of
# a multi-movement piece. Classical Shuffle picks performances at random from
# the pool, plays each in order via play_track_at_index, and advances when
# the now-playing track's (artist, album) leaves the performance's set.
#
# Built from cached xlsx exports (looked up by source_url) — playlists without
# a cached xlsx are skipped and listed in the returned `skipped` list. We use
# xlsx for the *grouping decision* only; at click time the live tracklist is
# re-scraped (so reorders since export are tolerated).


def _artist_tokens(s: str) -> set[str]:
    """Tokenize an artist string into lowercase artist *phrases*.

    Splits ONLY on Amazon's multi-artist separators — comma, ampersand, and
    forward slash — and keeps multi-word artist names intact as one token.
    So 'Berliner Philharmoniker, Herbert von Karajan & Ludwig van Beethoven'
    becomes {'berliner philharmoniker', 'herbert von karajan',
    'ludwig van beethoven'} rather than dissolving into individual words.

    Word-level tokenization would let unrelated artists with a shared word
    (e.g. two different 'Some Pianist' / 'Different Pianist') falsely match
    on the common word; phrase-level tokenization requires the entire name
    to match. Used by both the grouper (bidirectional subset of phrases
    decides whether consecutive rows belong to the same recording) and the
    now-playing matcher (non-empty intersection of phrases plus title
    membership decides whether the player is still in the picked perf).
    """
    out: set[str] = set()
    for sep in ",&/":
        s = s.replace(sep, "\0")
    for part in s.split("\0"):
        phrase = " ".join(part.lower().split())
        if phrase:
            out.add(phrase)
    return out


def _parse_duration_to_seconds(s: str | None) -> int:
    """Parse Amazon Music's duration string into seconds.

    Accepts ``"MM:SS"`` (e.g. ``"11:52"``) and ``"H:MM:SS"`` / ``"HH:MM:SS"``
    (for tracks longer than one hour — rare in classical but possible for
    e.g. an entire Wagner opera act on a single track). Returns 0 on
    empty / None / unparseable input — chosen deliberately so a malformed
    cell in one row makes that row contribute zero rather than crashing
    pool-build for the whole playlist (see Option-A vs Option-B tradeoff
    in [[project_classical_shuffle_playlist_gen_plan]]).

    Examples::

        "11:52"     → 712
        "08:37"     → 517
        "1:02:30"   → 3750
        "1:00:00"   → 3600
        ""          → 0
        None        → 0
        "garbage"   → 0
    """
    if not s:
        return 0
    s = s.strip()
    if not s:
        return 0
    parts = s.split(":")
    if len(parts) < 2 or len(parts) > 3:
        return 0
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return 0
    if any(n < 0 for n in nums):
        return 0
    if len(nums) == 2:
        # MM:SS
        m, sec = nums
        return m * 60 + sec
    # H:MM:SS
    h, m, sec = nums
    return h * 3600 + m * 60 + sec


# A track title is "<piece>: <movement>", but BOTH halves can carry their own
# colons — the piece may have a composer prefix ("Beethoven: Symphony No. 9
# ...") and the movement may have a tempo/subdivision tail ("II. Marcia
# funebre: Adagio assai", "V. Finale: a. Im Tempo", "I. Allegro (Cadenza:
# Kempff)", "Var.10 - Intermezzo: …", "Var.14 - Finale: E.D.U."). So splitting
# on the first or the last colon both corrupt the piece identifier. The
# movement half is the first ": "-delimited segment that BEGINS with a movement
# marker, one of:
#   - a Roman/Arabic numeral + optional subdivision letter/range + period:
#     "I.", "1.", "IVc.", "IVa-b.", "V." — the trailing period keeps a piece
#     name opening with a count ("5 Pieces, Op. 10") from being a movement;
#   - a variation marker "Var.N" / "Var. N" (Elgar Enigma, theme-and-variations);
#   - the word "Theme" used as a section (not "Theme and Variations" the piece);
#   - an opening quote — a sung text incipit used as the movement name
#     (Beethoven 9 '..."Choral": "O Freunde, nicht diese Töne!"').
_MOVEMENT_SEG_RE = re.compile(
    r'^(?:'
    r'(?:[IVXLCDM]+|\d+)[a-z]?(?:-[a-z]+)?\.'      # I.  1.  IVc.  IVa-b.  V.
    r'|[Vv]ar\.?\s*\d+'                            # Var.10  Var. 1
    r'|[Tt]heme(?=\s*[-.:]|\s*$)'                  # Theme - …   Theme. …
    r'|["“‘]'                            # "O Freunde…  (quoted incipit)
    r')'
)
# Dash movement separator for colon-less titles ("... Op. 67 - Andante con
# moto"). Requires whitespace BEFORE the dash so hyphenated words ("E-Flat")
# don't match; allows zero/one space after ("- Andante" or "-Allegro").
_DASH_MOVEMENT_RE = re.compile(r"\s+-\s*")


def _split_piece_and_movement(title: str) -> tuple[str, str]:
    """Split *title* into ``(piece, movement)`` at the movement boundary.

    *movement* starts at the first ": "-delimited segment matching
    ``_MOVEMENT_SEG_RE`` (a numbered movement). If the marker is the very
    first segment — a bare "II. Larghetto" with no piece name — *piece* is "".

    When NO numbered marker is present, fall back to splitting before the
    final ": ". This preserves the pre-2026-06-15 behavior for text-incipit
    finales that carry no numeral (Beethoven 9's '..."Choral": "O Freunde,
    nicht diese Töne!"'), which must keep the same piece prefix as the
    numbered movements so the choral-finale piece_match fallback still groups
    them. Colon-less titles ("Etudes Symphoniques") return ``(title, "")``.
    """
    segments = title.split(": ")
    for i, seg in enumerate(segments):
        if _MOVEMENT_SEG_RE.match(seg.strip()):
            return ": ".join(segments[:i]), ": ".join(segments[i:])
    # No numbered movement marker — replicate the old rsplit-last-colon split
    # so text-incipit finales and other non-numbered tails are unchanged.
    if len(segments) >= 2:
        return ": ".join(segments[:-1]), segments[-1]
    # No colon at all. Some recordings use " - <tempo>" as the movement
    # separator with no numeral (e.g. Karajan's Beethoven 5 on B0015HK92Q:
    # 'Symphony No. 5 in C minor (Fate), Op. 67 - Andante con moto'). Without
    # this, each movement's whole title becomes its own prefix and the
    # recording shatters into singletons. Split at the FIRST spaced dash so
    # every movement of one recording shares a prefix; the leading-space guard
    # keeps hyphenated piece names ('E-Flat', 'C-Minor') intact.
    m = _DASH_MOVEMENT_RE.search(title)
    if m:
        return title[:m.start()], title[m.end():]
    return title, ""


def _piece_prefix(title: str) -> str:
    """Return the lowercased 'piece identifier' portion of a track title —
    everything before the movement marker (see ``_split_piece_and_movement``).
    Examples:
      'Beethoven: Symphony No. 9 ... "Choral": I. Allegro'  → 'beethoven: symphony no. 9 ... "choral"'
      'Mahler: Sym No. 2: V. Finale: a. Im Tempo'           → 'mahler: sym no. 2'
      'Symphony No. 3 "Eroica": II. Marcia funebre: Adagio' → 'symphony no. 3 "eroica"'
      'II. Larghetto'   (bare movement, no piece name)      → ''
      'Etudes Symphoniques'                                 → 'etudes symphoniques'
    Used by the grouper to tell movements of one recording (shared prefix)
    apart from a different work on the same coupling album (Grieg PC Op. 16
    vs Schumann PC Op. 54 → different prefix), and as the choral-finale
    fallback when a soprano credit replaces the orchestra credit but the
    piece prefix still matches. An empty prefix (bare movement title) makes
    the grouper's piece_changes guard permissive — such works fall back to
    album+artist grouping.

    Historic bug (fixed 2026-06-15): the old `rsplit(":", 1)[0]` took
    everything before the LAST colon, so any movement title with a second
    colon — subdivided finales ("V. Finale: a"), tempo tails ("II. Marcia
    funebre: Adagio"), cadenza notes ("(Cadenza: Kempff)") — produced a
    corrupted, per-movement-varying prefix. That made `piece_changes` fire
    spuriously and shattered whole single-album recordings (Mahler 2, Eroica,
    Beethoven PC4) into lone-movement fragments that the sampler then drew.
    """
    return _split_piece_and_movement(title)[0].strip().lower()


def _prefix_match_key(prefix: str) -> str:
    """Whitespace-insensitive key for comparing two piece prefixes.

    Amazon's metadata is inconsistent about spacing inside catalogue/opus
    numbers — the same recording can render one movement as
    'Op. 35, TH.59' and the next as 'Op. 35, TH. 59', or 'Op. 16' vs
    'Op.16'. Those differ only in whitespace, so they're the same piece; the
    grouper strips all whitespace before its piece_match / piece_changes
    equality checks so a stray space can't split a single recording (or
    fabricate a lone-movement "orphan"). Two genuinely different works still
    differ in their non-space characters, so this never over-merges.
    """
    return "".join(prefix.split())


# Catalogue/opus numbers — the most reliable identity of a classical work.
# Amazon's per-track metadata is often inconsistent ABOUT THE SURROUNDING TEXT
# while keeping the catalogue number stable: the same recording can render
# mvt I as "Trio … No. 2 in E-Flat Major, Op. 100, D. 929: I. Allegro" and
# mvt II as "Trio … in E-Flat Major, Op. 100, D. 929: II. …" (dropping the
# "No. 2"). Whitespace-insensitive prefix matching can't bridge that, but the
# catalogue numbers (Op. 100, D. 929) are identical, so they pin the work.
# Single-letter schemes (D./K.) REQUIRE a period so they don't swallow key
# signatures ("in D major"); multi-letter schemes allow an optional period.
_CATALOGUE_RE = re.compile(
    r"\b(?:"
    r"op\.?\s*\d+[a-z]?"                 # Op. 100  Op.16  Op.27a
    r"|bwv\.?\s*\d+"                     # BWV 1047
    r"|woo\.?\s*\d+"                     # WoO 59
    r"|rv\.?\s*\d+"                      # RV 269
    r"|th\.?\s*\d+"                      # TH. 59
    r"|hob\.?[\s.]*[ivxlcm]+[:.]?\s*\d*" # Hob. I:94
    r"|kv?\.?\s*\d+"                     # K. 550 / KV 550 / K550
    r"|d\.?\s*\d+"                       # D. 929 / D 759 (digit required, so
    r")",                                #   a key like "in D major" can't match)
    re.IGNORECASE,
)


def _work_signature(prefix: str) -> frozenset:
    """Set of normalized catalogue tokens in a piece prefix (e.g.
    {'op100', 'd929'}). Empty when the prefix carries no catalogue number
    (Mahler symphonies, overtures, etc.) — callers fall back to prefix
    comparison in that case. Two prefixes with the same NON-empty signature
    are the same work even if the surrounding title text differs; different
    works carry different catalogue numbers (Grieg Op. 16 vs Schumann Op. 54).

    Both spaces AND periods are stripped from each token so the same number
    renders identically whether Amazon wrote 'D. 759' or 'D 759', 'Op. 100'
    or 'Op.100' — the same inconsistency the catalogue path exists to absorb.
    """
    return frozenset(
        re.sub(r"[.\s]+", "", m.group(0).lower())
        for m in _CATALOGUE_RE.finditer(prefix)
    )


_MOVEMENT_NUM_RE = re.compile(r"^([IVXLCDM]+|\d+)[a-z]?(?=[.\s]|$)")
_ROMAN_VALUES = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}


def _movement_number(title: str) -> int | None:
    """Parse the leading Roman or Arabic numeral from a track title's
    movement designator (the part after the final ':').

    Returns the integer movement number, or ``None`` when no clear marker
    is present — overtures, single-movement works, or oddly-titled tracks
    fall into this bucket. Trailing subdivision letters (``Ia``, ``IVc``)
    are ignored; the numeric part is what matters for ordering.

    Used by the cross-album merge in ``_group_into_performances`` to block
    the over-glue case where the previous group ended on (say) mvt IV and
    the next album starts a fresh mvt I — a reset signals a new recording
    even when artist tokens and piece prefix happen to match. Callers
    should treat ``None`` on either side as "unknown — don't block on a
    missing signal."

    Examples::

        'Mahler: Symphony No. 1: I. Langsam'      → 1
        'Brahms: Symphony No. 1: II. Andante'     → 2
        'Symphony No. 9 "Choral": IVc. Presto'    → 4
        'Mahler: Symphony No. 5: V. Rondo'        → 5
        'Beethoven: Coriolan Overture'            → None
        'Trauermarsch'                            → None
    """
    movement_part = _split_piece_and_movement(title)[1].strip()
    if not movement_part:
        return None
    m = _MOVEMENT_NUM_RE.match(movement_part)
    if not m:
        return None
    token = m.group(1)
    if token.isdigit():
        return int(token)
    # Roman numeral. Subtractive notation (IV, IX, XL, etc.) handled by
    # iterating right-to-left and subtracting when a smaller value
    # precedes a larger one.
    total = 0
    prev = 0
    for ch in reversed(token):
        val = _ROMAN_VALUES.get(ch)
        if val is None:
            return None
        if val < prev:
            total -= val
        else:
            total += val
        prev = val
    return total if total > 0 else None


class Performance(NamedTuple):
    playlist_url: str
    playlist_name: str
    artist: str        # longest artist string seen in the run — most info
    album: str         # original casing — kept for display only; do NOT use
                       # for now-playing matching (Amazon's player reports the
                       # playlist name in the album field, not the source
                       # album, so player-album never matches xlsx-album).
    track_count: int   # number of contiguous tracks belonging to this performance
    start_row: int     # 0-based row index of the first track within the xlsx
    titles: tuple[str, ...]  # exact track titles (lowercased) for membership
                             # check in `_classical_perf_match`
    artist_tokens: frozenset[str] = frozenset()
    # Union of `_artist_tokens(artist)` across all rows in the run. Used by the
    # matcher when artist credits vary per track (e.g. Karajan's Sym 9 mvts
    # I-IV credit Berliner Philharmoniker, but mvt IVc-j credits Janowitz the
    # soprano — both are part of the same recording). Empty by default so
    # tests / older constructors keep working; matcher falls back to
    # tokenizing `artist` if this is empty.
    duration_seconds: int = 0
    # Sum of `_parse_duration_to_seconds(<Duration cell>)` across all tracks
    # in the run. Computed once at pool-build time so the duration-target
    # sampler (Step 5 of the playlist generator) stays pure / file-I/O-free.
    # 0 default keeps existing constructors / tests back-compat — a perf
    # with duration_seconds=0 still gets sampled, just contributes nothing
    # to the duration tally. See Option-A discussion in the plan doc.


def _build_url_to_xlsx_index(output_dir: str = OUTPUT_DIR) -> dict[str, str]:
    """Scan *output_dir* for .xlsx files, return {source_url: xlsx_path} for any
    that have a recorded source URL (Amazon-Music-tagged spreadsheets).
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
            # First-wins: if multiple xlsx files share a source URL we take the
            # earliest-listed one. Not worth surfacing as a warning for now.
            index.setdefault(url, path)
    return index


def _find_xlsx_for_playlist_url(url: str) -> list[str]:
    """Return paths of every cached xlsx whose stored source_url matches *url*."""
    if not url:
        return []
    target = url.strip()
    matches: list[str] = []
    for row in _scan_output_dir():
        try:
            src = get_source(row["path"])
        except Exception:
            continue
        if (src.get("url") or "").strip() == target:
            matches.append(row["path"])
    return matches


def _read_song_rows(path: str) -> list[tuple[str, str, str, int, str]]:
    """Return [(title, artist, album, duration_seconds, album_asin), ...] in
    track order from *path*'s data sheet.

    Empty cells become empty strings (duration_seconds becomes 0). Values
    are stripped. Used to group tracks into Performance runs and to build
    the title-membership set used by ``_classical_perf_match``. Duration
    and album_asin are looked up by header alias so xlsx files written
    before either column existed (returns "" / 0 for those) stay back-compat
    — the grouper falls back to album name when album_asin is empty.
    """
    headers, rows = _read_tracks(path)
    title_idx = _pick_header_index(headers, _TITLE_HEADER_ALIASES)
    artist_idx = _pick_header_index(headers, _ARTIST_HEADER_ALIASES)
    album_idx = _pick_header_index(headers, _ALBUM_HEADER_ALIASES)
    duration_idx = _pick_header_index(headers, ("duration",))
    album_asin_idx = _pick_header_index(headers, ("album asin",))
    if (title_idx is None and artist_idx is None
            and album_idx is None and duration_idx is None):
        return []
    out: list[tuple[str, str, str, int, str]] = []
    for row in rows:
        def _cell(idx: int | None) -> str:
            if idx is None or idx >= len(row):
                return ""
            v = row[idx]
            return "" if v is None else str(v).strip()
        dur_str = _cell(duration_idx) if duration_idx is not None else ""
        out.append((
            _cell(title_idx),
            _cell(artist_idx),
            _cell(album_idx),
            _parse_duration_to_seconds(dur_str),
            _cell(album_asin_idx),
        ))
    return out


def _read_song_asins(
    path: str, start_row: int, count: int,
) -> list[tuple[str, str]]:
    """Return ``[(track_asin, album_asin), ...]`` for the *count* rows starting
    at *start_row* (0-based, matching ``Performance.start_row``) in *path*'s
    data sheet.

    Empty / missing cells become empty strings — caller decides whether an
    empty ASIN is fatal for that track. Returns fewer than *count* entries
    only if the xlsx has fewer rows than requested (defensive — shouldn't
    happen since ``Performance.track_count`` is derived from the same xlsx
    during pool-build).

    Header-indexed via ``Track ASIN`` / ``Album ASIN`` (added by the
    2026-06-02 backfill, written by ``exporter.export_to_xlsx`` at cols H/I).
    Returns an empty list if either header is missing — pool entries from a
    pre-backfill xlsx are not addressable by ASIN and should be skipped by
    the caller.
    """
    if count <= 0:
        return []
    headers, rows = _read_tracks(path)
    track_idx = _pick_header_index(headers, ("track asin",))
    album_idx = _pick_header_index(headers, ("album asin",))
    if track_idx is None or album_idx is None:
        return []
    out: list[tuple[str, str]] = []
    for i in range(start_row, min(start_row + count, len(rows))):
        row = rows[i]

        def _cell(idx: int) -> str:
            if idx >= len(row):
                return ""
            v = row[idx]
            return "" if v is None else str(v).strip()

        out.append((_cell(track_idx), _cell(album_idx)))
    return out


def _group_into_performances(
    rows: list[tuple[str, str, str, int, str]],
    playlist_url: str,
    playlist_name: str,
) -> list[Performance]:
    """Group consecutive (title, artist, album, duration_seconds, album_asin)
    rows into Performance runs.

    Two adjacent rows merge into the same run when EITHER:

    * **Same album** AND any of:
        - the new row's artist tokens are a subset of the running group's
          (bidirectional, matching `_classical_perf_match`) AND the piece
          prefix has NOT clearly changed (see below);
        - vice versa (same guard);
        - both rows share the same piece prefix (text before the final
          ':') — fallback for the choral-finale case where a soprano
          credit replaces the orchestra credit on mvt IVc-j despite being
          the same recording.

      The piece-prefix guard on the artist-overlap branch blocks the
      Arrau Grieg PC + Schumann PC coupling-album case (2026-06-12): both
      pieces are in A minor, so Amazon renders both as "Piano Concerto in
      A Minor, Op. NN: <mvt>", and same-album + same-artist would glue
      all 6 movements into one perf. The piece prefix DOES change across
      the boundary, so the guard fires and splits them. Permissive when
      either prefix is empty — a missing signal shouldn't block merging.
      The piece_match branch isn't gated because piece_match already
      implies the prefix didn't change.

      Same-album identity is the album ASIN when present (robust against
      two distinct albums sharing a display title — e.g. both Karajan and
      Abbado's Mahler 5 rendering as "Mahler: Symphony No. 5"), falling
      back to lowercased album name for xlsx files written before the
      Album ASIN column existed.

    * **Different album** AND artist token sets are EQUAL AND piece prefix
      matches AND the album display names differ — the
      cross-album-same-recording case. Mahler 1 mvt I is often pulled from
      a compilation album ("Mahler: The Symphonies") while mvts II–IV
      live on the per-symphony album ("Mahler: Symphony No.1") even
      though it's the same Chicago Symphony recording; without this rule
      mvt I splits off as a lone single-track perf and the sampler ends
      up picking that one movement.

      Cross-album merge requires artist token EQUALITY (not just
      bidirectional subset) so that same-orchestra-different-conductor
      pairings split correctly when the conductor is in the artist
      credit — the subset rule would over-glue them via the orchestra
      phrase alone.

      Cross-album merge ALSO requires the album display names to differ
      when both rows have ASINs. The compilation-vs-per-symphony pattern
      that motivated the rule always carries distinct album names; same
      display name across different ASINs is, in practice, two distinct
      recordings of the same work on same-titled album entries (Berliner
      Phil under Karajan vs Abbado both rendering as "Mahler: Symphony
      No. 5") — the orchestra-only artist credit can't tell them apart
      otherwise.

      Cross-album merge ALSO requires the new row's movement number to
      strictly advance past the previous row's. Two different recordings
      of the same work by the same orchestra (no conductor in the
      credit) — e.g. Berliner Phil's Brahms 1 on *Complete Symphonies*
      followed by their Brahms 1 on *Brahms / Schumann Symphony No.1* —
      look identical to artist-equal + piece-match, but the boundary
      reset (mvt IV → mvt I) is what makes them distinct. Legitimate
      compilation/per-symphony splits always advance (mvt I → mvt II)
      because the compilation typically contributes only the opening
      movement. Movement number is parsed by ``_movement_number``; when
      either side returns ``None`` (single-mvt works, overtures), the
      check falls through to permissive — a missing signal shouldn't
      block merging.

    The grouper tracks the running UNION of artist tokens seen in the
    current group, so a row joins (under the same-album rule) when its
    tokens are contained in any prior row's credits in the group (or vice
    versa). Stores the longest artist string seen as `Performance.artist`.
    Sums per-row durations into `Performance.duration_seconds` so the
    duration-target sampler (Step 5 of the playlist generator) can stay
    pure.

    Rows with both artist and album empty terminate the current run
    (ungroupable — likely missing metadata).
    """
    out: list[Performance] = []
    cur_album = ""        # original casing — for display
    cur_album_key = ""    # canonical identity — ASIN when present, else lowercased name
    cur_album_asin = ""   # tracked separately to distinguish "different ASIN but same display name" cases
    cur_artist = ""       # longest artist string seen in the run
    cur_tokens: set[str] = set()  # running union of artist tokens
    cur_last_prefix = ""  # piece prefix of the most recent track
    cur_movement_num: int | None = None  # movement number of the most recent track
    cur_count = 0
    cur_start = 0
    cur_titles: list[str] = []
    cur_duration = 0      # running sum of per-track duration_seconds

    def _flush() -> None:
        nonlocal cur_album, cur_album_key, cur_album_asin, cur_artist, cur_tokens
        nonlocal cur_last_prefix, cur_movement_num, cur_count, cur_start, cur_titles
        nonlocal cur_duration
        if cur_count > 0:
            out.append(Performance(
                playlist_url, playlist_name, cur_artist, cur_album,
                cur_count, cur_start, tuple(cur_titles),
                frozenset(cur_tokens),
                cur_duration,
            ))
        cur_album = ""
        cur_album_key = ""
        cur_album_asin = ""
        cur_artist = ""
        cur_tokens = set()
        cur_last_prefix = ""
        cur_movement_num = None
        cur_count = 0
        cur_start = 0
        cur_titles = []
        cur_duration = 0

    for row_idx, (title, artist, album, duration_seconds, album_asin) in enumerate(rows):
        if not artist and not album:
            _flush()
            continue
        album_key = album_asin if album_asin else album.lower()
        new_tokens = _artist_tokens(artist)
        new_prefix = _piece_prefix(title)
        new_movement_num = _movement_number(title)
        artist_overlap = (new_tokens.issubset(cur_tokens)
                          or cur_tokens.issubset(new_tokens))
        artist_equal = bool(new_tokens) and new_tokens == cur_tokens
        # Compare prefixes whitespace-insensitively so Amazon's inconsistent
        # spacing inside catalogue/opus numbers ('TH.59' vs 'TH. 59') can't
        # split one recording — see _prefix_match_key.
        new_pkey = _prefix_match_key(new_prefix)
        cur_pkey = _prefix_match_key(cur_last_prefix)
        piece_match = bool(new_prefix) and new_pkey == cur_pkey
        # Same-album fallback: two rows whose surrounding title text differs
        # (e.g. "No. 2" present on mvt I, dropped on mvts II-IV) but that carry
        # the SAME catalogue number(s) are the same work. Requires artist
        # overlap so a cross-composer same-opus coincidence on a compilation
        # can't glue two genuinely different pieces.
        new_sig = _work_signature(new_prefix)
        cur_sig = _work_signature(cur_last_prefix)
        sig_match = bool(new_sig) and new_sig == cur_sig
        work_match = sig_match and (
            new_tokens.issubset(cur_tokens) or cur_tokens.issubset(new_tokens)
        )
        # On a coupling album (e.g. Arrau's Grieg PC + Schumann PC, both
        # rendered by Amazon as "Piano Concerto in A Minor, Op. NN: ..."),
        # the same artist plays two different works back-to-back. The
        # artist-overlap branch alone would glue them into a single perf.
        # Block that branch when the piece prefix clearly changes. The
        # piece_match branch handles the choral-finale case below
        # independently, so the soprano-credit fallback still works.
        piece_changes = (
            bool(new_prefix) and bool(cur_last_prefix)
            and new_pkey != cur_pkey
        )
        same_album = album_key == cur_album_key
        # When both rows have ASINs that differ but the album display
        # names match, treat them as distinct recordings on same-titled
        # album entries rather than as a compilation/per-symphony split.
        # The cross-album rule that follows is intended for the
        # compilation pattern, which always carries DISTINCT album names.
        both_have_asin = bool(album_asin) and bool(cur_album_asin)
        same_album_name = bool(album) and bool(cur_album) and album.lower() == cur_album.lower()
        likely_distinct_recordings = (
            both_have_asin and same_album_name and album_asin != cur_album_asin
        )
        # Movement reset across the album boundary (mvt IV → mvt I) means
        # a new performance is starting, even when artist tokens and piece
        # prefix happen to match. Legitimate compilation/per-symphony
        # splits always advance (mvt I → mvt II). Permissive when either
        # side has no parseable movement number — overtures and
        # single-mvt works shouldn't be blocked by a missing signal.
        movement_advances = (
            cur_movement_num is None
            or new_movement_num is None
            or new_movement_num > cur_movement_num
        )
        # Cross-album merge accepts EITHER a title (piece_prefix) match OR a
        # catalogue-signature match (same Op./D./BWV number). The signature
        # path catches the same recording catalogued under two album entries
        # with inconsistently-named titles — e.g. Perlman's Tchaikovsky VC as
        # "Violin Concerto, Op.35 (1985 - Remaster): I." on one album and
        # "Violin Concerto in D Major, Op. 35: II." on another. Still gated by
        # artist EQUALITY, a strict movement advance (a IV→I reset means a new
        # recording, not a continuation), and the distinct-recordings guard.
        cross_album_same_recording = (
            not same_album
            and not likely_distinct_recordings
            and artist_equal and (piece_match or sig_match)
            and movement_advances
        )
        if cur_count > 0 and (
            (same_album and ((artist_overlap and not piece_changes)
                             or piece_match or work_match))
            or cross_album_same_recording
        ):
            cur_count += 1
            cur_titles.append(title.lower())
            cur_tokens |= new_tokens
            cur_last_prefix = new_prefix
            if new_movement_num is not None:
                cur_movement_num = new_movement_num
            cur_duration += duration_seconds
            if len(artist) > len(cur_artist):
                cur_artist = artist
        else:
            _flush()
            cur_album = album
            cur_album_key = album_key
            cur_album_asin = album_asin
            cur_artist = artist
            cur_tokens = new_tokens
            cur_last_prefix = new_prefix
            cur_movement_num = new_movement_num
            cur_count = 1
            cur_start = row_idx
            cur_titles = [title.lower()]
            cur_duration = duration_seconds
    _flush()
    return out


def _composer_from_org_path(path: list[str]) -> str | None:
    """Extract the composer name from an org-tree folder path.

    Convention: every classical piece sits under ``Classical/<Composer>/...``
    Returns the segment immediately after ``"Classical"``, or ``None`` if
    ``"Classical"`` doesn't appear in the path OR appears as the final
    segment (no composer below it). Case-sensitive ``"Classical"`` match —
    the org tree uses that exact casing consistently.

    A playlist filed under several org paths can have several composers;
    callers iterate all of those paths and union the results.

    Examples::

        ["Classical", "Beethoven", "Piano Sonatas", "Piano Sonata 28"] → "Beethoven"
        ["Classical", "Mahler", "Symphonies", "Symphony 6"]            → "Mahler"
        ["Classical", "Bach", "Cantatas", "Late Cantatas", "BWV 140"]  → "Bach"
        ["Rock", "Limp Bizkit"]                                        → None
        ["Classical"]                                                  → None
        []                                                             → None
    """
    if not path:
        return None
    try:
        i = path.index("Classical")
    except ValueError:
        return None
    if i + 1 >= len(path):
        return None
    composer = path[i + 1]
    # Defensively reject whitespace-only segments — shouldn't occur in
    # real org paths but cheap to guard against.
    return composer if composer and composer.strip() else None


def _composers_in_scope(org: dict, scope_path: list[str]) -> list[str]:
    """Sorted list of composers represented anywhere in *scope_path*'s
    subtree.

    Walks the scope node recursively. For every descendant folder path
    (including the scope itself) whose ``_composer_from_org_path`` is
    non-None, the composer name is added to the result. Multi-filed
    playlists naturally produce multiple composers because BOTH of their
    filing locations live under the scope when the scope is broad enough
    (e.g. ``["Classical"]``).

    Used by ``ClassicalShufflePlaylistDialog`` to populate its composer
    multi-select. Not called when the scope is itself single-composer
    (``_composer_from_org_path(scope_path)`` non-None) — that case hides
    the filter entirely.
    """
    composers: set[str] = set()

    def _walk(path: list[str]) -> None:
        node = get_node(org, path)
        if not node:
            return
        c = _composer_from_org_path(path)
        if c is not None:
            composers.add(c)
        for name in node.get("folders", {}) or {}:
            _walk(path + [name])

    _walk(list(scope_path))
    return sorted(composers, key=str.lower)


def _build_playlist_composer_map(org: dict) -> dict[str, set[str]]:
    """Walk the org tree once and return ``{playlist_url: {composer, ...}}``.

    For each playlist item encountered, ``_composer_from_org_path(path)`` is
    called against its filing folder. Non-None results are unioned into the
    playlist's composer set — so a playlist filed under both
    ``Classical/Beethoven/...`` and ``Classical/Cello/...`` ends up with
    ``{"Beethoven", "Cello"}``.

    Used by the Classical Shuffle generator (Step 6) to filter the sampled
    pool by the dialog's composer checkboxes without widening
    ``_build_classical_performance_pool``'s return shape. Playlists with no
    Classical filing get an empty set (still in the map so callers can
    distinguish "no Classical composer" from "URL never seen").
    """
    out: dict[str, set[str]] = {}

    def _walk(path: list[str]) -> None:
        node = get_node(org, path)
        if not node:
            return
        composer = _composer_from_org_path(path)
        for item in node.get("items", []) or []:
            if item.get("kind") != "playlist":
                continue
            url = (item.get("url") or "").strip()
            if not url:
                continue
            bucket = out.setdefault(url, set())
            if composer is not None:
                bucket.add(composer)
        for name in node.get("folders", {}) or {}:
            _walk(path + [name])

    _walk([])
    return out


def _piece_type_from_org_path(path: list[str]) -> str | None:
    """Extract the piece-type folder name from an org-tree folder path.

    Convention: every classical piece sits under
    ``Classical/<Composer>/<PieceType>/...`` — so the piece type is the
    segment two below ``"Classical"``. Returns ``None`` when the path
    doesn't reach that depth, or when ``"Classical"`` is absent. Mirrors
    ``_composer_from_org_path`` one level deeper.

    A playlist filed under several piece-type folders (rare but possible
    for cross-genre pieces) has several piece types; callers iterate all
    of those paths and union the results — same pattern the composer
    map uses.

    Examples::

        ["Classical", "Beethoven", "Piano Sonatas", "Piano Sonata 28"] → "Piano Sonatas"
        ["Classical", "Mahler", "Symphonies", "Symphony 6"]            → "Symphonies"
        ["Classical", "Bach", "Cantatas", "Late Cantatas", "BWV 140"]  → "Cantatas"
        ["Classical", "Beethoven"]                                     → None
        ["Rock", "Limp Bizkit", "Albums"]                              → None
        []                                                             → None
    """
    if not path:
        return None
    try:
        i = path.index("Classical")
    except ValueError:
        return None
    if i + 2 >= len(path):
        return None
    piece_type = path[i + 2]
    return piece_type if piece_type and piece_type.strip() else None


def _piece_types_in_scope(org: dict, scope_path: list[str]) -> list[str]:
    """Sorted list of piece-type folders represented anywhere in
    *scope_path*'s subtree.

    Same walk shape as ``_composers_in_scope`` — every descendant folder
    path whose ``_piece_type_from_org_path`` is non-None contributes its
    piece-type name. Used by ``ClassicalShufflePlaylistDialog`` to
    populate the piece-type multi-select. Not called when the scope is
    itself single-piece-type (``_piece_type_from_org_path(scope_path)``
    non-None) — that case hides the filter.
    """
    piece_types: set[str] = set()

    def _walk(path: list[str]) -> None:
        node = get_node(org, path)
        if not node:
            return
        pt = _piece_type_from_org_path(path)
        if pt is not None:
            piece_types.add(pt)
        for name in node.get("folders", {}) or {}:
            _walk(path + [name])

    _walk(list(scope_path))
    return sorted(piece_types, key=str.lower)


def _build_playlist_piece_type_map(org: dict) -> dict[str, set[str]]:
    """Walk the org tree once and return ``{playlist_url: {piece_type, ...}}``.

    Mirror of ``_build_playlist_composer_map`` one level deeper in the
    Classical hierarchy. A playlist filed under both
    ``Classical/Beethoven/Symphonies/...`` and
    ``Classical/Beethoven/Piano Sonatas/...`` ends up with
    ``{"Symphonies", "Piano Sonatas"}``.

    Used by the Classical Shuffle generator (Step 6) to filter the
    sampled pool by the dialog's piece-type checkboxes, sitting beside
    the existing composer filter. Playlists with no Classical filing
    get an empty set (still in the map so callers can distinguish
    "no Classical piece-type" from "URL never seen").
    """
    out: dict[str, set[str]] = {}

    def _walk(path: list[str]) -> None:
        node = get_node(org, path)
        if not node:
            return
        piece_type = _piece_type_from_org_path(path)
        for item in node.get("items", []) or []:
            if item.get("kind") != "playlist":
                continue
            url = (item.get("url") or "").strip()
            if not url:
                continue
            bucket = out.setdefault(url, set())
            if piece_type is not None:
                bucket.add(piece_type)
        for name in node.get("folders", {}) or {}:
            _walk(path + [name])

    _walk([])
    return out


def _sample_for_duration_target(
    pool: list[Performance],
    target_seconds: int,
    rng=random,
) -> list[Performance]:
    """Sample performances without replacement until cumulative duration
    meets or exceeds *target_seconds*. One overshoot allowed — no per-pick
    cap.

    Used by the Classical Shuffle playlist generator (Step 5). The
    overshoot policy was decided 2026-06-04: a Mahler 2 (~80 min) closing
    out a 3 h target produces a 4 h playlist, and that is acceptable. The
    sampler stays a single pure function with no cap parameter; if the UX
    ever needs tighter bounds, add it at the caller (e.g. by filtering the
    pool before sampling) rather than in here.

    Returns the picked performances in the shuffled order — the caller is
    responsible for any further ordering (e.g. composer interleave). On
    target ≤ 0 or empty pool, returns ``[]``. If the pool's total
    duration is below target, returns the whole shuffled pool (we never
    raise — "too short" is a quiet outcome the caller can surface).

    ``rng`` defaults to the module-level ``random`` (process global), but
    accepts any object with a ``.shuffle(list)`` method — pass a seeded
    ``random.Random(...)`` instance for deterministic tests.
    """
    if target_seconds <= 0 or not pool:
        return []
    shuffled = list(pool)
    rng.shuffle(shuffled)
    picked: list[Performance] = []
    total = 0
    for perf in shuffled:
        picked.append(perf)
        total += perf.duration_seconds
        if total >= target_seconds:
            break
    return picked


def _spread_adjacent_by_key(items: list, key_fn) -> list:
    """Reorder *items* so no two adjacent items share ``key_fn(item)``.

    Classic max-count interleave (aka "reorganize string"): bucket the
    input by key, then greedily emit from the largest non-blocked bucket.
    A bucket is "blocked" when its key equals the most recently emitted
    key. When the only available bucket is blocked, emit anyway —
    happens only when one key strictly dominates (count > (n+1)//2), in
    which case some clustering is mathematically unavoidable.

    Used by the Classical Shuffle playlist generator to break up adjacent
    recordings of the same work after uniform-random sampling — without
    it, three Beethoven 8 recordings in the pool plus 50 other works can
    still land two of them back-to-back by statistical bad luck.

    Pure, deterministic, no rng. Bucket insertion order follows input
    order (Python dict guarantees insertion ordering), and stable sort is
    used to resolve ties on remaining-count, so the same input always
    produces the same output. Items within a bucket are emitted in input
    order. ``key_fn`` is called once per item.

    Returns a new list. The input is not mutated.
    """
    if len(items) < 2:
        return list(items)
    groups: dict = {}
    for it in items:
        groups.setdefault(key_fn(it), []).append(it)
    if len(groups) == len(items):
        return list(items)
    out: list = []
    last_key = None
    while any(groups.values()):
        active = sorted(
            ((k, lst) for k, lst in groups.items() if lst),
            key=lambda kl: -len(kl[1]),
        )
        chosen = next((kl for kl in active if kl[0] != last_key), active[0])
        out.append(chosen[1].pop(0))
        last_key = chosen[0]
    return out


def _perf_has_restricted_track(
    perf: Performance,
    restricted_by_url: dict[str, list[dict]],
) -> bool:
    """True if ANY of *perf*'s tracks matches a recorded web-restricted entry.

    Pure function lifted out of ``_build_classical_performance_pool`` for
    direct unit testing. The pool builder passes a pre-bucketed
    ``{playlist_url: [entries]}`` map so the per-perf check is O(restricted
    in this perf's source playlist), not O(total restricted).

    Originally only the first track was checked (the live-mode click-time
    substitution path always derails at the playlist row click, so first-
    track matching was sufficient). The 2026-06-12 menu-hidden symptom —
    Amazon hiding the "Add to playlist" item for specific mid-perf track
    ASINs in the web player — flags individual non-first tracks. A single
    restricted track breaks the whole perf for the generator (a Beethoven
    8 minus mvt III is not a useful result), so skipping the entire perf
    if any of its tracks is restricted is the right tradeoff.

    Match is title-equality (case-insensitive) plus permissive artist match
    (matches when either side's artist is empty — restricted entries from
    older code paths may omit it; today's recorders always include it).
    """
    entries = restricted_by_url.get(perf.playlist_url) or []
    if not entries:
        return False
    titles = {t.strip().lower() for t in (perf.titles or ()) if t}
    if not titles:
        return False
    a = (perf.artist or "").strip().lower()
    for e in entries:
        et = (e.get("title") or "").strip().lower()
        if not et or et not in titles:
            continue
        ea = (e.get("artist") or "").strip().lower()
        if not a or not ea or a == ea:
            return True
    return False


def _build_classical_performance_pool(
    org: dict,
    folder_path: list[str],
    output_dir: str = OUTPUT_DIR,
) -> tuple[list[Performance], list[str], int]:
    """Walk the *folder_path* subtree, expand each playlist's cached xlsx
    into Performance runs, and return
    ``(performances, skipped_playlists, restricted_skip_count)``.

    * ``skipped_playlists`` is a list of human-readable reasons for
      *playlist-level* skips — playlists that didn't contribute any
      performances because they have no URL, no cached xlsx, an empty
      xlsx, or a read error. Each entry corresponds to one playlist.
    * ``restricted_skip_count`` is the number of individual performances
      filtered out because ANY of their tracks matches a recorded
      web-restricted entry. These DON'T represent missing playlists —
      the playlists are fine, just some perfs within them get a
      guaranteed misroute on the web player or a hidden "Add to playlist"
      menu item (see feedback_amazon_music_dom.md Trap 4 and §9). Counted
      separately so the caller can report the two categories without
      conflating them.
    """
    import web_restricted
    url_index = _build_url_to_xlsx_index(output_dir)
    performances: list[Performance] = []
    skipped: list[str] = []
    # Pre-load restricted entries grouped by playlist_url so the per-perf
    # filter is O(1) lookup.
    all_restricted = web_restricted.load()
    restricted_by_url: dict[str, list[dict]] = {}
    for r in all_restricted:
        url = (r.get("playlist_url") or "").strip()
        if url:
            restricted_by_url.setdefault(url, []).append(r)

    restricted_skip_count = 0
    for pl in iter_playlists_recursive(org, folder_path):
        url = (pl.get("url") or "").strip()
        name = pl.get("name") or url or "<unnamed>"
        if not url:
            skipped.append(f"{name}: no URL")
            continue
        xlsx_path = url_index.get(url)
        if not xlsx_path:
            msg = f"{name}: no cached xlsx (export the playlist first)"
            skipped.append(msg)
            logger.info("classical-shuffle: skipping %s", msg)
            continue
        try:
            song_rows = _read_song_rows(xlsx_path)
        except Exception:
            logger.exception("classical-shuffle: could not read %s", xlsx_path)
            skipped.append(f"{name}: error reading {os.path.basename(xlsx_path)}")
            continue
        if not song_rows:
            skipped.append(f"{name}: empty xlsx")
            continue
        for perf in _group_into_performances(song_rows, url, name):
            if _perf_has_restricted_track(perf, restricted_by_url):
                restricted_skip_count += 1
                logger.info(
                    "classical-shuffle: skipping web-restricted perf "
                    "'%s' by %s (playlist=%s start_row=%d)",
                    perf.album, perf.artist, perf.playlist_name,
                    perf.start_row,
                )
                continue
            performances.append(perf)
    return performances, skipped, restricted_skip_count


def _append_songs_to_xlsx(path: str, songs: list[dict]) -> None:
    """Append *songs* (dicts with title/artist/album) to the data sheet.

    Finds the title, artist, album columns by header and writes into them.
    Renumbers the '#' column so it stays contiguous.
    """
    if not songs:
        return
    wb = load_workbook(path)
    try:
        sheet_name = next(
            (s for s in wb.sheetnames if s != TAG_SHEET),
            wb.sheetnames[0] if wb.sheetnames else None,
        )
        if sheet_name is None:
            raise RuntimeError("Workbook has no data sheet")
        ws = wb[sheet_name]
        headers = [str(cell.value).strip().lower() if cell.value else "" for cell in ws[1]]

        def _col(aliases):
            for alias in aliases:
                if alias in headers:
                    return headers.index(alias) + 1  # openpyxl is 1-based
            return None

        title_col = _col(_TITLE_HEADER_ALIASES)
        artist_col = _col(_ARTIST_HEADER_ALIASES)
        album_col = _col(["album"])
        num_cols = len(headers)

        for song in songs:
            new_row = [None] * num_cols
            if title_col:
                new_row[title_col - 1] = song.get("title", "")
            if artist_col:
                new_row[artist_col - 1] = song.get("artist", "")
            if album_col:
                new_row[album_col - 1] = song.get("album", "")
            ws.append(new_row)

        # Renumber '#' column.
        for i, row in enumerate(ws.iter_rows(min_row=2, max_col=1), start=1):
            row[0].value = i
        wb.save(path)
    finally:
        wb.close()


def _compute_reconcile_diff(
    playlist_songs: list[dict],
    xlsx_songs: list[dict],
) -> dict:
    """Diff playlist_songs vs xlsx_songs.

    Returns:
        only_in_playlist — songs to add to the xlsx (present in Amazon, absent in xlsx)
        only_in_xlsx     — songs to remove from the xlsx (present in xlsx, absent in Amazon)
        only_in_xlsx also carries the 'row' key needed to call _delete_rows_from_xlsx.
    """
    pl_seen: dict[tuple, dict] = {}
    for s in playlist_songs:
        key = (s["title"].strip().lower(), s["artist"].strip().lower())
        if key not in pl_seen:
            pl_seen[key] = s

    xlsx_seen: dict[tuple, dict] = {}
    for s in xlsx_songs:
        key = (s["title"].strip().lower(), s["artist"].strip().lower())
        if key not in xlsx_seen:
            xlsx_seen[key] = s

    only_in_playlist = [s for k, s in pl_seen.items() if k not in xlsx_seen]
    only_in_xlsx = [s for k, s in xlsx_seen.items() if k not in pl_seen]

    return {"only_in_playlist": only_in_playlist, "only_in_xlsx": only_in_xlsx}


def _reorder_xlsx_rows(path: str, playlist_songs: list[dict]) -> int:
    """Rewrite the spreadsheet rows so their order matches *playlist_songs*.

    Songs that appear in the playlist are moved to the front in playlist order.
    Songs in the spreadsheet but absent from the playlist are appended at the
    end, preserving their relative order.  The '#' column is renumbered.

    Returns the number of rows that actually changed position.
    """
    wb = load_workbook(path)
    try:
        sheet_name = next(
            (s for s in wb.sheetnames if s != TAG_SHEET),
            wb.sheetnames[0] if wb.sheetnames else None,
        )
        if sheet_name is None:
            raise RuntimeError("Workbook has no data sheet")
        ws = wb[sheet_name]
        headers = [str(cell.value).strip().lower() if cell.value else "" for cell in ws[1]]

        def _col(aliases):
            for alias in aliases:
                if alias in headers:
                    return headers.index(alias)  # 0-based
            return None

        title_col = _col(_TITLE_HEADER_ALIASES)
        artist_col = _col(_ARTIST_HEADER_ALIASES)
        if title_col is None:
            raise RuntimeError("No title column found in spreadsheet")

        # Read all data rows as lists of cell values
        data_rows = [list(row) for row in ws.iter_rows(min_row=2, values_only=True)
                     if any(c is not None for c in row)]

        def _norm(title: str, artist: str) -> tuple[str, str]:
            return (title.strip().lower(), artist.strip().lower())

        # Index data rows by (title, artist) — keep a list per key for duplicates
        from collections import defaultdict
        key_to_rows: dict[tuple, list] = defaultdict(list)
        for dr in data_rows:
            t = str(dr[title_col]).strip() if dr[title_col] is not None else ""
            a = str(dr[artist_col]).strip() if artist_col is not None and dr[artist_col] is not None else ""
            key_to_rows[_norm(t, a)].append(dr)

        # Build the new order: playlist songs first (in playlist order),
        # then any leftover xlsx-only songs in their original order.
        used: dict[tuple, int] = {}  # key -> how many we've consumed
        new_data: list[list] = []

        for ps in playlist_songs:
            key = _norm(ps.get("title", ""), ps.get("artist", ""))
            candidates = key_to_rows.get(key, [])
            idx = used.get(key, 0)
            if idx < len(candidates):
                new_data.append(candidates[idx])
                used[key] = idx + 1

        # Append xlsx-only songs (those with remaining unconsumed rows)
        for dr in data_rows:
            t = str(dr[title_col]).strip() if dr[title_col] is not None else ""
            a = str(dr[artist_col]).strip() if artist_col is not None and dr[artist_col] is not None else ""
            key = _norm(t, a)
            consumed = used.get(key, 0)
            total = len(key_to_rows[key])
            # This row is "leftover" if we haven't consumed all copies via the playlist
            # We determine leftover by checking if this row was among the consumed ones.
            # Simplest: rebuild from scratch comparing new_data vs data_rows.
            # We track which original rows were used by position.
        # Redo leftover detection properly by tracking original row indices
        used_indices: set[int] = set()
        new_data = []
        consumed_per_key: dict[tuple, int] = {}
        for ps in playlist_songs:
            key = _norm(ps.get("title", ""), ps.get("artist", ""))
            candidates = key_to_rows.get(key, [])
            idx = consumed_per_key.get(key, 0)
            if idx < len(candidates):
                # Find the original index of this row
                orig_idx = data_rows.index(candidates[idx])
                new_data.append(candidates[idx])
                used_indices.add(orig_idx)
                consumed_per_key[key] = idx + 1

        for orig_idx, dr in enumerate(data_rows):
            if orig_idx not in used_indices:
                new_data.append(dr)

        # Count how many rows moved
        moved = sum(1 for i, (old, new) in enumerate(zip(data_rows, new_data)) if old != new)

        # Rewrite the sheet: delete all data rows then write new_data
        # Delete from bottom up
        for r in range(ws.max_row, 1, -1):
            ws.delete_rows(r)

        for i, dr in enumerate(new_data, start=1):
            row_values = list(dr)
            row_values[0] = i  # renumber '#' column
            ws.append(row_values)

        wb.save(path)
        return moved
    finally:
        wb.close()


def _scan_output_dir() -> list[dict]:
    """Return one dict per .xlsx anywhere under ./output/ (recursive)."""
    if not os.path.isdir(OUTPUT_DIR):
        return []
    results = []
    pattern = os.path.join(OUTPUT_DIR, "**", "*.xlsx")
    for path in sorted(glob.glob(pattern, recursive=True)):
        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(path))
            modified = mtime.strftime("%Y-%m-%d %H:%M")
        except OSError:
            modified = ""
        try:
            tag = get_tag_display(path)
        except Exception:
            tag = ""
        # Display-name shows subfolder context for files outside OUTPUT_DIR root
        # so two basenames in different folders don't look identical.
        try:
            rel = os.path.relpath(path, OUTPUT_DIR)
        except ValueError:
            rel = os.path.basename(path)
        display_name = rel.replace(os.sep, " / ") if os.sep in rel else rel
        results.append({
            "name": os.path.basename(path),
            "display_name": display_name,
            "path": path,
            "tag": tag,
            "modified": modified,
        })
    return results


# ---------------------------------------------------------------------------
# Organization tree: sidebar view of ./output/organization.json.
# ---------------------------------------------------------------------------
class _OrgTree(QTreeWidget):
    """QTreeWidget subclass with drag/drop semantics for organization.json.

    - Drag source: emits MIME_ORG with {"kind": "folder", "path": [...]}
      or {"kind": "item", "from": [...], "index": N}.
    - Drop target: folder rows (or empty space = root). Accepts MIME_ORG
      and MIME_SPREADSHEET. Qt's auto-move is bypassed — the owner widget
      mutates organization.json and re-populates via refresh().
    """

    def __init__(self, owner: "OrganizationTreeWidget") -> None:
        super().__init__()
        self._owner = owner
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        # Drop-target highlighting during drag-over.
        self._hover_item: QTreeWidgetItem | None = None
        self._hover_orig_bg: QBrush | None = None
        # A soft tint of the palette's highlight colour — reads well in either theme.
        self._hover_brush = QBrush(self.palette().highlight().color().lighter(140))

        # Ctrl+N → New Folder, scoped to the tree (and its children) so it
        # only fires when the user is interacting with the org panel and
        # doesn't shadow other shortcuts elsewhere. Target resolution
        # mirrors the right-click "New Folder…" action — see
        # `_new_folder_target`. Bucket rows resolve to None there; here
        # we fall back to root for friendliness so the shortcut is never
        # a silent no-op.
        new_folder_shortcut = QShortcut(QKeySequence("Ctrl+N"), self)
        new_folder_shortcut.setContext(
            Qt.ShortcutContext.WidgetWithChildrenShortcut
        )
        new_folder_shortcut.activated.connect(self._on_new_folder_shortcut)

    def _on_new_folder_shortcut(self) -> None:
        item = self.currentItem()
        target = self._new_folder_target(item)
        if target is None:
            target = []  # bucket row or other invalid context → root
        self._owner._create_folder_under(target)

    def mimeTypes(self):
        return [MIME_ORG]

    def mimeData(self, items):
        if not items:
            return None
        # Multi-drag isn't wired up — fall through to single-item drag only
        # when exactly one row is selected. With ExtendedSelection, Qt passes
        # the full selection here; silently dropping the tail would be worse
        # than refusing the drag.
        if len(items) > 1:
            return None
        item = items[0]
        kind = item.data(0, OrganizationTreeWidget.ROLE_KIND)
        payload: dict | None = None
        if kind == "folder":
            folder_path = item.data(0, OrganizationTreeWidget.ROLE_FOLDER_PATH) or []
            if not folder_path:
                return None  # can't drag the (virtual) root
            payload = {"kind": "folder", "path": folder_path}
        elif kind in ("spreadsheet", "playlist"):
            parent_item = item.parent()
            if parent_item is None or parent_item.data(
                0, OrganizationTreeWidget.ROLE_KIND
            ) != "folder":
                src_path: list[str] = []
            else:
                src_path = parent_item.data(
                    0, OrganizationTreeWidget.ROLE_FOLDER_PATH
                ) or []
            index = item.data(0, OrganizationTreeWidget.ROLE_ITEM_INDEX)
            if index is None:
                return None
            payload = {"kind": "item", "from": src_path, "index": int(index)}
        else:
            return None
        md = QMimeData()
        md.setData(MIME_ORG, json.dumps(payload).encode("utf-8"))
        return md

    def _resolve_drop_target(self, pos) -> list[str] | None:
        """Return dest folder path (possibly []) or None if the target is invalid."""
        target = self.itemAt(pos)
        if target is None:
            return []  # empty space → root
        kind = target.data(0, OrganizationTreeWidget.ROLE_KIND)
        if kind == "folder":
            return target.data(0, OrganizationTreeWidget.ROLE_FOLDER_PATH) or []
        return None  # buckets and leaves reject drops

    def dragEnterEvent(self, event):
        md = event.mimeData()
        if (
            md.hasFormat(MIME_ORG)
            or md.hasFormat(MIME_SPREADSHEET)
            or md.hasFormat(MIME_PLAYLIST)
        ):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        md = event.mimeData()
        if not (
            md.hasFormat(MIME_ORG)
            or md.hasFormat(MIME_SPREADSHEET)
            or md.hasFormat(MIME_PLAYLIST)
        ):
            event.ignore()
            self._clear_hover()
            return
        pos = event.position().toPoint()
        dest = self._resolve_drop_target(pos)
        if dest is None:
            event.ignore()
            self._clear_hover()
            return
        # Highlight the folder row under the cursor (if any). When dest
        # resolves to root via empty space, no single item hosts the drop,
        # so just clear any previous highlight.
        target = self.itemAt(pos)
        if target is not None and target.data(
            0, OrganizationTreeWidget.ROLE_KIND
        ) == "folder":
            self._set_hover(target)
        else:
            self._clear_hover()
        event.acceptProposedAction()

    def dragLeaveEvent(self, event):
        self._clear_hover()
        super().dragLeaveEvent(event)

    def _set_hover(self, item: QTreeWidgetItem) -> None:
        if item is self._hover_item:
            return
        self._clear_hover()
        self._hover_orig_bg = item.background(0)
        item.setBackground(0, self._hover_brush)
        self._hover_item = item

    def _clear_hover(self) -> None:
        if self._hover_item is not None:
            self._hover_item.setBackground(
                0, self._hover_orig_bg if self._hover_orig_bg is not None else QBrush()
            )
            self._hover_item = None
            self._hover_orig_bg = None

    def dropEvent(self, event):
        self._clear_hover()
        md = event.mimeData()
        dest = self._resolve_drop_target(event.position().toPoint())
        if dest is None:
            event.ignore()
            return
        copy_mode = bool(
            event.modifiers() & Qt.KeyboardModifier.ShiftModifier
        )
        if md.hasFormat(MIME_ORG):
            try:
                payload = json.loads(bytes(md.data(MIME_ORG)).decode("utf-8"))
            except Exception:
                event.ignore()
                return
            self._owner._handle_org_drop(payload, dest, copy_mode)
            event.acceptProposedAction()
        elif md.hasFormat(MIME_SPREADSHEET):
            path = bytes(md.data(MIME_SPREADSHEET)).decode("utf-8")
            if path:
                self._owner._handle_spreadsheet_drop(path, dest)
                event.acceptProposedAction()
            else:
                event.ignore()
        elif md.hasFormat(MIME_PLAYLIST):
            try:
                payload = json.loads(
                    bytes(md.data(MIME_PLAYLIST)).decode("utf-8")
                )
            except Exception:
                event.ignore()
                return
            self._owner._handle_playlist_drop(payload, dest)
            event.acceptProposedAction()
        else:
            event.ignore()

    def contextMenuEvent(self, event):
        item = self.itemAt(event.pos())
        kind = item.data(0, OrganizationTreeWidget.ROLE_KIND) if item else None

        # Multi-select operative set: only kicks in when the right-clicked row
        # is part of a ≥2-row selection AND every selected row is a leaf
        # (spreadsheet/playlist). Otherwise the menu operates on the clicked
        # row alone — matches Windows Explorer behavior.
        selected = list(self.selectedItems())
        multi_items: list[QTreeWidgetItem] = []
        if (
            item is not None
            and item in selected
            and len(selected) >= 2
            and all(
                s.data(0, OrganizationTreeWidget.ROLE_KIND)
                in ("spreadsheet", "playlist")
                for s in selected
            )
        ):
            multi_items = selected
        is_multi = bool(multi_items)

        # A valid cut/copy source is a folder (non-root), spreadsheet, or playlist.
        can_cut_copy = False
        label = ""
        if is_multi:
            can_cut_copy = True
            label = f"{len(multi_items)} items"
        elif kind == "folder":
            folder_path = item.data(0, OrganizationTreeWidget.ROLE_FOLDER_PATH) or []
            if folder_path:
                can_cut_copy = True
                label = folder_path[-1]
        elif kind in ("spreadsheet", "playlist"):
            can_cut_copy = True
            label = item.text(0)

        paste_target = self._paste_target_path(item)
        has_clip = self._owner._clipboard is not None
        new_folder_target = self._new_folder_target(item)
        delete_folder_path = self._delete_folder_path(item)

        menu = QMenu(self)
        cut_text = f"Cut '{label}'" if label else "Cut"
        copy_text = f"Copy '{label}'" if label else "Copy"
        act_cut = menu.addAction(cut_text)
        act_cut.setEnabled(can_cut_copy)
        act_copy = menu.addAction(copy_text)
        act_copy.setEnabled(can_cut_copy)
        act_paste = menu.addAction("Paste")
        act_paste.setEnabled(has_clip and paste_target is not None)
        menu.addSeparator()
        # "\t" in QAction text right-justifies the trailing portion as a
        # keyboard-shortcut hint — purely cosmetic; the actual Ctrl+N
        # binding is a QShortcut on _OrgTree (see __init__) so it
        # persists across menu-open/close cycles.
        act_new_folder = menu.addAction("New Folder…\tCtrl+N")
        act_new_folder.setEnabled(new_folder_target is not None)
        act_rename_folder = None
        act_del_folder = None
        if delete_folder_path is not None:
            act_rename_folder = menu.addAction(
                f"Rename Folder '{delete_folder_path[-1]}'…"
            )
            act_del_folder = menu.addAction(
                f"Delete Folder '{delete_folder_path[-1]}'"
            )
        # "Play all in folder" / "Play all (recursive)" — only for folder
        # rows that contain at least one playlist (directly or in a
        # descendant). Mirrors the CLI's `p` / `pr` commands.
        act_play_folder = None
        act_play_folder_recursive = None
        act_classical_shuffle = None
        act_classical_shuffle_generate = None
        if kind == "folder":
            folder_path = (
                item.data(0, OrganizationTreeWidget.ROLE_FOLDER_PATH) or []
            )
            try:
                org = load_org()
            except Exception:
                org = {"root": {"folders": {}, "items": []}}
            node = get_node(org, folder_path) or {}
            direct_playlists = sum(
                1 for it in node.get("items", [])
                if it.get("kind") == "playlist"
            )
            recursive_playlists = sum(
                1 for _ in iter_playlists_recursive(org, folder_path)
            )
            if direct_playlists or recursive_playlists:
                menu.addSeparator()
                if direct_playlists:
                    act_play_folder = menu.addAction(
                        f"Play all in folder ({direct_playlists} "
                        f"playlist{'s' if direct_playlists != 1 else ''})"
                    )
                if recursive_playlists > direct_playlists:
                    act_play_folder_recursive = menu.addAction(
                        f"Play all (recursive, {recursive_playlists} "
                        f"playlist{'s' if recursive_playlists != 1 else ''})"
                    )
                # "Classical Shuffle (recursive)" — only when this folder is
                # the "Classical" folder or sits beneath one.
                if recursive_playlists and any(
                    seg == "Classical" for seg in folder_path
                ):
                    act_classical_shuffle = menu.addAction(
                        "Classical Shuffle (recursive)"
                    )
                    # Sibling entry: generate a one-shot playlist for iOS
                    # listening from the same pool. Same scope semantics.
                    act_classical_shuffle_generate = menu.addAction(
                        "Generate Classical Shuffle playlist…"
                    )

        act_remove_item = None
        act_delete_file = None
        act_rename_file = None
        act_export_playlist = None
        if is_multi:
            menu.addSeparator()
            act_remove_item = menu.addAction(
                f"Remove {len(multi_items)} items from folder"
            )
            spreadsheet_paths = [
                from_stored_path(
                    (it.data(0, OrganizationTreeWidget.ROLE_PAYLOAD) or {})
                    .get("path") or ""
                )
                for it in multi_items
                if it.data(0, OrganizationTreeWidget.ROLE_KIND) == "spreadsheet"
            ]
            spreadsheet_paths = [p for p in spreadsheet_paths if p]
            if spreadsheet_paths and len(spreadsheet_paths) == len(multi_items):
                act_delete_file = menu.addAction(
                    f"Delete {len(spreadsheet_paths)} files…"
                )
        elif kind in ("spreadsheet", "playlist"):
            menu.addSeparator()
            act_remove_item = menu.addAction("Remove from folder")
            if kind == "spreadsheet":
                entry = item.data(0, OrganizationTreeWidget.ROLE_PAYLOAD) or {}
                entry_path = from_stored_path(entry.get("path") or "")
                if entry_path:
                    act_rename_file = menu.addAction(
                        f"Rename '{os.path.basename(entry_path)}'…"
                    )
                    act_delete_file = menu.addAction(
                        f"Delete file '{os.path.basename(entry_path)}'…"
                    )
            elif kind == "playlist":
                act_export_playlist = menu.addAction("Export to spreadsheet…")
                # Default-disable, then enable below if we have a URL.
                entry = item.data(0, OrganizationTreeWidget.ROLE_PAYLOAD) or {}
                if not (entry.get("url") or "").strip():
                    act_export_playlist.setEnabled(False)

        chosen = menu.exec(event.globalPos())
        if chosen is None:
            return
        if chosen is act_cut:
            if is_multi:
                self._owner._cut_or_copy_items_from_tree(multi_items, "cut")
            else:
                self._owner._cut_or_copy_from_tree(item, "cut")
        elif chosen is act_copy:
            if is_multi:
                self._owner._cut_or_copy_items_from_tree(multi_items, "copy")
            else:
                self._owner._cut_or_copy_from_tree(item, "copy")
        elif chosen is act_paste and paste_target is not None:
            self._owner._paste_into(paste_target)
        elif chosen is act_new_folder and new_folder_target is not None:
            self._owner._create_folder_under(new_folder_target)
        elif act_rename_folder is not None and chosen is act_rename_folder:
            self._owner._rename_folder_action(delete_folder_path)
        elif act_del_folder is not None and chosen is act_del_folder:
            self._owner._delete_folder_action(delete_folder_path)
        elif act_remove_item is not None and chosen is act_remove_item:
            if is_multi:
                refs = self._collect_leaf_refs(multi_items)
                self._owner._remove_items_from_folder(refs)
            else:
                parent_item = item.parent()
                if (
                    parent_item is not None
                    and parent_item.data(0, OrganizationTreeWidget.ROLE_KIND) == "folder"
                ):
                    parent_path = parent_item.data(
                        0, OrganizationTreeWidget.ROLE_FOLDER_PATH
                    ) or []
                else:
                    parent_path = []
                idx = item.data(0, OrganizationTreeWidget.ROLE_ITEM_INDEX)
                if idx is not None:
                    self._owner._remove_item_from_folder(parent_path, int(idx))
        elif act_rename_file is not None and chosen is act_rename_file:
            entry = item.data(0, OrganizationTreeWidget.ROLE_PAYLOAD) or {}
            entry_path = from_stored_path(entry.get("path") or "")
            if entry_path:
                self._owner._rename_spreadsheet_file(entry_path)
        elif act_delete_file is not None and chosen is act_delete_file:
            if is_multi:
                paths = [
                    from_stored_path(
                        (it.data(0, OrganizationTreeWidget.ROLE_PAYLOAD) or {})
                        .get("path") or ""
                    )
                    for it in multi_items
                ]
                paths = [p for p in paths if p]
                if paths:
                    self._owner._delete_spreadsheet_files(paths)
                return
            entry = item.data(0, OrganizationTreeWidget.ROLE_PAYLOAD) or {}
            entry_path = from_stored_path(entry.get("path") or "")
            if entry_path:
                self._owner._delete_spreadsheet_file(entry_path)
        elif act_play_folder is not None and chosen is act_play_folder:
            folder_path = (
                item.data(0, OrganizationTreeWidget.ROLE_FOLDER_PATH) or []
            )
            self._owner.play_folder_requested.emit(list(folder_path), False)
        elif (
            act_play_folder_recursive is not None
            and chosen is act_play_folder_recursive
        ):
            folder_path = (
                item.data(0, OrganizationTreeWidget.ROLE_FOLDER_PATH) or []
            )
            self._owner.play_folder_requested.emit(list(folder_path), True)
        elif (
            act_classical_shuffle is not None
            and chosen is act_classical_shuffle
        ):
            folder_path = (
                item.data(0, OrganizationTreeWidget.ROLE_FOLDER_PATH) or []
            )
            self._owner.classical_shuffle_requested.emit(list(folder_path))
        elif (
            act_classical_shuffle_generate is not None
            and chosen is act_classical_shuffle_generate
        ):
            folder_path = (
                item.data(0, OrganizationTreeWidget.ROLE_FOLDER_PATH) or []
            )
            self._owner.classical_shuffle_generate_requested.emit(
                list(folder_path)
            )
        elif (
            act_export_playlist is not None
            and chosen is act_export_playlist
        ):
            entry = item.data(0, OrganizationTreeWidget.ROLE_PAYLOAD) or {}
            url = (entry.get("url") or "").strip()
            name = (entry.get("name") or "").strip()
            parent_item = item.parent()
            if (
                parent_item is not None
                and parent_item.data(0, OrganizationTreeWidget.ROLE_KIND) == "folder"
            ):
                parent_path = parent_item.data(
                    0, OrganizationTreeWidget.ROLE_FOLDER_PATH
                ) or []
            else:
                parent_path = []
            if url:
                self._owner.playlist_export_to_folder_requested.emit(
                    url, name, list(parent_path)
                )

    def _collect_leaf_refs(
        self, items: list[QTreeWidgetItem]
    ) -> list[dict]:
        """For a list of leaf rows, return [{from: [...], index: N}, ...].

        Skips any row whose parent isn't a folder or which has no item index.
        """
        refs: list[dict] = []
        for it in items:
            parent_item = it.parent()
            if (
                parent_item is None
                or parent_item.data(0, OrganizationTreeWidget.ROLE_KIND) != "folder"
            ):
                parent_path: list[str] = []
            else:
                parent_path = parent_item.data(
                    0, OrganizationTreeWidget.ROLE_FOLDER_PATH
                ) or []
            idx = it.data(0, OrganizationTreeWidget.ROLE_ITEM_INDEX)
            if idx is None:
                continue
            refs.append({"from": list(parent_path), "index": int(idx)})
        return refs

    def _new_folder_target(self, item) -> list[str] | None:
        """Where a 'New Folder…' action should insert, given the clicked item."""
        if item is None:
            return []  # empty space → under root
        kind = item.data(0, OrganizationTreeWidget.ROLE_KIND)
        if kind in ("bucket_all", "bucket_unfiled", "bucket_all_playlists",
                    "bucket_search"):
            return None
        if kind == "folder":
            return item.data(0, OrganizationTreeWidget.ROLE_FOLDER_PATH) or []
        # leaf → under its parent folder (or root)
        parent_item = item.parent()
        if parent_item is None:
            return []
        if parent_item.data(0, OrganizationTreeWidget.ROLE_KIND) == "folder":
            return parent_item.data(0, OrganizationTreeWidget.ROLE_FOLDER_PATH) or []
        return []

    def _delete_folder_path(self, item) -> list[str] | None:
        """Path to delete, only for non-root folder rows; None otherwise."""
        if item is None:
            return None
        if item.data(0, OrganizationTreeWidget.ROLE_KIND) != "folder":
            return None
        path = item.data(0, OrganizationTreeWidget.ROLE_FOLDER_PATH) or []
        return path if path else None

    def _paste_target_path(self, item) -> list[str] | None:
        """Resolve paste destination from the clicked item.

        Folder → into that folder. Leaf → into its parent folder.
        Bucket rows have no sensible paste target (return None).
        Empty space (item None) → root.
        """
        if item is None:
            return []
        kind = item.data(0, OrganizationTreeWidget.ROLE_KIND)
        if kind == "folder":
            return item.data(0, OrganizationTreeWidget.ROLE_FOLDER_PATH) or []
        if kind in ("spreadsheet", "playlist"):
            parent_item = item.parent()
            if parent_item is None:
                return []
            pkind = parent_item.data(0, OrganizationTreeWidget.ROLE_KIND)
            if pkind == "folder":
                return parent_item.data(0, OrganizationTreeWidget.ROLE_FOLDER_PATH) or []
            return []
        return None


class OrganizationTreeWidget(QWidget):
    ROLE_KIND = Qt.ItemDataRole.UserRole
    ROLE_PAYLOAD = Qt.ItemDataRole.UserRole + 1
    ROLE_FOLDER_PATH = Qt.ItemDataRole.UserRole + 2  # list[str] for folder items
    ROLE_ITEM_INDEX = Qt.ItemDataRole.UserRole + 3   # int index into folder's items[] (for item rows)

    spreadsheet_activated = pyqtSignal(str)       # path
    playlist_activated = pyqtSignal(str, str)     # url, name
    # Filter for the main spreadsheet list.
    # paths: None = show everything; set[str] of normalized paths = show only these.
    # label: short display string for the filter ("Bands", "Unfiled", "") — empty = no chip.
    folder_selected = pyqtSignal(object, str)     # (paths_or_none, label)
    clipboard_changed = pyqtSignal()               # emitted whenever _clipboard changes
    show_playlists = pyqtSignal()                  # "All Playlists" bucket was selected
    show_search = pyqtSignal()                     # "Search" bucket was selected
    org_changed = pyqtSignal()                     # emitted on every refresh (org file may have changed)
    spreadsheet_renamed = pyqtSignal(str, str)     # (old_path, new_path) — emitted after a successful rename
    # (folder_path: list[str], recursive: bool) — user picked "Play all in folder".
    play_folder_requested = pyqtSignal(object, bool)
    # (folder_path: list[str]) — user picked "Classical Shuffle (recursive)".
    classical_shuffle_requested = pyqtSignal(object)
    # (folder_path: list[str]) — user picked "Generate Classical Shuffle
    # playlist…". MainWindow opens ClassicalShufflePlaylistDialog; the
    # Step 6 build coroutine will eventually consume the dialog's values.
    classical_shuffle_generate_requested = pyqtSignal(object)
    # (url, name, folder_path) — user picked "Export to spreadsheet…" on a
    # playlist row inside a folder.
    playlist_export_to_folder_requested = pyqtSignal(str, str, object)

    def __init__(self) -> None:
        super().__init__()
        # Cut/Copy clipboard. None = empty. Otherwise:
        #   {"source": "tree_folder", "path": [...],        "op": "cut"|"copy"}
        #   {"source": "tree_item",   "from": [...], "index": N, "op": ...}
        #   {"source": "list_file",   "path": "...",      "op": ...}
        self._clipboard: dict | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        header = QHBoxLayout()
        title = QLabel("Organization")
        title_font = title.font()
        title_font.setBold(True)
        title.setFont(title_font)
        self._count_label = QLabel("")
        self._refresh_btn = QPushButton("Refresh")
        self._refresh_btn.clicked.connect(self.refresh)
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(self._count_label)
        header.addSpacing(6)
        header.addWidget(self._refresh_btn)
        layout.addLayout(header)

        self._tree = _OrgTree(self)
        self._tree.setHeaderHidden(True)
        self._tree.itemDoubleClicked.connect(self._on_double_click)
        self._tree.currentItemChanged.connect(self._on_current_item_changed)
        layout.addWidget(self._tree)

        style = self.style()
        self._folder_icon = style.standardIcon(QStyle.StandardPixmap.SP_DirIcon)
        self._spreadsheet_icon = style.standardIcon(
            QStyle.StandardPixmap.SP_FileIcon
        )
        self._playlist_icon = style.standardIcon(
            QStyle.StandardPixmap.SP_MediaPlay
        )
        self._all_icon = style.standardIcon(
            QStyle.StandardPixmap.SP_DirHomeIcon
        )
        self._unfiled_icon = style.standardIcon(
            QStyle.StandardPixmap.SP_DirOpenIcon
        )
        self._all_playlists_icon = style.standardIcon(
            QStyle.StandardPixmap.SP_MediaPlay
        )
        self._search_icon = style.standardIcon(
            QStyle.StandardPixmap.SP_FileDialogContentsView
        )

        self.refresh()

    @pyqtSlot()
    def refresh(self) -> None:
        # Snapshot expansion + selection so a rebuild doesn't collapse the
        # tree or kick the content view back to "All Spreadsheets".
        expanded_before = self._collect_expanded_folder_paths()
        selected_before = self._snapshot_current_selection()

        self._tree.clear()
        try:
            org = load_org()
        except Exception as e:
            logger.exception("Could not load organization.json")
            placeholder = QTreeWidgetItem(
                self._tree, [f"(error loading organization: {e})"]
            )
            placeholder.setDisabled(True)
            self._count_label.setText("")
            return

        # Virtual buckets at the top.
        all_item = QTreeWidgetItem(self._tree, ["All Spreadsheets"])
        all_item.setIcon(0, self._all_icon)
        all_item.setData(0, self.ROLE_KIND, "bucket_all")

        unfiled_item = QTreeWidgetItem(self._tree, ["Unfiled"])
        unfiled_item.setIcon(0, self._unfiled_icon)
        unfiled_item.setData(0, self.ROLE_KIND, "bucket_unfiled")

        all_playlists_item = QTreeWidgetItem(self._tree, ["All Playlists"])
        all_playlists_item.setIcon(0, self._all_playlists_icon)
        all_playlists_item.setData(0, self.ROLE_KIND, "bucket_all_playlists")

        search_item = QTreeWidgetItem(self._tree, ["Search"])
        search_item.setIcon(0, self._search_icon)
        search_item.setData(0, self.ROLE_KIND, "bucket_search")

        root = org.get("root", {})
        folders, items = self._populate(
            self._tree.invisibleRootItem(), root, path=[]
        )
        if expanded_before:
            self._restore_expanded(expanded_before)
        else:
            # First-ever refresh: reveal the top-level folders.
            self._tree.expandToDepth(0)
        self._count_label.setText(f"{folders} folder{'s' if folders != 1 else ''}, "
                                  f"{items} item{'s' if items != 1 else ''}")
        # Restore the previous selection if it still exists. Falling through
        # to All Spreadsheets only when nothing was selected before — this
        # keeps the content view stable across a refresh triggered by
        # mutations (e.g. dropping a playlist into a folder).
        restored = (
            selected_before is not None
            and self._restore_current_selection(selected_before)
        )
        if not restored:
            self._tree.setCurrentItem(all_item)
        # Re-apply cut-styling if something is on the clipboard as "cut".
        self._apply_clipboard_style()
        self.org_changed.emit()

    def _collect_expanded_folder_paths(self) -> set[tuple[str, ...]]:
        """Snapshot which folder paths are currently expanded."""
        expanded: set[tuple[str, ...]] = set()

        def walk(parent: QTreeWidgetItem) -> None:
            for i in range(parent.childCount()):
                it = parent.child(i)
                if (
                    it.data(0, self.ROLE_KIND) == "folder"
                    and it.isExpanded()
                ):
                    path = it.data(0, self.ROLE_FOLDER_PATH) or []
                    expanded.add(tuple(path))
                walk(it)

        walk(self._tree.invisibleRootItem())
        return expanded

    def _snapshot_current_selection(self) -> tuple[str, tuple] | None:
        """Capture identity of the currently-selected row so a refresh can
        restore it. Returns ``(kind, key)`` where *key* is an empty tuple for
        bucket rows and the folder path for folder rows. Leaf items
        (spreadsheet/playlist) are not preserved — selection on a leaf
        rarely carries semantic intent and item indices shift on mutation."""
        item = self._tree.currentItem()
        if item is None:
            return None
        kind = item.data(0, self.ROLE_KIND)
        if kind in (
            "bucket_all", "bucket_unfiled",
            "bucket_all_playlists", "bucket_search",
        ):
            return (str(kind), ())
        if kind == "folder":
            path = tuple(item.data(0, self.ROLE_FOLDER_PATH) or [])
            return ("folder", path)
        return None

    def _restore_current_selection(
        self, saved: tuple[str, tuple]
    ) -> bool:
        """Re-select the row identified by *saved*. Returns True on success."""
        kind, key = saved
        if kind in (
            "bucket_all", "bucket_unfiled",
            "bucket_all_playlists", "bucket_search",
        ):
            for i in range(self._tree.topLevelItemCount()):
                it = self._tree.topLevelItem(i)
                if it.data(0, self.ROLE_KIND) == kind:
                    self._tree.setCurrentItem(it)
                    return True
            return False
        if kind == "folder":
            item = self._find_folder_item(list(key))
            if item is not None:
                self._tree.setCurrentItem(item)
                return True
            return False
        return False

    def _restore_expanded(self, paths: set[tuple[str, ...]]) -> None:
        def walk(parent: QTreeWidgetItem) -> None:
            for i in range(parent.childCount()):
                it = parent.child(i)
                if it.data(0, self.ROLE_KIND) == "folder":
                    path = tuple(it.data(0, self.ROLE_FOLDER_PATH) or [])
                    if path in paths:
                        it.setExpanded(True)
                walk(it)

        walk(self._tree.invisibleRootItem())

    def _find_folder_item(
        self, path: list[str]
    ) -> QTreeWidgetItem | None:
        """Locate a folder tree item by its path. None if not present."""
        target = tuple(path)

        def walk(parent: QTreeWidgetItem) -> QTreeWidgetItem | None:
            for i in range(parent.childCount()):
                it = parent.child(i)
                if it.data(0, self.ROLE_KIND) == "folder":
                    if tuple(it.data(0, self.ROLE_FOLDER_PATH) or []) == target:
                        return it
                found = walk(it)
                if found is not None:
                    return found
            return None

        return walk(self._tree.invisibleRootItem())

    def _populate(
        self, parent: QTreeWidgetItem, node: dict, path: list[str]
    ) -> tuple[int, int]:
        folder_count = 0
        item_count = 0
        for name in sorted(node.get("folders", {}).keys(), key=str.lower):
            child_node = node["folders"][name]
            child_path = path + [name]
            folder_item = QTreeWidgetItem(parent, [name])
            folder_item.setIcon(0, self._folder_icon)
            folder_item.setData(0, self.ROLE_KIND, "folder")
            folder_item.setData(0, self.ROLE_FOLDER_PATH, child_path)
            sub_folders, sub_items = self._populate(
                folder_item, child_node, child_path
            )
            folder_count += 1 + sub_folders
            item_count += sub_items
        items = sorted(
            node.get("items", []),
            key=lambda e: ((e.get("name") or "").lower(), e.get("kind") or ""),
        )
        for idx, entry in enumerate(items):
            kind = entry.get("kind")
            name = entry.get("name") or "(unnamed)"
            tw_item = QTreeWidgetItem(parent, [name])
            tw_item.setData(0, self.ROLE_KIND, kind)
            tw_item.setData(0, self.ROLE_PAYLOAD, entry)
            tw_item.setData(0, self.ROLE_ITEM_INDEX, idx)
            if kind == "spreadsheet":
                tw_item.setIcon(0, self._spreadsheet_icon)
                path = from_stored_path(entry.get("path") or "")
                if path:
                    tw_item.setToolTip(0, path)
                    if not os.path.exists(path):
                        tw_item.setForeground(0, Qt.GlobalColor.gray)
                        tw_item.setToolTip(0, f"{path} (missing)")
            elif kind == "playlist":
                tw_item.setIcon(0, self._playlist_icon)
                url = entry.get("url") or ""
                if url:
                    tw_item.setToolTip(0, url)
            item_count += 1
        return folder_count, item_count

    @pyqtSlot(QTreeWidgetItem, int)
    def _on_double_click(self, item: QTreeWidgetItem, column: int) -> None:
        del column
        kind = item.data(0, self.ROLE_KIND)
        payload = item.data(0, self.ROLE_PAYLOAD)
        if not payload:
            return
        if kind == "spreadsheet":
            path = from_stored_path(payload.get("path") or "")
            if not path:
                return
            if not os.path.exists(path):
                QMessageBox.warning(self, "File missing",
                                    f"Spreadsheet not found:\n{path}")
                return
            self.spreadsheet_activated.emit(path)
        elif kind == "playlist":
            self.playlist_activated.emit(
                payload.get("url") or "", payload.get("name") or ""
            )

    @pyqtSlot(QTreeWidgetItem, QTreeWidgetItem)
    def _on_current_item_changed(
        self,
        current: QTreeWidgetItem | None,
        previous: QTreeWidgetItem | None,
    ) -> None:
        del previous
        if current is None:
            return
        kind = current.data(0, self.ROLE_KIND)
        if kind == "bucket_all":
            self.folder_selected.emit(None, "")
            return
        if kind == "bucket_all_playlists":
            self.show_playlists.emit()
            return
        if kind == "bucket_search":
            self.show_search.emit()
            return
        if kind == "bucket_unfiled":
            try:
                org = load_org()
            except Exception:
                self.folder_selected.emit(set(), "Unfiled")
                return
            referenced = _collect_spreadsheet_paths(org.get("root", {}))
            on_disk = {_norm_path(r["path"]) for r in _scan_output_dir()}
            self.folder_selected.emit(on_disk - referenced, "Unfiled")
            return
        if kind == "folder":
            try:
                org = load_org()
            except Exception:
                self.folder_selected.emit(set(), "")
                return
            folder_path = current.data(0, self.ROLE_FOLDER_PATH) or []
            node = get_node(org, folder_path) or {}
            label = "/".join(folder_path) if folder_path else ""
            self.folder_selected.emit(_collect_spreadsheet_paths(node), label)
            return
        # Spreadsheet/playlist leaf selection doesn't change the list filter.

    # --- Drop handlers (invoked by _OrgTree) -------------------------------
    def _handle_org_drop(
        self, payload: dict, dest_path: list[str], copy_mode: bool
    ) -> None:
        """Apply an internal tree drop (move or Shift-copy) to organization.json."""
        try:
            org = load_org()
            if payload.get("kind") == "folder":
                self._move_or_copy_folder(
                    org, payload.get("path") or [], dest_path, copy_mode
                )
            elif payload.get("kind") == "item":
                self._move_or_copy_item(
                    org,
                    payload.get("from") or [],
                    int(payload.get("index", -1)),
                    dest_path,
                    copy_mode,
                )
            else:
                return
            save_org(org)
        except _DropError as e:
            QMessageBox.warning(self, "Cannot move", str(e))
            return
        except Exception as e:
            logger.exception("Drop failed")
            QMessageBox.critical(self, "Drop failed", str(e))
            return
        self.refresh()

    def _handle_playlist_drop(
        self, payload: dict, dest_path: list[str]
    ) -> None:
        """File a playlist from the All Playlists view into *dest_path*.

        Dedupes by URL within the destination folder so dragging the same row
        twice does not add a second copy.
        """
        url = (payload or {}).get("url") or ""
        name = (payload or {}).get("name") or ""
        if not url:
            return
        try:
            org = load_org()
            node = get_node(org, dest_path)
            if node is None:
                raise _DropError(
                    f"Folder not found: /{'/'.join(dest_path)}"
                )
            items = node.setdefault("items", [])
            for existing in items:
                if (
                    existing.get("kind") == "playlist"
                    and (existing.get("url") or "") == url
                ):
                    # Already filed here — silent no-op.
                    return
            items.append({
                "kind": "playlist",
                "url": url,
                "name": name or url,
            })
            save_org(org)
        except _DropError as e:
            QMessageBox.warning(self, "Cannot add", str(e))
            return
        except Exception as e:
            logger.exception("Playlist drop failed")
            QMessageBox.critical(self, "Drop failed", str(e))
            return
        self.refresh()

    def _handle_spreadsheet_drop(self, path: str, dest_path: list[str]) -> None:
        """Add a spreadsheet from the main list into the dest folder."""
        try:
            org = load_org()
            node = get_node(org, dest_path)
            if node is None:
                raise _DropError(f"Folder not found: /{'/'.join(dest_path)}")
            items = node.setdefault("items", [])
            dest_norm = _norm_path(path)
            for existing in items:
                if (
                    existing.get("kind") == "spreadsheet"
                    and _norm_path(from_stored_path(existing.get("path") or "")) == dest_norm
                ):
                    # Already filed here — silent no-op.
                    return
            items.append({
                "kind": "spreadsheet",
                "path": to_stored_path(path),
                "name": os.path.basename(path),
            })
            save_org(org)
        except _DropError as e:
            QMessageBox.warning(self, "Cannot add", str(e))
            return
        except Exception as e:
            logger.exception("Spreadsheet drop failed")
            QMessageBox.critical(self, "Drop failed", str(e))
            return
        self.refresh()

    def _move_or_copy_item(
        self,
        org: dict,
        src_path: list[str],
        index: int,
        dest_path: list[str],
        copy_mode: bool,
    ) -> None:
        src_node = get_node(org, src_path)
        dest_node = get_node(org, dest_path)
        if src_node is None or dest_node is None:
            raise _DropError("Source or destination folder not found (tree may be stale — try Refresh).")
        items = src_node.get("items") or []
        if not (0 <= index < len(items)):
            raise _DropError("Item no longer exists at that position (try Refresh).")
        entry = items[index]
        if src_path == dest_path and not copy_mode:
            return  # no-op
        dest_items = dest_node.setdefault("items", [])
        dest_items.append(_copy.deepcopy(entry))
        if not copy_mode:
            # Remove only after append succeeds. Same-folder copy already returned above.
            src_node["items"].pop(index)

    def _move_or_copy_folder(
        self,
        org: dict,
        src_path: list[str],
        dest_path: list[str],
        copy_mode: bool,
    ) -> None:
        if not src_path:
            raise _DropError("Cannot move the root.")
        # Guard: can't drop a folder into itself or a descendant.
        if _is_prefix(src_path, dest_path):
            raise _DropError("Cannot move a folder into itself or one of its descendants.")
        src_parent = get_node(org, src_path[:-1])
        dest_node = get_node(org, dest_path)
        if src_parent is None or dest_node is None:
            raise _DropError("Source or destination folder not found (tree may be stale — try Refresh).")
        name = src_path[-1]
        dest_folders = dest_node.setdefault("folders", {})
        # Same-location no-op for a plain move.
        if src_path[:-1] == dest_path and not copy_mode:
            return
        if name in dest_folders:
            raise _DropError(
                f"A folder named '{name}' already exists in the destination."
            )
        if copy_mode:
            dest_folders[name] = _copy.deepcopy(src_parent["folders"][name])
        else:
            dest_folders[name] = src_parent["folders"].pop(name)

    # --- Clipboard / context menu ------------------------------------------
    def _cut_or_copy_from_tree(self, item: QTreeWidgetItem, op: str) -> None:
        kind = item.data(0, self.ROLE_KIND)
        if kind == "folder":
            folder_path = item.data(0, self.ROLE_FOLDER_PATH) or []
            if not folder_path:
                return
            self._clipboard = {
                "source": "tree_folder", "path": list(folder_path), "op": op,
            }
        elif kind in ("spreadsheet", "playlist"):
            parent_item = item.parent()
            if parent_item is not None and parent_item.data(0, self.ROLE_KIND) == "folder":
                src_path = parent_item.data(0, self.ROLE_FOLDER_PATH) or []
            else:
                src_path = []
            idx = item.data(0, self.ROLE_ITEM_INDEX)
            if idx is None:
                return
            self._clipboard = {
                "source": "tree_item",
                "from": list(src_path),
                "index": int(idx),
                "op": op,
            }
        else:
            return
        self._apply_clipboard_style()
        self.clipboard_changed.emit()

    def _cut_or_copy_items_from_tree(
        self, items: list[QTreeWidgetItem], op: str
    ) -> None:
        """Bulk-clipboard variant: store a list of leaf refs.

        Each ref is `{from: [...folder path...], index: int}`. Folders are
        rejected — caller is expected to pre-filter to leaf rows.
        """
        refs: list[dict] = []
        for it in items:
            kind = it.data(0, self.ROLE_KIND)
            if kind not in ("spreadsheet", "playlist"):
                continue
            parent_item = it.parent()
            if (
                parent_item is not None
                and parent_item.data(0, self.ROLE_KIND) == "folder"
            ):
                src_path = parent_item.data(0, self.ROLE_FOLDER_PATH) or []
            else:
                src_path = []
            idx = it.data(0, self.ROLE_ITEM_INDEX)
            if idx is None:
                continue
            refs.append({"from": list(src_path), "index": int(idx)})
        if not refs:
            return
        self._clipboard = {"source": "tree_items", "items": refs, "op": op}
        self._apply_clipboard_style()
        self.clipboard_changed.emit()

    def set_clipboard_from_list(self, path: str, op: str) -> None:
        """Called by MainWindow when the user Cut/Copy-ed a row in the main list."""
        self._clipboard = {"source": "list_file", "path": path, "op": op}
        self._apply_clipboard_style()
        self.clipboard_changed.emit()

    def set_clipboard_from_list_multi(self, paths: list[str], op: str) -> None:
        """Bulk variant: Cut/Copy of N rows from the main list."""
        deduped = [p for p in dict.fromkeys(paths) if p]
        if not deduped:
            return
        self._clipboard = {"source": "list_files", "paths": deduped, "op": op}
        self._apply_clipboard_style()
        self.clipboard_changed.emit()

    def _clear_clipboard(self) -> None:
        self._clipboard = None
        self._apply_clipboard_style()
        self.clipboard_changed.emit()

    def _paste_into(self, dest_path: list[str]) -> None:
        clip = self._clipboard
        if clip is None:
            return
        try:
            org = load_org()
            src = clip["source"]
            op = clip.get("op", "copy")
            if src == "tree_folder":
                self._move_or_copy_folder(
                    org, clip["path"], dest_path, copy_mode=(op == "copy")
                )
            elif src == "tree_item":
                self._move_or_copy_item(
                    org, clip["from"], int(clip["index"]),
                    dest_path, copy_mode=(op == "copy"),
                )
            elif src == "tree_items":
                refs = list(clip.get("items") or [])
                copy_mode = (op == "copy")
                # Snapshot every entry up-front (a) so we can append to dest
                # before mutating sources, and (b) so removes in cut-mode can
                # process highest-index-first per folder without re-reading.
                snapshots: list[tuple[list[str], int, dict]] = []
                for ref in refs:
                    src_node = get_node(org, ref["from"])
                    if src_node is None:
                        raise _DropError(
                            "Source folder not found (tree may be stale — try Refresh)."
                        )
                    src_items = src_node.get("items") or []
                    idx = int(ref["index"])
                    if not (0 <= idx < len(src_items)):
                        raise _DropError(
                            "Item no longer exists at that position (try Refresh)."
                        )
                    snapshots.append((list(ref["from"]), idx, src_items[idx]))
                dest_node = get_node(org, dest_path)
                if dest_node is None:
                    raise _DropError("Destination folder not found.")
                dest_items = dest_node.setdefault("items", [])
                for _from, _idx, entry in snapshots:
                    # Same-folder cut → no-op for that item; same-folder copy
                    # appends a duplicate (matches single-item paste behavior).
                    if _from == dest_path and not copy_mode:
                        continue
                    dest_items.append(_copy.deepcopy(entry))
                if not copy_mode:
                    # Remove sources in descending index order, grouped by
                    # folder, so earlier indices stay valid through the loop.
                    by_folder: dict[tuple, list[int]] = {}
                    for _from, _idx, _entry in snapshots:
                        if _from == dest_path:
                            continue  # same-folder cut was a no-op above
                        by_folder.setdefault(tuple(_from), []).append(_idx)
                    for folder_key, idxs in by_folder.items():
                        node = get_node(org, list(folder_key))
                        if node is None:
                            continue
                        items_list = node.get("items") or []
                        for i in sorted(idxs, reverse=True):
                            if 0 <= i < len(items_list):
                                items_list.pop(i)
            elif src == "list_file":
                # Same semantics as a spreadsheet drag from the list.
                node = get_node(org, dest_path)
                if node is None:
                    raise _DropError("Destination folder not found.")
                items = node.setdefault("items", [])
                dest_norm = _norm_path(clip["path"])
                already_there = any(
                    e.get("kind") == "spreadsheet"
                    and _norm_path(from_stored_path(e.get("path") or "")) == dest_norm
                    for e in items
                )
                if not already_there:
                    items.append({
                        "kind": "spreadsheet",
                        "path": to_stored_path(clip["path"]),
                        "name": os.path.basename(clip["path"]),
                    })
            elif src == "list_files":
                # Bulk variant of list_file: append every path that isn't
                # already in the destination folder.
                node = get_node(org, dest_path)
                if node is None:
                    raise _DropError("Destination folder not found.")
                items = node.setdefault("items", [])
                existing_norms = {
                    _norm_path(from_stored_path(e.get("path") or ""))
                    for e in items
                    if e.get("kind") == "spreadsheet"
                }
                for p in clip.get("paths") or []:
                    if not p:
                        continue
                    if _norm_path(p) in existing_norms:
                        continue
                    items.append({
                        "kind": "spreadsheet",
                        "path": to_stored_path(p),
                        "name": os.path.basename(p),
                    })
                    existing_norms.add(_norm_path(p))
            else:
                return
            save_org(org)
        except _DropError as e:
            QMessageBox.warning(self, "Cannot paste", str(e))
            return
        except Exception as e:
            logger.exception("Paste failed")
            QMessageBox.critical(self, "Paste failed", str(e))
            return
        # "Cut" semantics: consume the clipboard once the source was removed.
        # "Copy" keeps the clipboard so users can paste again.
        # list_file{,s} Cut is effectively Copy — the source files aren't
        # removed, so keep the clipboard too.
        if (
            clip.get("op") == "cut"
            and clip.get("source") not in ("list_file", "list_files")
        ):
            self._clipboard = None
        self.refresh()
        self.clipboard_changed.emit()

    def _apply_clipboard_style(self) -> None:
        """Italic + gray the source row when clipboard op is 'cut'."""
        clip = self._clipboard
        mark_folder: list[str] | None = None
        mark_item: tuple[list[str], int] | None = None
        mark_items: set[tuple[tuple[str, ...], int]] = set()
        if clip is not None and clip.get("op") == "cut":
            if clip["source"] == "tree_folder":
                mark_folder = clip["path"]
            elif clip["source"] == "tree_item":
                mark_item = (clip["from"], int(clip["index"]))
            elif clip["source"] == "tree_items":
                for ref in clip.get("items") or []:
                    mark_items.add(
                        (tuple(ref.get("from") or []), int(ref["index"]))
                    )

        def walk(parent: QTreeWidgetItem) -> None:
            for i in range(parent.childCount()):
                it = parent.child(i)
                kind = it.data(0, self.ROLE_KIND)
                is_cut = False
                if kind == "folder" and mark_folder is not None:
                    path = it.data(0, self.ROLE_FOLDER_PATH) or []
                    if path == mark_folder:
                        is_cut = True
                elif kind in ("spreadsheet", "playlist"):
                    parent_kind = parent.data(0, self.ROLE_KIND)
                    parent_path = (
                        parent.data(0, self.ROLE_FOLDER_PATH) or []
                        if parent_kind == "folder" else []
                    )
                    idx = it.data(0, self.ROLE_ITEM_INDEX)
                    idx_int = int(idx) if idx is not None else -1
                    if mark_item is not None and (parent_path, idx_int) == mark_item:
                        is_cut = True
                    elif mark_items and (tuple(parent_path), idx_int) in mark_items:
                        is_cut = True
                font = it.font(0)
                font.setItalic(is_cut)
                it.setFont(0, font)
                # Only override color for cut; leave existing (gray for missing
                # files, default for present) otherwise.
                if is_cut:
                    it.setForeground(0, Qt.GlobalColor.gray)
                walk(it)

        walk(self._tree.invisibleRootItem())

    # --- Folder CRUD / item deletion ---------------------------------------
    def _create_folder_under(self, parent_path: list[str]) -> None:
        name, ok = QInputDialog.getText(
            self, "New Folder", "Folder name:"
        )
        if not ok:
            return
        name = name.strip()
        if not name:
            return
        try:
            org = load_org()
            create_folder(org, parent_path, name)
            save_org(org)
        except ValueError as e:
            QMessageBox.warning(self, "Cannot create folder", str(e))
            return
        except Exception as e:
            logger.exception("Create folder failed")
            QMessageBox.critical(self, "Create folder failed", str(e))
            return
        self.refresh()
        # Expand the parent so the newly-created subfolder is visible.
        if parent_path:
            parent_item = self._find_folder_item(parent_path)
            if parent_item is not None:
                self._tree.expandItem(parent_item)

    def _delete_folder_action(self, path: list[str]) -> None:
        if not path:
            return
        reply = QMessageBox.question(
            self,
            "Delete folder",
            f"Delete folder '{path[-1]}' and everything in it?\n"
            "(Files on disk are not affected.)",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            org = load_org()
            delete_folder(org, path)
            save_org(org)
        except ValueError as e:
            QMessageBox.warning(self, "Cannot delete folder", str(e))
            return
        except Exception as e:
            logger.exception("Delete folder failed")
            QMessageBox.critical(self, "Delete folder failed", str(e))
            return
        self._clear_clipboard()
        self.refresh()

    def _rename_folder_action(self, path: list[str]) -> None:
        if not path:
            return
        old = path[-1]
        new_name, ok = QInputDialog.getText(
            self, "Rename folder", "New folder name:", text=old
        )
        if not ok:
            return
        new_name = new_name.strip()
        if not new_name or new_name == old:
            return
        try:
            org = load_org()
            rename_folder(org, path, new_name)
            save_org(org)
        except ValueError as e:
            QMessageBox.warning(self, "Cannot rename folder", str(e))
            return
        except Exception as e:
            logger.exception("Rename folder failed")
            QMessageBox.critical(self, "Rename folder failed", str(e))
            return
        # Stored folder paths in the clipboard are now stale; clearing avoids
        # a paste landing in the wrong place (or failing silently).
        self._clear_clipboard()
        self.refresh()
        # Re-select and reveal the renamed folder so the user can see the
        # rename took effect.
        new_path = path[:-1] + [new_name]
        new_item = self._find_folder_item(new_path)
        if new_item is not None:
            self._tree.setCurrentItem(new_item)
            self._tree.scrollToItem(new_item)

    def _remove_item_from_folder(
        self, folder_path: list[str], index: int
    ) -> None:
        try:
            org = load_org()
            remove_item(org, folder_path, index)
            save_org(org)
        except ValueError as e:
            QMessageBox.warning(self, "Cannot remove", str(e))
            return
        except Exception as e:
            logger.exception("Remove item failed")
            QMessageBox.critical(self, "Remove failed", str(e))
            return
        self._clear_clipboard()
        # Select the parent folder before refreshing so _snapshot_current_selection
        # captures it; otherwise the leaf item returns None and refresh falls back
        # to "All Spreadsheets", changing what the viewer shows.
        if folder_path:
            parent_item = self._find_folder_item(folder_path)
            if parent_item is not None:
                self._tree.setCurrentItem(parent_item)
        self.refresh()

    def _remove_items_from_folder(self, refs: list[dict]) -> None:
        """Bulk remove from folder. *refs* is `[{from: [...], index: int}, ...]`.

        Removes per-folder in descending-index order so indices stay valid.
        """
        if not refs:
            return
        try:
            org = load_org()
            by_folder: dict[tuple, list[int]] = {}
            for ref in refs:
                key = tuple(ref.get("from") or [])
                by_folder.setdefault(key, []).append(int(ref["index"]))
            for folder_key, idxs in by_folder.items():
                folder_path = list(folder_key)
                for i in sorted(idxs, reverse=True):
                    try:
                        remove_item(org, folder_path, i)
                    except ValueError:
                        # Stale index — skip and keep going.
                        continue
            save_org(org)
        except Exception as e:
            logger.exception("Bulk remove failed")
            QMessageBox.critical(self, "Remove failed", str(e))
            return
        self._clear_clipboard()
        # Restore selection focus to the most-likely parent (first ref's folder).
        first_folder = list(refs[0].get("from") or [])
        if first_folder:
            parent_item = self._find_folder_item(first_folder)
            if parent_item is not None:
                self._tree.setCurrentItem(parent_item)
        self.refresh()

    def _delete_spreadsheet_files(self, paths: list[str]) -> None:
        """Bulk delete-from-disk + strip refs everywhere. Single confirmation."""
        paths = [p for p in paths if p]
        if not paths:
            return
        names = [os.path.basename(p) for p in paths]
        preview = "\n".join(f"  • {n}" for n in names[:8])
        if len(names) > 8:
            preview += f"\n  • … and {len(names) - 8} more"
        reply = QMessageBox.question(
            self,
            "Delete files",
            f"Delete {len(paths)} files from disk?\n\n{preview}\n\n"
            "This cannot be undone. All folder references will be removed too.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        failures: list[tuple[str, str]] = []
        try:
            org = load_org()
            for p in paths:
                target_norm = _norm_path(p)
                self._strip_spreadsheet_refs(org.get("root", {}), target_norm)
            save_org(org)
        except Exception as e:
            logger.exception("Bulk delete: refs strip failed")
            QMessageBox.critical(self, "Delete failed", str(e))
            return
        for p in paths:
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError as e:
                failures.append((os.path.basename(p), str(e)))
        if failures:
            joined = "\n".join(f"  • {n}: {err}" for n, err in failures[:8])
            QMessageBox.warning(
                self, "Some files could not be deleted",
                f"{len(failures)} file(s) failed to delete:\n\n{joined}"
            )
        self._clear_clipboard()
        self.refresh()

    def _delete_spreadsheet_file(self, path: str) -> None:
        """Delete the file from disk AND strip every reference from the org tree."""
        name = os.path.basename(path)
        reply = QMessageBox.question(
            self,
            "Delete file",
            f"Delete '{name}' from disk?\n"
            "This cannot be undone. All folder references will be removed too.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            org = load_org()
            target_norm = _norm_path(path)
            self._strip_spreadsheet_refs(org.get("root", {}), target_norm)
            save_org(org)
            if os.path.exists(path):
                os.remove(path)
        except Exception as e:
            logger.exception("Delete file failed")
            QMessageBox.critical(self, "Delete failed", str(e))
            return
        self._clear_clipboard()
        self.refresh()

    def _strip_spreadsheet_refs(self, node: dict, target_norm: str) -> None:
        items = node.get("items") or []
        node["items"] = [
            e for e in items
            if not (
                e.get("kind") == "spreadsheet"
                and _norm_path(from_stored_path(e.get("path") or "")) == target_norm
            )
        ]
        for sub in node.get("folders", {}).values():
            self._strip_spreadsheet_refs(sub, target_norm)

    def _rename_spreadsheet_file(self, path: str) -> None:
        """Prompt for a new name, rename the .xlsx on disk, rewrite refs."""
        if not path or not os.path.exists(path):
            QMessageBox.warning(
                self, "Rename",
                f"'{os.path.basename(path)}' is not on disk."
            )
            return
        old_basename = os.path.basename(path)
        # Show full basename including ext so users see / can override .xlsx.
        new_name, ok = QInputDialog.getText(
            self, "Rename spreadsheet",
            f"New filename for '{old_basename}':",
            QLineEdit.EchoMode.Normal,
            old_basename,
        )
        if not ok:
            return
        new_name = new_name.strip()
        if not new_name:
            QMessageBox.warning(self, "Rename", "Name cannot be empty.")
            return
        # Disallow path separators — rename is in-place, not a move.
        if "/" in new_name or "\\" in new_name:
            QMessageBox.warning(
                self, "Rename",
                "Name cannot contain '/' or '\\'."
            )
            return
        if not new_name.lower().endswith(".xlsx"):
            new_name += ".xlsx"
        new_path = os.path.join(os.path.dirname(path), new_name)
        if _norm_path(new_path) == _norm_path(path):
            return  # no-op
        if os.path.exists(new_path):
            QMessageBox.warning(
                self, "Rename",
                f"'{new_name}' already exists in the output folder."
            )
            return
        try:
            os.rename(path, new_path)
        except OSError as e:
            logger.exception("Rename file failed")
            QMessageBox.critical(
                self, "Rename failed",
                f"Could not rename file: {e}\n\n"
                "If the spreadsheet is open in Excel, close it first."
            )
            return
        try:
            org = load_org()
            old_norm = _norm_path(path)
            updated = self._rewrite_spreadsheet_refs(
                org.get("root", {}), old_norm, new_path
            )
            if updated:
                save_org(org)
        except Exception:
            logger.exception("Rewriting org refs after rename failed")
            # The file rename succeeded; org refs may be stale. We still emit
            # the rename signal so the UI refreshes — refs that didn't update
            # will simply look broken until the user fixes them by hand.
        # Update the clipboard if it was pointing at the old path.
        clip = self._clipboard
        if (
            clip is not None
            and clip.get("source") == "list_file"
            and _norm_path(clip.get("path") or "") == _norm_path(path)
        ):
            clip["path"] = new_path
        self.refresh()
        self.spreadsheet_renamed.emit(path, new_path)

    def _rewrite_spreadsheet_refs(
        self, node: dict, old_norm: str, new_path: str
    ) -> int:
        """Walk the org tree; rewrite spreadsheet refs whose path matches.

        Returns the number of references updated. Also refreshes the *name*
        field to the new basename for any rewritten entry.
        """
        new_basename = os.path.basename(new_path)
        count = 0
        for entry in node.get("items", []) or []:
            if (
                entry.get("kind") == "spreadsheet"
                and _norm_path(from_stored_path(entry.get("path") or "")) == old_norm
            ):
                entry["path"] = to_stored_path(new_path)
                entry["name"] = new_basename
                count += 1
        for sub in (node.get("folders", {}) or {}).values():
            count += self._rewrite_spreadsheet_refs(sub, old_norm, new_path)
        return count


class _DropError(Exception):
    """User-visible drop failure."""


def _is_prefix(prefix: list[str], path: list[str]) -> bool:
    """True if `prefix` equals `path` or is a proper ancestor of it."""
    if len(prefix) > len(path):
        return False
    return path[: len(prefix)] == prefix


def _norm_path(path: str) -> str:
    """Normalize a path for equality comparison across Windows quirks."""
    try:
        return os.path.normcase(os.path.abspath(path))
    except Exception:
        return path


def _collect_spreadsheet_paths(node: dict) -> set[str]:
    """Walk a folder node and return normalized spreadsheet paths under it."""
    paths: set[str] = set()
    for item in node.get("items", []):
        if item.get("kind") == "spreadsheet":
            p = from_stored_path(item.get("path") or "")
            if p:
                paths.add(_norm_path(p))
    for sub in node.get("folders", {}).values():
        paths.update(_collect_spreadsheet_paths(sub))
    return paths


# ---------------------------------------------------------------------------
# Browser worker: owns an asyncio loop on its own QThread and keeps a
# BrowserManager alive for the lifetime of a "connected" session.
# ---------------------------------------------------------------------------
async def _minimize_browser_window(manager: BrowserManager) -> None:
    """Minimize the launched Chrome via Win32 ShowWindow (by PID).

    CDP's `Browser.getWindowForTarget` intermittently fails with "Browser
    window not found" even when given an explicit targetId — the window
    binding isn't registered yet right after launch. EnumWindows against
    the Chrome PID is deterministic, no-ops on non-Windows.
    """
    proc = manager._chrome_process
    if proc is None or proc.poll() is not None:
        return
    count = await asyncio.to_thread(minimize_process_windows, proc.pid)
    if count == 0:
        logger.warning("Chrome window not found to minimize (continuing)")


def _keep_awake(enable: bool) -> None:
    """Block (enable=True) or allow (enable=False) Windows idle sleep for the
    calling thread.

    Windows idle-sleep suspends the machine after the user walks away (default
    ~10-30 min), which detaches Chrome's remote-debug port — every subsequent
    Playwright/CDP call then dies with TargetClosedError. A long Classical
    Shuffle generation (hours) is the classic victim. Bracketing the live
    browser session with this keeps the SYSTEM awake so the CDP connection
    survives; the DISPLAY is intentionally allowed to turn off (system stays
    running, so the connection is fine — only full system sleep drops it).
    ES_CONTINUOUS makes the request persist on this thread until cleared;
    clearing on disconnect is essential so we don't leak the no-sleep state.
    No-op on non-Windows / if the API is unavailable.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ES_CONTINUOUS = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        # NOTE: deliberately NOT ES_DISPLAY_REQUIRED — we block system sleep
        # but let the monitor power off (user preference). Blocking system
        # sleep alone is enough to keep the CDP connection alive.
        flags = (
            (ES_CONTINUOUS | ES_SYSTEM_REQUIRED) if enable else ES_CONTINUOUS
        )
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
        logger.info("Keep-awake %s (idle-sleep %s)",
                    "enabled" if enable else "cleared",
                    "blocked" if enable else "allowed")
    except Exception:
        # Best-effort; if SetThreadExecutionState isn't available, just skip.
        logger.debug("Could not set thread execution state", exc_info=True)


class BrowserWorker(QObject):
    connected = pyqtSignal()
    disconnected = pyqtSignal()
    failed = pyqtSignal(str)
    task_completed = pyqtSignal(str, object)  # (task_name, result)
    task_failed = pyqtSignal(str, str)        # (task_name, error_message)

    def __init__(self) -> None:
        super().__init__()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._manager: BrowserManager | None = None
        # Whether to block Windows idle-sleep for the connected session. Set by
        # MainWindow from the user's saved preference before run(); can be
        # changed live via set_keep_awake().
        self.keep_awake = True

    @pyqtSlot()
    def run(self) -> None:
        """Entry point on the worker thread — blocks until disconnect."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._session())
        finally:
            self._loop.close()
            self._loop = None

    async def _session(self) -> None:
        self._stop_event = asyncio.Event()
        try:
            self._manager = BrowserManager(headless=False)
            await self._manager.__aenter__()
            # Leave the window on a blank tab — actions navigate as needed.
            # Minimize so it stays out of the way. Use a browser-level CDP
            # session (Browser.* domain isn't available on per-page sessions)
            # and resolve the window via the page's targetId. Failures here
            # are non-fatal; worst case the window stays on-screen.
            try:
                await _minimize_browser_window(self._manager)
            except Exception:
                logger.exception("Could not minimize Chrome window (continuing)")
            # Block Windows idle-sleep for the whole connected session so a
            # long unattended run (e.g. a multi-hour Classical Shuffle
            # generation) doesn't lose the CDP connection when the user walks
            # away. Honors the user's preference; called on the worker thread,
            # cleared in finally below.
            _keep_awake(self.keep_awake)
            self.connected.emit()
            await self._stop_event.wait()
        except Exception as e:
            logger.exception("Chrome connection failed")
            self.failed.emit(str(e))
        finally:
            _keep_awake(False)
            if self._manager is not None:
                try:
                    await self._manager.__aexit__(None, None, None)
                except Exception:
                    logger.exception("Error during Chrome shutdown")
                self._manager = None
            self.disconnected.emit()

    def request_stop(self) -> None:
        """Called from the GUI thread to end the session."""
        loop = self._loop
        event = self._stop_event
        if loop is not None and event is not None:
            loop.call_soon_threadsafe(event.set)

    def set_keep_awake(self, enable: bool) -> None:
        """Apply a keep-awake preference change to the LIVE session.

        Called from the GUI thread. The SetThreadExecutionState assertion is
        thread-scoped, so we schedule the actual call onto the worker thread's
        loop (where the session asserted it) rather than running it here.
        """
        self.keep_awake = enable
        loop = self._loop
        if loop is not None:
            loop.call_soon_threadsafe(_keep_awake, enable)

    def submit_task(self, async_fn, task_name: str) -> None:
        """Schedule an async task against the live browser page.

        `async_fn` is a callable that takes a Playwright Page and returns
        a result. Runs on the worker thread's asyncio loop; emits
        `task_completed` or `task_failed` with the task name.
        """
        loop = self._loop
        if loop is None or self._manager is None:
            self.task_failed.emit(task_name, "Not connected to Chrome")
            return
        asyncio.run_coroutine_threadsafe(
            self._run_task(async_fn, task_name), loop
        )

    async def _run_task(self, async_fn, task_name: str) -> None:
        if self._manager is None or self._manager.page is None:
            self.task_failed.emit(task_name, "Browser page is not available")
            return
        page = self._manager.page
        if page.is_closed():
            self.task_failed.emit(
                task_name,
                "The browser tab was closed. Disconnect and reconnect, then try again."
            )
            return
        try:
            from playwright._impl._errors import TargetClosedError as _TargetClosedError
        except ImportError:
            _TargetClosedError = None

        try:
            result = await async_fn(page)
            self.task_completed.emit(task_name, result)
        except Exception as e:
            # Playwright raises TargetClosedError when the tab/browser disappears
            # mid-task. Give a friendlier message than the raw Playwright traceback.
            if _TargetClosedError is not None and isinstance(e, _TargetClosedError):
                msg = "The browser tab was closed during the task. Disconnect and reconnect, then try again."
                logger.error("Task %s failed: browser target closed", task_name)
                self.task_failed.emit(task_name, msg)
            else:
                logger.exception("Task %s failed", task_name)
                self.task_failed.emit(task_name, str(e))


# ---------------------------------------------------------------------------
# Background tasks: off-thread execution for non-browser work.
# ---------------------------------------------------------------------------
class _FnRunnable(QRunnable):
    def __init__(self, fn, host: "BackgroundTasks", name: str) -> None:
        super().__init__()
        self._fn = fn
        self._host = host
        self._name = name

    def run(self) -> None:
        try:
            result = self._fn()
            self._host.task_completed.emit(self._name, result)
        except Exception as e:
            logger.exception("Background task %s failed", self._name)
            self._host.task_failed.emit(self._name, str(e))


class BackgroundTasks(QObject):
    task_completed = pyqtSignal(str, object)
    task_failed = pyqtSignal(str, str)

    def submit(self, fn, name: str) -> None:
        QThreadPool.globalInstance().start(_FnRunnable(fn, self, name))


# ---------------------------------------------------------------------------
# Folder picker — small tree-style dialog for choosing a destination folder
# in organization.json. Used by "Add to folder…" actions.
# ---------------------------------------------------------------------------
class _FolderPickerDialog(QDialog):
    """Modal dialog with a folder tree. ``chosen_path`` returns the picked
    path as ``list[str]`` (empty list = root)."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        title: str = "Pick a folder",
        prompt: str = "Select a destination folder:",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(360)
        self.setMinimumHeight(420)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(prompt))

        self._tree = QTreeWidget()
        self._tree.setHeaderHidden(True)
        self._tree.itemDoubleClicked.connect(self._on_double_click)
        layout.addWidget(self._tree, 1)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

        self._populate()

    def _populate(self) -> None:
        try:
            org = load_org()
        except Exception:
            org = {"root": {"folders": {}, "items": []}}
        root_node = org.get("root", {}) or {}
        root_item = QTreeWidgetItem(self._tree)
        root_item.setText(0, "/  (Root)")
        root_item.setData(0, Qt.ItemDataRole.UserRole, [])
        self._fill(root_item, root_node, [])
        self._tree.expandAll()
        self._tree.setCurrentItem(root_item)

    def _fill(
        self, parent_item: QTreeWidgetItem, node: dict, path: list[str]
    ) -> None:
        for name in sorted(node.get("folders", {}) or {}, key=str.lower):
            sub = node["folders"][name]
            child = QTreeWidgetItem(parent_item)
            child.setText(0, name)
            child.setData(0, Qt.ItemDataRole.UserRole, path + [name])
            self._fill(child, sub, path + [name])

    @pyqtSlot(QTreeWidgetItem, int)
    def _on_double_click(self, item: QTreeWidgetItem, _col: int) -> None:
        # Double-click acts as accept.
        self.accept()

    def chosen_path(self) -> list[str] | None:
        item = self._tree.currentItem()
        if item is None:
            return None
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if data is None:
            return None
        return list(data)


# ---------------------------------------------------------------------------
# Export: dialog + coroutine.
# ---------------------------------------------------------------------------
class ExportDialog(QDialog):
    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        prefill_url: str = "",
        prefill_output: str = "",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Export Playlist")
        self.setMinimumWidth(560)

        self._url = QLineEdit(prefill_url)
        self._url.setPlaceholderText("https://music.amazon.com/playlists/...")

        self._output = QLineEdit(
            prefill_output or os.path.join(OUTPUT_DIR, "playlist.xlsx")
        )
        browse = QPushButton("…")
        browse.setMaximumWidth(40)
        browse.clicked.connect(self._on_browse)
        out_row = QHBoxLayout()
        out_row.setContentsMargins(0, 0, 0, 0)
        out_row.addWidget(self._output)
        out_row.addWidget(browse)

        self._metadata = QCheckBox(
            "Look up release year/genre via MusicBrainz (slow, ~1 req/sec)"
        )

        form = QFormLayout()
        form.addRow("Playlist URL:", self._url)
        form.addRow("Output file:", out_row)
        form.addRow("", self._metadata)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(btns)

    @pyqtSlot()
    def _on_browse(self) -> None:
        start = self._output.text().strip() or OUTPUT_DIR
        path, _ = QFileDialog.getSaveFileName(
            self, "Save spreadsheet as", start, "Excel files (*.xlsx)"
        )
        if path:
            if not path.lower().endswith(".xlsx"):
                path += ".xlsx"
            self._output.setText(path)

    def values(self) -> dict:
        return {
            "url": self._url.text().strip(),
            "output": self._output.text().strip(),
            "metadata": self._metadata.isChecked(),
        }


class UpdateDialog(QDialog):
    """Prompts for playlist URL (only if missing from the sheet) + metadata flag."""

    def __init__(self, xlsx_path: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Update Spreadsheet")
        self.setMinimumWidth(560)

        src = {}
        try:
            src = get_source(xlsx_path)
        except Exception:
            logger.exception("Could not read stored source for %s", xlsx_path)
        self._stored_url = src.get("url") or ""
        stored_name = src.get("name") or ""

        layout = QVBoxLayout(self)

        target = QLabel(f"<b>{os.path.basename(xlsx_path)}</b>")
        target.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(target)

        if self._stored_url or stored_name:
            label = stored_name or self._stored_url
            info = QLabel(f"Source on record: <i>{label}</i>")
            info.setTextFormat(Qt.TextFormat.RichText)
            info.setWordWrap(True)
            layout.addWidget(info)
            self._url_input: QLineEdit | None = None
        else:
            form = QFormLayout()
            self._url_input = QLineEdit()
            self._url_input.setPlaceholderText(
                "https://music.amazon.com/playlists/..."
            )
            form.addRow("Playlist URL:", self._url_input)
            layout.addLayout(form)

        self._metadata = QCheckBox(
            "Look up release year/genre for new songs via MusicBrainz"
        )
        layout.addWidget(self._metadata)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def values(self) -> dict:
        url = self._stored_url
        if self._url_input is not None:
            url = self._url_input.text().strip() or url
        return {"url": url, "metadata": self._metadata.isChecked()}


def _find_playlists_by_name(name: str) -> list[dict]:
    """Return all known playlists whose name matches *name* (case-insensitive).

    Searches both organization.json and the playlist cache.
    Each returned entry is ``{"url": ..., "name": ...}``.
    """
    needle = name.strip().lower()
    seen: dict[str, dict] = {}  # url → entry (dedup)

    # org entries
    try:
        org = load_org()
        for entry in _walk_playlist_entries(org.get("root", {})):
            url = entry.get("url") or ""
            n = (entry.get("name") or "").strip().lower()
            if url and n == needle:
                seen[url] = {"url": url, "name": entry.get("name") or name}
    except Exception:
        logger.exception("Could not search organization.json for playlist name")

    # cache entries
    try:
        from playlist_cache import load_cached_playlists
        for pl in (load_cached_playlists() or []):
            url = pl.get("url") or ""
            n = (pl.get("name") or "").strip().lower()
            if url and n == needle:
                seen[url] = {"url": url, "name": pl.get("name") or name}
    except Exception:
        logger.exception("Could not search playlist cache for playlist name")

    return list(seen.values())


def _build_update_coroutine(xlsx_path: str, url: str, metadata: bool):
    """Return an async callable `fn(page)` that appends new songs to *xlsx_path*.

    This is the same logic as `main.cmd_export`'s --update branch, inlined so
    we log through the GUI's logger instead of printing, and reuse the live
    BrowserManager rather than creating a new one.
    """
    async def _do_update(page):
        from auth import _detect_logged_in, _suppress_dialogs
        from config import BASE_URL, LONG_TIMEOUT
        from models import Song
        from scraper import scrape_playlist
        from spreadsheet_tags import set_tag, set_updated, set_source
        from utils import normalize

        if not os.path.exists(xlsx_path):
            raise RuntimeError(f"Spreadsheet missing: {xlsx_path}")
        if not url:
            raise RuntimeError(
                "No playlist URL to update from. Enter one in the dialog."
            )

        logger.info("Checking Amazon Music login…")
        await page.goto(BASE_URL, wait_until="domcontentloaded",
                        timeout=LONG_TIMEOUT)
        await page.wait_for_timeout(2000)
        if not await _detect_logged_in(page):
            raise RuntimeError(
                "Not logged in to Amazon Music. Log in via the Chrome window, "
                "then try Update again."
            )
        await _suppress_dialogs(page)

        playlist = await scrape_playlist(page, playlist_url=url)
        if not playlist.songs:
            raise RuntimeError("No songs found in playlist")

        source_url = url or page.url
        source_name = playlist.name

        wb = load_workbook(xlsx_path)
        # Use the first non-metadata sheet — wb.active can point to the hidden
        # _metadata sheet after tag operations, which would make every song
        # appear "new" and cause the entire playlist to be duplicated.
        ws = next(
            (s for s in wb.worksheets if s.title != TAG_SHEET),
            wb.worksheets[0],
        )

        existing_keys: set[tuple] = set()
        for row in ws.iter_rows(min_row=2, values_only=False):
            title = row[1].value or ""
            if not title:
                continue
            artist = row[2].value or ""
            album = row[3].value or ""
            existing_keys.add(
                (normalize(str(title)), normalize(str(artist)),
                 normalize(str(album)))
            )

        new_songs = [
            s for s in playlist.songs
            if (normalize(s.title), normalize(s.artist), normalize(s.album))
            not in existing_keys
        ]

        # When metadata is requested, also enrich existing rows that are
        # missing year or genre — update those cells in-place.
        enriched_existing = 0
        if metadata:
            from metadata import enrich_songs as _enrich_songs
            stale: list[tuple[int, Song]] = []  # (row_number, Song)
            for row in ws.iter_rows(min_row=2, values_only=False):
                title = row[1].value or ""
                if not title:
                    continue
                year = str(row[4].value or "").strip()
                genre = str(row[5].value or "").strip()
                if year and genre:
                    continue  # already complete
                artist = str(row[2].value or "")
                album = str(row[3].value or "")
                duration = str(row[6].value or "")
                s = Song(title=str(title), artist=artist, album=album,
                         release_date=year, genre=genre, duration=duration)
                stale.append((row[0].row, s))

            if stale:
                logger.info(
                    "Enriching %d existing song(s) missing year/genre via MusicBrainz…",
                    len(stale),
                )
                _enrich_songs([s for _, s in stale])
                for row_num, song in stale:
                    if song.release_date:
                        date_val = str(song.release_date)[:4]
                        ws.cell(row=row_num, column=5, value=date_val)
                    if song.genre:
                        ws.cell(row=row_num, column=6, value=song.genre)
                enriched_existing = len(stale)

            if new_songs:
                logger.info(
                    "Looking up metadata for %d new song(s) via MusicBrainz…",
                    len(new_songs),
                )
                _enrich_songs(new_songs)

        if not new_songs:
            wb.save(xlsx_path)
            wb.close()
            set_tag(xlsx_path, TAG_AMAZON)
            set_updated(xlsx_path)
            set_source(xlsx_path, url=source_url, name=source_name)
            msg = (
                f"No new songs — '{os.path.basename(xlsx_path)}' is up to date "
                f"({len(existing_keys)} songs"
                + (f", enriched {enriched_existing} existing" if enriched_existing else "")
                + ")."
            )
            logger.info(msg)
            return {
                "added": 0,
                "enriched": enriched_existing,
                "total": len(existing_keys),
                "path": xlsx_path,
            }

        logger.info("Found %d new song(s) (existing: %d).",
                    len(new_songs), len(existing_keys))

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
        wb.save(xlsx_path)
        wb.close()
        set_tag(xlsx_path, TAG_AMAZON)
        set_updated(xlsx_path)
        set_source(xlsx_path, url=source_url, name=source_name)
        total = len(existing_keys) + len(new_songs)
        logger.info("Added %d new song(s) to %s (total: %d)",
                    len(new_songs), os.path.basename(xlsx_path), total)
        return {
            "added": len(new_songs),
            "enriched": enriched_existing,
            "total": total,
            "path": xlsx_path,
        }

    return _do_update


def _build_export_coroutine(url: str, output: str, metadata: bool):
    """Return an async callable `fn(page)` that performs the export."""
    async def _do_export(page):
        # Keep imports local so they don't run unless the action is triggered.
        from auth import _detect_logged_in, _suppress_dialogs
        from config import BASE_URL, LONG_TIMEOUT
        from exporter import export_to_xlsx
        from scraper import scrape_playlist

        logger.info("Checking Amazon Music login…")
        await page.goto(BASE_URL, wait_until="domcontentloaded",
                        timeout=LONG_TIMEOUT)
        await page.wait_for_timeout(2000)
        if not await _detect_logged_in(page):
            raise RuntimeError(
                "Not logged in to Amazon Music. Log in via the Chrome window, "
                "then try Export again."
            )
        await _suppress_dialogs(page)

        playlist = await scrape_playlist(page, playlist_url=url)
        if not playlist.songs:
            raise RuntimeError("No songs found in playlist")

        source_url = url or page.url
        if metadata:
            logger.info("Looking up metadata for %d songs via MusicBrainz…",
                        len(playlist.songs))
            from metadata import enrich_songs
            enrich_songs(playlist.songs)

        path = export_to_xlsx(
            playlist, output,
            source_url=source_url, source_name=playlist.name,
        )
        return path

    return _do_export


class MetadataDialog(QDialog):
    def __init__(self, xlsx_path: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Metadata Lookup")
        self.setMinimumWidth(520)

        layout = QVBoxLayout(self)
        target = QLabel(f"<b>{os.path.basename(xlsx_path)}</b>")
        target.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(target)

        note = QLabel(
            "Looks up release year and genre via MusicBrainz (~1 request/sec).\n"
            "VPN must be off."
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        self._force_dates = QCheckBox(
            "Re-lookup all release dates (use if existing years look wrong)"
        )
        layout.addWidget(self._force_dates)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def values(self) -> dict:
        return {"force_dates": self._force_dates.isChecked()}


def _run_metadata_task(xlsx_path: str, force_dates: bool) -> dict:
    """Sync worker for BackgroundTasks: enrich *xlsx_path* via MusicBrainz.

    Port of `main.cmd_metadata` that logs through the GUI logger instead of
    printing. Returns a summary dict.
    """
    from models import Song
    from metadata import enrich_songs

    wb = load_workbook(xlsx_path)
    try:
        ws = wb.active
        songs: list[Song] = []
        for row in ws.iter_rows(min_row=2, values_only=False):
            title = row[1].value or ""
            if not title:
                continue
            songs.append(Song(
                title=str(title),
                artist=str(row[2].value or ""),
                album=str(row[3].value or ""),
                release_date=str(row[4].value or ""),
                genre=str(row[5].value or ""),
            ))

        if force_dates:
            for s in songs:
                s.release_date = ""
            logger.info("Cleared %d release dates for re-lookup", len(songs))

        need = [s for s in songs if not s.release_date or not s.genre]
        logger.info("Loaded %d songs — %d already complete, %d need lookup",
                    len(songs), len(songs) - len(need), len(need))
        if not need:
            return {"enriched": 0, "total": len(songs), "path": xlsx_path}

        def progress(current, total, enriched):
            if current % 25 == 0 or current == total:
                logger.info("  %d/%d looked up (%d enriched)",
                            current, total, enriched)

        enrich_songs(songs, progress_callback=progress)

        enriched_count = 0
        for i, song in enumerate(songs):
            row_idx = i + 2
            date_val = str(song.release_date) if song.release_date else ""
            if len(date_val) > 4:
                date_val = date_val[:4]
            prev_date = ws.cell(row=row_idx, column=5).value or ""
            prev_genre = ws.cell(row=row_idx, column=6).value or ""
            ws.cell(row=row_idx, column=5, value=date_val)
            ws.cell(row=row_idx, column=6, value=song.genre)
            if (str(prev_date) != date_val) or (str(prev_genre) != song.genre):
                enriched_count += 1

        wb.save(xlsx_path)
    finally:
        wb.close()

    logger.info("Metadata enrichment complete for %s",
                os.path.basename(xlsx_path))
    return {"enriched": enriched_count, "total": len(songs), "path": xlsx_path}


# ---------------------------------------------------------------------------
# Filter: dialog + task.
# ---------------------------------------------------------------------------
def _parse_year_range(text: str) -> tuple[int | None, int | None]:
    """Copy of menu._parse_year_range. Accepts '1990', '1990-1999', '1990s', etc."""
    text = text.strip().lower()
    if text.endswith("s") and text[:-1].isdigit():
        decade = int(text[:-1])
        if decade < 100:
            decade = 1900 + decade if decade >= 30 else 2000 + decade
        return decade, decade + 9
    if "-" in text:
        parts = text.split("-")
        if len(parts) == 2:
            try:
                return int(parts[0].strip()), int(parts[1].strip())
            except ValueError:
                pass
    if text.startswith("before "):
        try:
            return 0, int(text[7:].strip()) - 1
        except ValueError:
            pass
    if text.startswith("after "):
        try:
            return int(text[6:].strip()) + 1, 9999
        except ValueError:
            pass
    if text.isdigit():
        y = int(text)
        return y, y
    return None, None


class ReconcilePreviewDialog(QDialog):
    """Shows what will change before a reconcile operation is applied."""

    def __init__(
        self,
        direction: str,
        to_add: list[dict],
        to_remove: list[dict],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Reconcile Preview")
        self.setMinimumWidth(520)
        self.setMinimumHeight(400)
        layout = QVBoxLayout(self)

        layout.addWidget(QLabel(f"<b>{direction}</b>"))
        layout.addSpacing(6)

        def _make_list_widget(songs: list[dict]) -> QListWidget:
            lw = QListWidget()
            for s in songs:
                label = s.get("title", "")
                if s.get("artist"):
                    label += f"  —  {s['artist']}"
                lw.addItem(label)
            lw.setMaximumHeight(180)
            return lw

        n_add = len(to_add)
        n_rem = len(to_remove)

        if n_add:
            layout.addWidget(QLabel(f"Add {n_add} song{'s' if n_add != 1 else ''}:"))
            layout.addWidget(_make_list_widget(to_add))
        else:
            layout.addWidget(QLabel("Nothing to add."))
        layout.addSpacing(8)
        if n_rem:
            layout.addWidget(QLabel(f"Remove {n_rem} song{'s' if n_rem != 1 else ''}:"))
            layout.addWidget(_make_list_widget(to_remove))
        else:
            layout.addWidget(QLabel("Nothing to remove."))

        layout.addSpacing(12)
        btns = QHBoxLayout()
        btns.addStretch(1)
        self._apply_btn = QPushButton("Apply")
        self._apply_btn.setDefault(True)
        self._apply_btn.setEnabled(n_add > 0 or n_rem > 0)
        cancel_btn = QPushButton("Cancel")
        self._apply_btn.clicked.connect(self.accept)
        cancel_btn.clicked.connect(self.reject)
        btns.addWidget(cancel_btn)
        btns.addWidget(self._apply_btn)
        layout.addLayout(btns)


class FilterDialog(QDialog):
    def __init__(self, xlsx_path: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Filter Songs")
        self.setMinimumWidth(580)
        self._xlsx_path = xlsx_path

        layout = QVBoxLayout(self)
        target = QLabel(f"<b>{os.path.basename(xlsx_path)}</b>")
        target.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(target)

        note = QLabel(
            "Leave fields blank to skip that filter. All filters are ANDed."
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        form = QFormLayout()
        self._year = QLineEdit()
        self._year.setPlaceholderText("1990 · 1990-1999 · 1990s · before 2000 · after 1980")
        self._genre = QLineEdit()
        self._genre.setPlaceholderText("rock · pop, rock (comma-OR)")
        self._artist = QLineEdit()
        self._artist.setPlaceholderText("Taylor Swift")
        self._keyword = QLineEdit()
        self._keyword.setPlaceholderText("matches title, artist, or album")
        form.addRow("Year:", self._year)
        form.addRow("Genre:", self._genre)
        form.addRow("Artist:", self._artist)
        form.addRow("Keyword:", self._keyword)
        layout.addLayout(form)

        out_label = QLabel("Save filtered songs to:")
        layout.addWidget(out_label)
        self._output = QLineEdit(os.path.join(OUTPUT_DIR, "filtered.xlsx"))
        browse = QPushButton("…")
        browse.setMaximumWidth(40)
        browse.clicked.connect(self._on_browse)
        out_row = QHBoxLayout()
        out_row.addWidget(self._output)
        out_row.addWidget(browse)
        layout.addLayout(out_row)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    @pyqtSlot()
    def _on_browse(self) -> None:
        start = self._output.text().strip() or OUTPUT_DIR
        path, _ = QFileDialog.getSaveFileName(
            self, "Save filtered spreadsheet as", start, "Excel files (*.xlsx)"
        )
        if path:
            if not path.lower().endswith(".xlsx"):
                path += ".xlsx"
            self._output.setText(path)

    def values(self) -> dict:
        return {
            "year": self._year.text().strip(),
            "genre": self._genre.text().strip(),
            "artist": self._artist.text().strip(),
            "keyword": self._keyword.text().strip(),
            "output": self._output.text().strip(),
        }


def _run_filter_task(xlsx_path: str, filters: dict) -> dict:
    """Apply filters to *xlsx_path*, write matches to filters['output']."""
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from spreadsheet_tags import set_tag, TAG_LOCAL

    wb = load_workbook(xlsx_path, read_only=True, data_only=True)
    try:
        ws = wb.active
        songs: list[dict] = []
        for row in ws.iter_rows(min_row=2, values_only=True):
            if not row or not row[1]:
                continue
            songs.append({
                "title": str(row[1] or ""),
                "artist": str(row[2] or "") if len(row) > 2 else "",
                "album": str(row[3] or "") if len(row) > 3 else "",
                "year": str(row[4] or "") if len(row) > 4 else "",
                "genre": (str(row[5] or "") if len(row) > 5 else "").lower(),
            })
    finally:
        wb.close()

    filtered = songs[:]
    if filters["year"]:
        y0, y1 = _parse_year_range(filters["year"])
        if y0 is not None and y1 is not None:
            filtered = [s for s in filtered
                        if s["year"].isdigit() and y0 <= int(s["year"]) <= y1]
    if filters["genre"]:
        terms = [g.strip().lower() for g in filters["genre"].split(",") if g.strip()]
        filtered = [s for s in filtered
                    if any(t in s["genre"] for t in terms)]
    if filters["artist"]:
        needle = filters["artist"].lower()
        filtered = [s for s in filtered if needle in s["artist"].lower()]
    if filters["keyword"]:
        kw = filters["keyword"].lower()
        filtered = [s for s in filtered
                    if kw in s["title"].lower()
                    or kw in s["artist"].lower()
                    or kw in s["album"].lower()]

    out_path = filters["output"]
    if not out_path.lower().endswith(".xlsx"):
        out_path += ".xlsx"
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

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
    new_wb.save(out_path)
    set_tag(out_path, TAG_LOCAL)

    logger.info("Filter: %d / %d songs matched, saved to %s",
                len(filtered), len(songs), os.path.basename(out_path))
    return {
        "matched": len(filtered),
        "total": len(songs),
        "path": out_path,
    }


# ---------------------------------------------------------------------------
# Merge: dialog + task.
# ---------------------------------------------------------------------------
def _populate_spreadsheet_combo(combo: QComboBox, exclude_path: str = "") -> None:
    """Fill *combo* with ./output xlsx files; store full path in UserRole."""
    combo.clear()
    for row in _scan_output_dir():
        if exclude_path and os.path.abspath(row["path"]) == os.path.abspath(exclude_path):
            continue
        label = row.get("display_name") or row["name"]
        combo.addItem(f"{label}  [{row['tag']}]", row["path"])


class MergeDialog(QDialog):
    def __init__(self, parent: QWidget | None = None,
                 default_target: str = "") -> None:
        super().__init__(parent)
        self.setWindowTitle("Merge Spreadsheets")
        self.setMinimumWidth(560)

        layout = QVBoxLayout(self)
        info = QLabel(
            "Append songs from a <b>source</b> spreadsheet into a "
            "<b>target</b>, skipping duplicates. By default this modifies the "
            "target; tick the box below to write to a new spreadsheet instead."
        )
        info.setTextFormat(Qt.TextFormat.RichText)
        info.setWordWrap(True)
        layout.addWidget(info)

        form = QFormLayout()
        self._target = QComboBox()
        _populate_spreadsheet_combo(self._target)
        if default_target:
            for i in range(self._target.count()):
                if os.path.abspath(self._target.itemData(i)) == os.path.abspath(default_target):
                    self._target.setCurrentIndex(i)
                    break

        self._source = QComboBox()
        _populate_spreadsheet_combo(
            self._source, exclude_path=self._target.currentData() or ""
        )
        self._target.currentIndexChanged.connect(self._on_target_changed)

        form.addRow("Source:", self._source)
        form.addRow("Target:", self._target)
        layout.addLayout(form)

        self._new_file_cb = QCheckBox("Save to a new spreadsheet (don't modify target)")
        self._new_file_cb.toggled.connect(self._on_new_file_toggled)
        layout.addWidget(self._new_file_cb)

        new_form = QFormLayout()
        self._new_name = QLineEdit()
        self._new_name.setPlaceholderText("merged_playlist")
        self._new_name.setEnabled(False)
        new_form.addRow("New filename:", self._new_name)
        layout.addLayout(new_form)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    @pyqtSlot(int)
    def _on_target_changed(self, _idx: int) -> None:
        current_source = self._source.currentData()
        _populate_spreadsheet_combo(
            self._source, exclude_path=self._target.currentData() or ""
        )
        if current_source:
            for i in range(self._source.count()):
                if self._source.itemData(i) == current_source:
                    self._source.setCurrentIndex(i)
                    break

    @pyqtSlot(bool)
    def _on_new_file_toggled(self, checked: bool) -> None:
        self._new_name.setEnabled(checked)
        if checked:
            self._new_name.setFocus()

    def values(self) -> dict:
        new_name = self._new_name.text().strip() if self._new_file_cb.isChecked() else ""
        new_path = ""
        if new_name:
            if not new_name.lower().endswith(".xlsx"):
                new_name += ".xlsx"
            # Keep output under ./output/ unless the user typed a path.
            if os.path.dirname(new_name):
                new_path = new_name
            else:
                new_path = os.path.join(OUTPUT_DIR, new_name)
        return {
            "target": self._target.currentData() or "",
            "source": self._source.currentData() or "",
            "new_path": new_path,
        }


def _run_merge_task(target_path: str, source_path: str) -> dict:
    """Append source songs into target; skip rows whose (title, artist) match."""
    from utils import normalize

    src_wb = load_workbook(source_path, read_only=True, data_only=True)
    try:
        src_ws = src_wb.active
        source_songs: list[dict] = []
        for row in src_ws.iter_rows(min_row=2, values_only=True):
            if not row or not row[1]:
                continue
            source_songs.append({
                "title": str(row[1] or ""),
                "artist": str(row[2] or "") if len(row) > 2 else "",
                "album": str(row[3] or "") if len(row) > 3 else "",
                "year": str(row[4] or "") if len(row) > 4 else "",
                "genre": str(row[5] or "") if len(row) > 5 else "",
            })
    finally:
        src_wb.close()

    tgt_wb = load_workbook(target_path)
    try:
        tgt_ws = tgt_wb.active
        existing = set()
        for row in tgt_ws.iter_rows(min_row=2, values_only=False):
            t = row[1].value or ""
            a = row[2].value or ""
            if t:
                existing.add((normalize(str(t)), normalize(str(a))))

        new_songs = [
            s for s in source_songs
            if (normalize(s["title"]), normalize(s["artist"])) not in existing
        ]
        if not new_songs:
            logger.info("Merge: all %d source songs already in target",
                        len(source_songs))
            return {
                "added": 0,
                "source_total": len(source_songs),
                "target_total": len(existing),
                "path": target_path,
            }

        next_row = tgt_ws.max_row + 1
        for i, s in enumerate(new_songs):
            r = next_row + i
            tgt_ws.cell(row=r, column=1, value=r - 1)
            tgt_ws.cell(row=r, column=2, value=s["title"])
            tgt_ws.cell(row=r, column=3, value=s["artist"])
            tgt_ws.cell(row=r, column=4, value=s["album"])
            tgt_ws.cell(row=r, column=5, value=s["year"])
            tgt_ws.cell(row=r, column=6, value=s["genre"])
        # Renumber column 1.
        for i, row in enumerate(tgt_ws.iter_rows(min_row=2, max_col=1), start=1):
            row[0].value = i
        tgt_wb.save(target_path)
    finally:
        tgt_wb.close()

    total = len(existing) + len(new_songs)
    logger.info("Merge: added %d songs to %s (total %d)",
                len(new_songs), os.path.basename(target_path), total)
    return {
        "added": len(new_songs),
        "source_total": len(source_songs),
        "target_total": total,
        "path": target_path,
    }


def _run_merge_to_new_task(
    target_path: str, source_path: str, new_path: str
) -> dict:
    """Copy *target_path* to *new_path* and merge *source_path* into the copy.

    The result is a standalone Local-tagged spreadsheet — source_url is
    cleared so an Update action won't pull from the original target's
    Amazon Music playlist.
    """
    import shutil
    from spreadsheet_tags import set_tag, TAG_LOCAL

    if os.path.exists(new_path):
        raise RuntimeError(f"{os.path.basename(new_path)} already exists")
    os.makedirs(os.path.dirname(new_path) or ".", exist_ok=True)
    shutil.copy2(target_path, new_path)
    # Retag the copy as Local and clear any inherited source fields.
    set_tag(new_path, TAG_LOCAL)
    wb = load_workbook(new_path)
    try:
        if TAG_SHEET in wb.sheetnames:
            ws = wb[TAG_SHEET]
            ws["C2"] = ""
            ws["D2"] = ""
            wb.save(new_path)
    finally:
        wb.close()
    result = _run_merge_task(new_path, source_path)
    # Override the target_total label name: the result's "path" already
    # points to new_path since we merged into the copy.
    result["new_file"] = True
    return result


# ---------------------------------------------------------------------------
# Duplicates: scan task + review dialog.
# ---------------------------------------------------------------------------
def _run_duplicates_scan_task(xlsx_path: str, threshold: float = 0.85) -> dict:
    """Find candidate duplicate groups in *xlsx_path* using fuzzy matching."""
    from duplicates import find_duplicates
    groups = find_duplicates(xlsx_path, threshold=threshold)
    logger.info("Duplicates: %d candidate group(s) in %s (threshold %.2f)",
                len(groups), os.path.basename(xlsx_path), threshold)
    return {"path": xlsx_path, "groups": groups, "threshold": threshold}


class DuplicatesDialog(QDialog):
    """Review dialog for duplicate candidate groups.

    Each group gets an action selector (Skip / Dismiss / Delete) and per-song
    checkboxes. Apply writes row deletions to the xlsx and persists dismissed
    group keys to `./output/dismissed_duplicates.json` so they don't reappear.
    """

    def __init__(
        self,
        parent: QWidget | None,
        xlsx_path: str,
        groups: list[list[dict]],
        threshold: float,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Duplicates")
        self.setMinimumSize(720, 600)

        from duplicates import _load_dismissed, _group_key
        self._xlsx_path = xlsx_path
        self._threshold = threshold
        self._dismissed = _load_dismissed()
        self._result_summary: dict = {"deleted": 0, "dismissed": 0}

        # Filter out already-dismissed groups so we only show fresh candidates.
        self._filtered: list[tuple[str, list[dict]]] = []
        dismissed_seen = 0
        for g in groups:
            key = _group_key([(s["title"], s["artist"]) for s in g])
            if key in self._dismissed:
                dismissed_seen += 1
            else:
                self._filtered.append((key, g))
        self._group_widgets: list[dict] = []

        layout = QVBoxLayout(self)

        new_count = len(self._filtered)
        total = len(groups)
        header = QLabel(
            f"Found <b>{new_count}</b> candidate group"
            f"{'s' if new_count != 1 else ''}"
            + (f" ({dismissed_seen} previously dismissed)"
               if dismissed_seen else "")
            + f" · threshold {threshold:.2f} · {os.path.basename(xlsx_path)}"
        )
        header.setTextFormat(Qt.TextFormat.RichText)
        header.setWordWrap(True)
        layout.addWidget(header)

        top = QHBoxLayout()
        self._reset_btn = QPushButton(
            f"Reset {len(self._dismissed)} dismissed…"
        )
        self._reset_btn.setEnabled(len(self._dismissed) > 0)
        self._reset_btn.clicked.connect(self._on_reset_dismissed)
        top.addWidget(self._reset_btn)
        top.addStretch(1)
        layout.addLayout(top)

        if new_count == 0:
            msg = QLabel(
                "No new duplicate candidates."
                + (" Clear the dismissed list above to see previously-dismissed groups."
                   if dismissed_seen else "")
            )
            msg.setWordWrap(True)
            layout.addWidget(msg)
        else:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            container = QWidget()
            container_layout = QVBoxLayout(container)
            container_layout.setContentsMargins(0, 0, 0, 0)
            for i, (key, group) in enumerate(self._filtered):
                container_layout.addWidget(
                    self._build_group_widget(i + 1, new_count, key, group)
                )
            container_layout.addStretch(1)
            scroll.setWidget(container)
            layout.addWidget(scroll, 1)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Apply
            | QDialogButtonBox.StandardButton.Close
        )
        apply_btn = btns.button(QDialogButtonBox.StandardButton.Apply)
        close_btn = btns.button(QDialogButtonBox.StandardButton.Close)
        apply_btn.clicked.connect(self._on_apply)
        close_btn.clicked.connect(self.reject)
        if new_count == 0:
            apply_btn.setEnabled(False)
        layout.addWidget(btns)

    def _build_group_widget(
        self, idx: int, total: int, key: str, group: list[dict]
    ) -> QGroupBox:
        gbox = QGroupBox(f"Group {idx} of {total}")
        v = QVBoxLayout(gbox)

        action_row = QHBoxLayout()
        action_row.addWidget(QLabel("Action:"))
        combo = QComboBox()
        combo.addItem("Skip for now (ask next time)", "skip")
        combo.addItem("Not duplicates — dismiss forever", "dismiss")
        combo.addItem("Delete checked row(s)", "delete")
        action_row.addWidget(combo, 1)
        v.addLayout(action_row)

        checkboxes: list[QCheckBox] = []
        for song in group:
            row_h = QHBoxLayout()
            cb = QCheckBox()
            cb.setToolTip(
                "Mark for deletion (only applied when action is 'Delete')"
            )
            row_h.addWidget(cb)
            info = QLabel(
                f"<b>Row {song['row']}</b> — {song['title']}<br>"
                f"<span style='color:gray'>Artist: {song['artist']}"
                f" &nbsp;·&nbsp; Album: {song['album']}</span>"
            )
            info.setTextFormat(Qt.TextFormat.RichText)
            info.setWordWrap(True)
            row_h.addWidget(info, 1)
            v.addLayout(row_h)
            checkboxes.append(cb)

        self._group_widgets.append({
            "key": key,
            "action": combo,
            "checkboxes": checkboxes,
            "songs": group,
        })
        return gbox

    @pyqtSlot()
    def _on_reset_dismissed(self) -> None:
        from duplicates import _DISMISSED_FILE
        if not self._dismissed:
            return
        resp = QMessageBox.question(
            self,
            "Reset dismissed",
            f"Forget {len(self._dismissed)} dismissed duplicate group"
            f"{'s' if len(self._dismissed) != 1 else ''}? "
            "They'll appear in future scans.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if resp != QMessageBox.StandardButton.Yes:
            return
        try:
            if os.path.exists(_DISMISSED_FILE):
                os.remove(_DISMISSED_FILE)
        except OSError as e:
            QMessageBox.warning(self, "Reset failed", str(e))
            return
        self._dismissed.clear()
        self._reset_btn.setEnabled(False)
        self._reset_btn.setText("Reset 0 dismissed")
        QMessageBox.information(
            self, "Reset",
            "Dismissed list cleared. Close this dialog and re-run Duplicates "
            "to see everything."
        )

    @pyqtSlot()
    def _on_apply(self) -> None:
        rows_to_delete: set[int] = set()
        keys_to_dismiss: set[str] = set()
        warnings: list[str] = []

        for gw in self._group_widgets:
            action = gw["action"].currentData()
            if action == "skip":
                continue
            if action == "dismiss":
                keys_to_dismiss.add(gw["key"])
                continue
            if action == "delete":
                checked_idx = [
                    i for i, cb in enumerate(gw["checkboxes"]) if cb.isChecked()
                ]
                if not checked_idx:
                    warnings.append(
                        f"'{gw['songs'][0]['title']}': Delete chosen, but "
                        "no rows checked — skipped."
                    )
                    continue
                if len(checked_idx) == len(gw["songs"]):
                    warnings.append(
                        f"'{gw['songs'][0]['title']}': can't delete ALL "
                        "rows in the group — skipped."
                    )
                    continue
                for i in checked_idx:
                    rows_to_delete.add(gw["songs"][i]["row"])
                # Dismiss after deletion so remaining rows don't re-surface.
                keys_to_dismiss.add(gw["key"])

        if warnings:
            resp = QMessageBox.warning(
                self,
                "Apply",
                "\n".join(warnings) + "\n\nContinue with remaining actions?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if resp != QMessageBox.StandardButton.Yes:
                return

        if not rows_to_delete and not keys_to_dismiss:
            QMessageBox.information(
                self, "Apply", "Nothing to apply (all groups set to Skip)."
            )
            return

        try:
            if rows_to_delete:
                wb = load_workbook(self._xlsx_path)
                try:
                    sheet_name = next(
                        (s for s in wb.sheetnames if s != TAG_SHEET),
                        wb.sheetnames[0] if wb.sheetnames else None,
                    )
                    if sheet_name is None:
                        raise RuntimeError("Workbook has no data sheet")
                    ws = wb[sheet_name]
                    for rn in sorted(rows_to_delete, reverse=True):
                        ws.delete_rows(rn)
                    for i, row in enumerate(
                        ws.iter_rows(min_row=2, max_col=1), start=1
                    ):
                        row[0].value = i
                    wb.save(self._xlsx_path)
                finally:
                    wb.close()
            if keys_to_dismiss:
                from duplicates import _save_dismissed
                self._dismissed.update(keys_to_dismiss)
                _save_dismissed(self._dismissed)
        except Exception as e:
            logger.exception("Duplicates apply failed")
            QMessageBox.critical(self, "Apply failed", str(e))
            return

        self._result_summary = {
            "deleted": len(rows_to_delete),
            "dismissed": len(keys_to_dismiss),
        }
        self.accept()

    def result_summary(self) -> dict:
        return self._result_summary


# ---------------------------------------------------------------------------
# Curate: hand-pick songs from one spreadsheet into a new or existing one.
# ---------------------------------------------------------------------------
class _NumItem(QTableWidgetItem):
    """Table item that sorts numerically when both sides parse as int."""

    def __lt__(self, other) -> bool:  # type: ignore[override]
        try:
            return int(self.text()) < int(other.text())
        except (ValueError, TypeError):
            return self.text() < other.text()


def _parse_range_spec(text: str, upper: int) -> set[int]:
    """Parse strings like '1,3,5-8' into a set of 0-based indices.

    Returns an empty set on any parse error. *upper* is exclusive, so indices
    outside [0, upper) are dropped silently.
    """
    if not text.strip():
        return set()
    out: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            try:
                a_str, b_str = part.split("-", 1)
                a, b = int(a_str.strip()), int(b_str.strip())
            except ValueError:
                return set()
            if a > b:
                a, b = b, a
            for n in range(a, b + 1):
                if 1 <= n <= upper:
                    out.add(n - 1)
        else:
            try:
                n = int(part)
            except ValueError:
                return set()
            if 1 <= n <= upper:
                out.add(n - 1)
    return out


def _read_curate_songs(path: str) -> list[dict]:
    """Load (title, artist, album, year, genre) from *path*'s data sheet."""
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        sheet_name = next(
            (s for s in wb.sheetnames if s != TAG_SHEET),
            wb.sheetnames[0] if wb.sheetnames else None,
        )
        if sheet_name is None:
            return []
        ws = wb[sheet_name]
        songs: list[dict] = []
        for row in ws.iter_rows(min_row=2, values_only=True):
            if not row or len(row) < 2:
                continue
            title = row[1]
            if not title:
                continue
            songs.append({
                "title": str(title),
                "artist": str(row[2]) if len(row) > 2 and row[2] else "",
                "album":  str(row[3]) if len(row) > 3 and row[3] else "",
                "year":   str(row[4]) if len(row) > 4 and row[4] else "",
                "genre":  str(row[5]) if len(row) > 5 and row[5] else "",
            })
        return songs
    finally:
        wb.close()


def _read_dest_keys(path: str) -> set[tuple[str, str]]:
    """Normalized (title, artist) keys already in *path*."""
    from utils import normalize
    try:
        wb = load_workbook(path, read_only=True, data_only=True)
    except Exception:
        return set()
    try:
        sheet_name = next(
            (s for s in wb.sheetnames if s != TAG_SHEET),
            wb.sheetnames[0] if wb.sheetnames else None,
        )
        if sheet_name is None:
            return set()
        ws = wb[sheet_name]
        keys: set[tuple[str, str]] = set()
        for row in ws.iter_rows(min_row=2, values_only=True):
            if not row or len(row) < 2 or not row[1]:
                continue
            t = normalize(str(row[1]))
            a = normalize(str(row[2])) if len(row) > 2 and row[2] else ""
            keys.add((t, a))
        return keys
    finally:
        wb.close()


def _run_curate_commit_task(
    source_path: str, dest_path: str, dest_is_new: bool, songs: list[dict]
) -> dict:
    """Write *songs* to *dest_path* — new file or append-with-dedupe."""
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from utils import normalize
    from spreadsheet_tags import set_tag, TAG_LOCAL

    headers = ["#", "Song Name", "Artist", "Album", "Release Year", "Genre"]

    if dest_is_new:
        if os.path.exists(dest_path):
            raise RuntimeError(
                f"{os.path.basename(dest_path)} already exists"
            )
        os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
        new_wb = Workbook()
        new_ws = new_wb.active
        new_ws.title = "Curated Playlist"[:31]
        bold = Font(bold=True)
        for col, h in enumerate(headers, 1):
            cell = new_ws.cell(row=1, column=col, value=h)
            cell.font = bold
        for i, s in enumerate(songs, 1):
            new_ws.cell(row=i + 1, column=1, value=i)
            new_ws.cell(row=i + 1, column=2, value=s["title"])
            new_ws.cell(row=i + 1, column=3, value=s["artist"])
            new_ws.cell(row=i + 1, column=4, value=s["album"])
            new_ws.cell(row=i + 1, column=5, value=s["year"])
            new_ws.cell(row=i + 1, column=6, value=s["genre"])
        new_wb.save(dest_path)
        set_tag(dest_path, TAG_LOCAL)
        written = len(songs)
        skipped = 0
    else:
        existing = _read_dest_keys(dest_path)
        to_write = [
            s for s in songs
            if (normalize(s["title"]), normalize(s["artist"])) not in existing
        ]
        skipped = len(songs) - len(to_write)
        if to_write:
            wb = load_workbook(dest_path)
            try:
                sheet_name = next(
                    (s for s in wb.sheetnames if s != TAG_SHEET),
                    wb.sheetnames[0] if wb.sheetnames else None,
                )
                if sheet_name is None:
                    raise RuntimeError("Destination has no data sheet")
                ws = wb[sheet_name]
                next_row = ws.max_row + 1
                for i, s in enumerate(to_write):
                    r = next_row + i
                    ws.cell(row=r, column=1, value=r - 1)
                    ws.cell(row=r, column=2, value=s["title"])
                    ws.cell(row=r, column=3, value=s["artist"])
                    ws.cell(row=r, column=4, value=s["album"])
                    ws.cell(row=r, column=5, value=s["year"])
                    ws.cell(row=r, column=6, value=s["genre"])
                for i, row in enumerate(
                    ws.iter_rows(min_row=2, max_col=1), start=1
                ):
                    row[0].value = i
                wb.save(dest_path)
            finally:
                wb.close()
        written = len(to_write)

    logger.info(
        "Curate: wrote %d song(s) to %s (%d skipped as already present)",
        written, os.path.basename(dest_path), skipped,
    )
    return {
        "written": written,
        "skipped": skipped,
        "path": dest_path,
        "dest_is_new": dest_is_new,
    }


class CurateDialog(QDialog):
    COL_CHECK = 0
    COL_NUM = 1
    COL_TITLE = 2
    COL_ARTIST = 3
    COL_ALBUM = 4
    COL_YEAR = 5
    COL_GENRE = 6

    def __init__(
        self, parent: QWidget | None, source_path: str
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Curate Songs")
        self.setMinimumSize(960, 680)
        self._source_path = source_path
        self._applying = False
        self._result_summary: dict = {"written": 0, "skipped": 0, "path": ""}

        try:
            self._songs = _read_curate_songs(source_path)
        except Exception as e:
            logger.exception("Could not read source songs")
            QMessageBox.critical(
                self, "Curate",
                f"Failed to read {os.path.basename(source_path)}: {e}"
            )
            self._songs = []

        layout = QVBoxLayout(self)

        header = QLabel(
            f"Source: <b>{os.path.basename(source_path)}</b> — "
            f"{len(self._songs)} song{'s' if len(self._songs) != 1 else ''}"
        )
        header.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(header)

        dest_box = QGroupBox("Destination")
        dest_layout = QFormLayout(dest_box)
        self._dest_group = QButtonGroup(self)
        self._new_radio = QRadioButton("Save to a new spreadsheet")
        self._append_radio = QRadioButton("Append to an existing spreadsheet")
        self._dest_group.addButton(self._new_radio)
        self._dest_group.addButton(self._append_radio)
        self._new_radio.setChecked(True)
        self._new_name = QLineEdit()
        self._new_name.setPlaceholderText("curated_mix")
        self._append_combo = QComboBox()
        _populate_spreadsheet_combo(
            self._append_combo, exclude_path=source_path
        )
        self._append_combo.setEnabled(False)
        self._new_radio.toggled.connect(self._on_dest_mode_changed)
        self._append_combo.currentIndexChanged.connect(
            self._on_dest_target_changed
        )
        dest_layout.addRow(self._new_radio, self._new_name)
        dest_layout.addRow(self._append_radio, self._append_combo)
        layout.addWidget(dest_box)

        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("Range:"))
        self._range_input = QLineEdit()
        self._range_input.setPlaceholderText("e.g. 1,3,5-8")
        self._range_input.setToolTip(
            "Type numbers/ranges and click Toggle (or press Enter) to flip "
            "their check state."
        )
        self._range_input.returnPressed.connect(self._on_range_toggle)
        toolbar.addWidget(self._range_input, 1)
        toolbar.addWidget(self._make_btn("Toggle", self._on_range_toggle))
        toolbar.addSpacing(12)
        toolbar.addWidget(self._make_btn("Select all", self._on_select_all))
        toolbar.addWidget(self._make_btn("Clear", self._on_clear_all))
        toolbar.addWidget(self._make_btn("Invert", self._on_invert))
        layout.addLayout(toolbar)

        self._table = QTableWidget(len(self._songs), 7)
        self._table.setHorizontalHeaderLabels(
            ["", "#", "Title", "Artist", "Album", "Year", "Genre"]
        )
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._table.setSortingEnabled(False)
        self._table.verticalHeader().setVisible(False)
        h = self._table.horizontalHeader()
        h.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        default_widths = [28, 50, 280, 200, 180, 60, 120]
        for c, w in enumerate(default_widths):
            self._table.setColumnWidth(c, w)
        h.setSectionResizeMode(self.COL_TITLE, QHeaderView.ResizeMode.Stretch)
        self._table.itemChanged.connect(self._on_item_changed)
        # Clicking anywhere in a row toggles its checkbox (more forgiving UX).
        self._table.cellClicked.connect(self._on_cell_clicked)
        self._populate_table()
        self._table.setSortingEnabled(True)
        layout.addWidget(self._table, 1)

        self._status_label = QLabel("")
        layout.addWidget(self._status_label)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel
        )
        self._commit_btn = QPushButton("Write selected")
        self._commit_btn.setDefault(True)
        self._commit_btn.clicked.connect(self._on_commit)
        btns.addButton(self._commit_btn, QDialogButtonBox.ButtonRole.AcceptRole)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

        self._update_status()

    def _make_btn(self, text: str, slot) -> QPushButton:
        b = QPushButton(text)
        b.clicked.connect(slot)
        return b

    def _populate_table(self) -> None:
        self._table.blockSignals(True)
        try:
            for i, s in enumerate(self._songs):
                check = QTableWidgetItem()
                check.setFlags(
                    Qt.ItemFlag.ItemIsUserCheckable
                    | Qt.ItemFlag.ItemIsEnabled
                )
                check.setCheckState(Qt.CheckState.Unchecked)
                # Stash the song index so lookups survive header sort.
                check.setData(Qt.ItemDataRole.UserRole, i)
                self._table.setItem(i, self.COL_CHECK, check)
                self._table.setItem(i, self.COL_NUM, _NumItem(str(i + 1)))
                self._table.setItem(i, self.COL_TITLE, QTableWidgetItem(s["title"]))
                self._table.setItem(i, self.COL_ARTIST, QTableWidgetItem(s["artist"]))
                self._table.setItem(i, self.COL_ALBUM, QTableWidgetItem(s["album"]))
                self._table.setItem(i, self.COL_YEAR, _NumItem(s["year"]))
                self._table.setItem(i, self.COL_GENRE, QTableWidgetItem(s["genre"]))
        finally:
            self._table.blockSignals(False)

    def _song_index_for_row(self, row: int) -> int | None:
        item = self._table.item(row, self.COL_CHECK)
        if item is None:
            return None
        si = item.data(Qt.ItemDataRole.UserRole)
        return si if isinstance(si, int) else None

    def _row_for_song_index(self, song_index: int) -> int | None:
        for r in range(self._table.rowCount()):
            if self._song_index_for_row(r) == song_index:
                return r
        return None

    # --- Destination -------------------------------------------------------
    @pyqtSlot(bool)
    def _on_dest_mode_changed(self, _checked: bool) -> None:
        new_mode = self._new_radio.isChecked()
        self._new_name.setEnabled(new_mode)
        self._append_combo.setEnabled(not new_mode)
        self._apply_dest_dedupe_styling()
        self._update_status()

    @pyqtSlot(int)
    def _on_dest_target_changed(self, _idx: int) -> None:
        self._apply_dest_dedupe_styling()
        self._update_status()

    def _current_dest_keys(self) -> set[tuple[str, str]]:
        if self._append_radio.isChecked():
            dest = self._append_combo.currentData() or ""
            if dest and os.path.exists(dest):
                try:
                    return _read_dest_keys(dest)
                except Exception:
                    return set()
        return set()

    def _apply_dest_dedupe_styling(self) -> None:
        """Grey rows that are already present in the currently-picked dest."""
        from utils import normalize
        keys = self._current_dest_keys()
        self._table.blockSignals(True)
        try:
            for r in range(self._table.rowCount()):
                si = self._song_index_for_row(r)
                if si is None:
                    continue
                s = self._songs[si]
                k = (normalize(s["title"]), normalize(s["artist"]))
                dup = k in keys
                tip = "Already in destination — will be skipped" if dup else ""
                for c in range(self._table.columnCount()):
                    cell = self._table.item(r, c)
                    if cell is None:
                        continue
                    if dup:
                        cell.setForeground(Qt.GlobalColor.gray)
                        cell.setToolTip(tip)
                    else:
                        # Clear the override so rows use the theme's default
                        # text color instead of a forced black.
                        cell.setData(Qt.ItemDataRole.ForegroundRole, None)
                        cell.setToolTip("")
        finally:
            self._table.blockSignals(False)

    # --- Selection ops -----------------------------------------------------
    def _set_check_range(self, indices, state: Qt.CheckState) -> None:
        self._table.blockSignals(True)
        try:
            for i in indices:
                item = self._table.item(i, self.COL_CHECK)
                if item is not None:
                    item.setCheckState(state)
        finally:
            self._table.blockSignals(False)
        self._update_status()

    @pyqtSlot()
    def _on_select_all(self) -> None:
        self._set_check_range(range(self._table.rowCount()), Qt.CheckState.Checked)

    @pyqtSlot()
    def _on_clear_all(self) -> None:
        self._set_check_range(range(self._table.rowCount()), Qt.CheckState.Unchecked)

    @pyqtSlot()
    def _on_invert(self) -> None:
        self._table.blockSignals(True)
        try:
            for i in range(self._table.rowCount()):
                item = self._table.item(i, self.COL_CHECK)
                if item is None:
                    continue
                item.setCheckState(
                    Qt.CheckState.Unchecked
                    if item.checkState() == Qt.CheckState.Checked
                    else Qt.CheckState.Checked
                )
        finally:
            self._table.blockSignals(False)
        self._update_status()

    @pyqtSlot()
    def _on_range_toggle(self) -> None:
        indices = _parse_range_spec(self._range_input.text(), len(self._songs))
        if not indices:
            QMessageBox.information(
                self, "Range",
                "Couldn't parse that range. Try something like '1,3,5-8'."
            )
            return
        # `indices` are 0-based song indices (matching the # column). Map them
        # to current rows so range selection still works after header sort.
        si_to_row: dict[int, int] = {}
        for r in range(self._table.rowCount()):
            si = self._song_index_for_row(r)
            if si is not None:
                si_to_row[si] = r
        self._table.blockSignals(True)
        try:
            for si in indices:
                r = si_to_row.get(si)
                if r is None:
                    continue
                item = self._table.item(r, self.COL_CHECK)
                if item is None:
                    continue
                item.setCheckState(
                    Qt.CheckState.Unchecked
                    if item.checkState() == Qt.CheckState.Checked
                    else Qt.CheckState.Checked
                )
        finally:
            self._table.blockSignals(False)
        self._update_status()

    @pyqtSlot(int, int)
    def _on_cell_clicked(self, row: int, col: int) -> None:
        # A click in the checkbox column is handled natively; for other
        # columns we still toggle so the UX isn't picky about hit area.
        if col == self.COL_CHECK:
            return
        item = self._table.item(row, self.COL_CHECK)
        if item is None:
            return
        self._table.blockSignals(True)
        try:
            item.setCheckState(
                Qt.CheckState.Unchecked
                if item.checkState() == Qt.CheckState.Checked
                else Qt.CheckState.Checked
            )
        finally:
            self._table.blockSignals(False)
        self._update_status()

    @pyqtSlot(QTableWidgetItem)
    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() == self.COL_CHECK:
            self._update_status()

    # --- Status + commit ---------------------------------------------------
    def _checked_indices(self) -> list[int]:
        """Return song indices (not row indices) of checked rows."""
        out: list[int] = []
        for r in range(self._table.rowCount()):
            item = self._table.item(r, self.COL_CHECK)
            if item is None or item.checkState() != Qt.CheckState.Checked:
                continue
            si = self._song_index_for_row(r)
            if si is not None:
                out.append(si)
        return out

    def _update_status(self) -> None:
        from utils import normalize
        checked = self._checked_indices()
        selected_songs = [self._songs[i] for i in checked]
        # Dedup within selection by normalized key (first-wins).
        seen: set[tuple[str, str]] = set()
        deduped: list[dict] = []
        for s in selected_songs:
            k = (normalize(s["title"]), normalize(s["artist"]))
            if k in seen:
                continue
            seen.add(k)
            deduped.append(s)
        in_dest = self._current_dest_keys()
        will_write = [
            s for s in deduped
            if (normalize(s["title"]), normalize(s["artist"])) not in in_dest
        ]
        skipped_by_dest = len(deduped) - len(will_write)
        parts = [f"Selected: {len(selected_songs)}"]
        if len(deduped) != len(selected_songs):
            parts.append(
                f"deduped to {len(deduped)}"
            )
        if self._append_radio.isChecked():
            parts.append(
                f"will write {len(will_write)}"
                + (f" ({skipped_by_dest} already in dest)"
                   if skipped_by_dest else "")
            )
        else:
            parts.append(f"will write {len(will_write)}")
        self._status_label.setText(" · ".join(parts))
        self._commit_btn.setText(
            f"Write {len(will_write)} song{'s' if len(will_write) != 1 else ''}"
        )
        self._commit_btn.setEnabled(len(will_write) > 0 and not self._applying)

    def _resolve_dest(self) -> tuple[str, bool] | None:
        """Return (path, is_new) or None with a warning dialog on bad input."""
        if self._new_radio.isChecked():
            raw = self._new_name.text().strip()
            if not raw:
                QMessageBox.warning(
                    self, "Destination", "Enter a filename for the new spreadsheet."
                )
                return None
            if not raw.lower().endswith(".xlsx"):
                raw += ".xlsx"
            if os.path.dirname(raw):
                path = raw
            else:
                path = os.path.join(OUTPUT_DIR, raw)
            if os.path.exists(path):
                QMessageBox.warning(
                    self, "Destination",
                    f"'{os.path.basename(path)}' already exists. Pick a different name."
                )
                return None
            return path, True
        else:
            dest = self._append_combo.currentData() or ""
            if not dest:
                QMessageBox.warning(
                    self, "Destination",
                    "Pick an existing spreadsheet to append to."
                )
                return None
            if os.path.abspath(dest) == os.path.abspath(self._source_path):
                QMessageBox.warning(
                    self, "Destination",
                    "Source and destination can't be the same file."
                )
                return None
            return dest, False

    @pyqtSlot()
    def _on_commit(self) -> None:
        from utils import normalize
        resolved = self._resolve_dest()
        if resolved is None:
            return
        dest_path, dest_is_new = resolved

        checked = self._checked_indices()
        # Dedup within selection preserving first-occurrence order.
        seen: set[tuple[str, str]] = set()
        selected: list[dict] = []
        for i in checked:
            s = self._songs[i]
            k = (normalize(s["title"]), normalize(s["artist"]))
            if k in seen:
                continue
            seen.add(k)
            selected.append(s)

        if not selected:
            QMessageBox.information(
                self, "Nothing selected", "Check at least one song to write."
            )
            return

        self._applying = True
        self._commit_btn.setEnabled(False)
        try:
            result = _run_curate_commit_task(
                self._source_path, dest_path, dest_is_new, selected
            )
        except Exception as e:
            logger.exception("Curate commit failed")
            QMessageBox.critical(self, "Curate failed", str(e))
            self._applying = False
            self._update_status()
            return
        self._result_summary = result
        self.accept()

    def result_summary(self) -> dict:
        return self._result_summary


# ---------------------------------------------------------------------------
# Create Playlist: dialog + progress UI + match-picker + worker coroutine.
# ---------------------------------------------------------------------------
class CreatePlaylistDialog(QDialog):
    """Configure a playlist-creation run from an existing spreadsheet."""

    def __init__(self, parent: QWidget | None, source_path: str,
                 song_count: int) -> None:
        super().__init__(parent)
        self.setWindowTitle("Create Amazon Music Playlist")
        self.setMinimumWidth(520)
        self._source_path = source_path

        default_name = os.path.splitext(os.path.basename(source_path))[0]

        header = QLabel(
            f"Source: <b>{os.path.basename(source_path)}</b> — "
            f"{song_count} song{'s' if song_count != 1 else ''}"
        )
        header.setTextFormat(Qt.TextFormat.RichText)

        self._name = QLineEdit(default_name)
        self._name.setPlaceholderText("Playlist name on Amazon Music")

        self._threshold = QSpinBox()
        self._threshold.setRange(50, 100)
        self._threshold.setValue(90)
        self._threshold.setSuffix("%")
        self._threshold.setToolTip(
            "Songs whose best search match scores at or above this threshold "
            "are auto-added; lower-scored matches will prompt you to pick."
        )

        form = QFormLayout()
        form.addRow("Playlist name:", self._name)
        form.addRow("Auto-pick threshold:", self._threshold)

        note = QLabel(
            "<i>Chrome must be connected and you must be logged in to "
            "Amazon Music. Each song triggers a search; expect ~5–15s per "
            "song.</i>"
        )
        note.setTextFormat(Qt.TextFormat.RichText)
        note.setWordWrap(True)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(header)
        layout.addLayout(form)
        layout.addWidget(note)
        layout.addWidget(btns)

    def values(self) -> dict:
        return {
            "name": self._name.text().strip(),
            "threshold": int(self._threshold.value()),
        }


class MatchPickerDialog(QDialog):
    """Prompt the user to pick from medium-confidence search candidates."""

    def __init__(self, parent: QWidget | None, desired_title: str,
                 desired_artist: str, candidates: list[dict]) -> None:
        super().__init__(parent)
        self.setWindowTitle("Pick a match")
        self.setMinimumWidth(560)
        self._chosen: int | None = None

        header = QLabel(
            f"No clear match for <b>{desired_title}</b> by "
            f"<b>{desired_artist}</b>. Pick one of the search results, "
            "or skip this song."
        )
        header.setTextFormat(Qt.TextFormat.RichText)
        header.setWordWrap(True)

        self._list = QListWidget()
        for c in candidates:
            label = (
                f"{c['title']} — {c['artist']}"
                + (f" ({c['album']})" if c.get("album") else "")
                + f"  [score {c['score']}]"
            )
            self._list.addItem(QListWidgetItem(label))
        if candidates:
            self._list.setCurrentRow(0)
        self._list.itemDoubleClicked.connect(lambda _i: self._accept_pick())

        btns = QDialogButtonBox()
        pick_btn = QPushButton("Pick")
        pick_btn.setDefault(True)
        pick_btn.clicked.connect(self._accept_pick)
        skip_btn = QPushButton("Skip song")
        skip_btn.clicked.connect(self.reject)
        btns.addButton(pick_btn, QDialogButtonBox.ButtonRole.AcceptRole)
        btns.addButton(skip_btn, QDialogButtonBox.ButtonRole.RejectRole)

        layout = QVBoxLayout(self)
        layout.addWidget(header)
        layout.addWidget(self._list, 1)
        layout.addWidget(btns)

    @pyqtSlot()
    def _accept_pick(self) -> None:
        row = self._list.currentRow()
        self._chosen = row if row >= 0 else None
        self.accept()

    def selected_index(self) -> int | None:
        return self._chosen


class CreatePlaylistProgressDialog(QDialog):
    """Live progress + recent decisions + cancel for a create-playlist run."""

    cancel_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None, playlist_name: str,
                 total: int) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Creating playlist '{playlist_name}'")
        self.setMinimumSize(640, 420)
        # Modal so the user can't kick off other browser actions concurrently.
        self.setModal(True)

        self._header = QLabel(
            f"Adding songs to <b>{playlist_name}</b>…"
        )
        self._header.setTextFormat(Qt.TextFormat.RichText)

        self._current = QLabel("Starting…")
        self._current.setWordWrap(True)

        self._bar = QProgressBar()
        self._bar.setRange(0, max(total, 1))
        self._bar.setValue(0)

        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumBlockCount(2000)

        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.clicked.connect(self._on_cancel_clicked)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        btn_row.addWidget(self._cancel_btn)

        layout = QVBoxLayout(self)
        layout.addWidget(self._header)
        layout.addWidget(self._current)
        layout.addWidget(self._bar)
        layout.addWidget(self._log, 1)
        layout.addLayout(btn_row)

    @pyqtSlot()
    def _on_cancel_clicked(self) -> None:
        self._cancel_btn.setEnabled(False)
        self._cancel_btn.setText("Cancelling…")
        self.cancel_requested.emit()

    @pyqtSlot(int, int, str)
    def update_progress(self, idx: int, total: int, label: str) -> None:
        if total > 0 and self._bar.maximum() != total:
            self._bar.setRange(0, total)
        self._bar.setValue(idx)
        self._current.setText(label)

    @pyqtSlot(str)
    def append_log(self, line: str) -> None:
        self._log.appendPlainText(line)

    def closeEvent(self, e) -> None:  # type: ignore[override]
        # Treat close-button as cancel rather than a hard kill so the
        # coroutine can wind down. We then accept the close.
        if self._cancel_btn.isEnabled():
            self._on_cancel_clicked()
        super().closeEvent(e)


class CreatePlaylistController(QObject):
    """Bridges the worker-thread coroutine and the GUI-thread dialogs."""

    progress = pyqtSignal(int, int, str)   # current_idx, total, label
    log = pyqtSignal(str)                  # one decision line per song
    ask_match = pyqtSignal(object)         # match-prompt request payload
    finished = pyqtSignal(object)          # final summary dict

    def __init__(self) -> None:
        super().__init__()
        self.cancelled = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pending_future: asyncio.Future | None = None

    def request_cancel(self) -> None:
        """Called from the GUI thread to ask the run to wind down."""
        self.cancelled = True
        # If a match prompt is pending, resolve it as skip so the coroutine
        # doesn't hang waiting for user input we've already given up on.
        fut = self._pending_future
        loop = self._loop
        if fut is not None and loop is not None:
            self._pending_future = None
            loop.call_soon_threadsafe(
                lambda: fut.set_result(None) if not fut.done() else None
            )

    def resolve_match(self, idx: int | None) -> None:
        """Called from the GUI thread once the user picks/skips a candidate."""
        fut = self._pending_future
        loop = self._loop
        if fut is None or loop is None:
            return
        self._pending_future = None
        loop.call_soon_threadsafe(
            lambda: fut.set_result(idx) if not fut.done() else None
        )


class BulkDeleteProgressDialog(QDialog):
    """Live progress + recent decisions + cancel for a bulk-delete run."""

    cancel_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None, total: int) -> None:
        super().__init__(parent)
        self.setWindowTitle("Deleting playlists")
        self.setMinimumSize(640, 420)
        self.setModal(True)

        self._header = QLabel(
            f"Deleting <b>{total}</b> playlist{'s' if total != 1 else ''} "
            "from Amazon Music…"
        )
        self._header.setTextFormat(Qt.TextFormat.RichText)

        self._current = QLabel("Starting…")
        self._current.setWordWrap(True)

        self._bar = QProgressBar()
        self._bar.setRange(0, max(total, 1))
        self._bar.setValue(0)

        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumBlockCount(2000)

        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.clicked.connect(self._on_cancel_clicked)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        btn_row.addWidget(self._cancel_btn)

        layout = QVBoxLayout(self)
        layout.addWidget(self._header)
        layout.addWidget(self._current)
        layout.addWidget(self._bar)
        layout.addWidget(self._log, 1)
        layout.addLayout(btn_row)

    @pyqtSlot()
    def _on_cancel_clicked(self) -> None:
        self._cancel_btn.setEnabled(False)
        self._cancel_btn.setText("Cancelling…")
        self.cancel_requested.emit()

    @pyqtSlot(int, int, str)
    def update_progress(self, idx: int, total: int, label: str) -> None:
        if total > 0 and self._bar.maximum() != total:
            self._bar.setRange(0, total)
        self._bar.setValue(idx)
        self._current.setText(label)

    @pyqtSlot(str)
    def append_log(self, line: str) -> None:
        self._log.appendPlainText(line)

    def closeEvent(self, e) -> None:  # type: ignore[override]
        # Close-button = cancel, so the coroutine winds down cleanly.
        if self._cancel_btn.isEnabled():
            self._on_cancel_clicked()
        super().closeEvent(e)


class BulkDeleteController(QObject):
    """Bridges the worker-thread bulk-delete coroutine and the GUI dialog."""

    progress = pyqtSignal(int, int, str)   # current_idx, total, label
    log = pyqtSignal(str)                  # one decision line per item
    finished = pyqtSignal(object)          # final summary dict

    def __init__(self) -> None:
        super().__init__()
        self.cancelled = False

    def request_cancel(self) -> None:
        self.cancelled = True


class GenerateShufflePlaylistController(QObject):
    """Bridges the Step 6 generator coroutine and its progress dialog.

    Distinct from ``CreatePlaylistController`` — ASIN-direct adds have no
    ambiguous-match prompt, so this controller carries only the three
    one-way signals the dialog needs plus the cancel flag.
    """

    progress = pyqtSignal(int, int, str)   # current_idx, total, label
    log = pyqtSignal(str)                  # one decision line per track
    finished = pyqtSignal(object)          # final summary dict

    def __init__(self) -> None:
        super().__init__()
        self.cancelled = False

    def request_cancel(self) -> None:
        self.cancelled = True


class GenerateShufflePlaylistProgressDialog(QDialog):
    """Live progress + log + cancel for the Classical Shuffle generator.

    Mirrors ``BulkDeleteProgressDialog`` — modal, progress bar capped at
    *total* tracks (re-rangeable so the coroutine can update the count
    once sampling completes), 2000-line scrolling log, close-button =
    cancel.
    """

    cancel_requested = pyqtSignal()

    def __init__(
        self, parent: QWidget | None, playlist_name: str, total: int,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Generating playlist '{playlist_name}'")
        self.setMinimumSize(640, 420)
        self.setModal(True)

        self._header = QLabel(
            f"Building <b>{playlist_name}</b>…"
        )
        self._header.setTextFormat(Qt.TextFormat.RichText)

        self._current = QLabel("Sampling performances…")
        self._current.setWordWrap(True)

        self._bar = QProgressBar()
        self._bar.setRange(0, max(total, 1))
        self._bar.setValue(0)

        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumBlockCount(2000)

        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.clicked.connect(self._on_cancel_clicked)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        btn_row.addWidget(self._cancel_btn)

        layout = QVBoxLayout(self)
        layout.addWidget(self._header)
        layout.addWidget(self._current)
        layout.addWidget(self._bar)
        layout.addWidget(self._log, 1)
        layout.addLayout(btn_row)

    @pyqtSlot()
    def _on_cancel_clicked(self) -> None:
        self._cancel_btn.setEnabled(False)
        self._cancel_btn.setText("Cancelling…")
        self.cancel_requested.emit()

    @pyqtSlot(int, int, str)
    def update_progress(self, idx: int, total: int, label: str) -> None:
        if total > 0 and self._bar.maximum() != total:
            self._bar.setRange(0, total)
        self._bar.setValue(idx)
        self._current.setText(label)

    @pyqtSlot(str)
    def append_log(self, line: str) -> None:
        self._log.appendPlainText(line)

    def closeEvent(self, e) -> None:  # type: ignore[override]
        if self._cancel_btn.isEnabled():
            self._on_cancel_clicked()
        super().closeEvent(e)


def _build_bulk_delete_coroutine(
    items: list[dict], controller: BulkDeleteController,
):
    """Return ``async fn(page)`` that deletes each playlist in sequence.

    Per-item failures are caught and logged — the loop never aborts mid-sweep
    for a single bad playlist. Cancel is checked between items; the in-flight
    item runs to completion to leave the browser in a clean state.
    """
    async def _do_bulk_delete(page):
        from scraper import delete_playlist as _delete

        total = len(items)
        deleted: list[dict] = []
        failed: list[dict] = []

        for idx, it in enumerate(items, 1):
            if controller.cancelled:
                controller.log.emit("Cancelled by user.")
                break
            url = (it or {}).get("url") or ""
            name = (it or {}).get("name") or url
            label = f"[{idx}/{total}] {name}"
            controller.progress.emit(idx, total, label)
            controller.log.emit(f"{label} — deleting…")

            if not url:
                failed.append({"url": "", "name": name, "err": "missing URL"})
                controller.log.emit("  ✗ missing URL")
                continue

            try:
                await _delete(page, url)
            except Exception as e:
                logger.exception("Bulk delete: failed to delete %s", url)
                err = str(e) or e.__class__.__name__
                failed.append({"url": url, "name": name, "err": err})
                # Truncate very long error strings in the log line.
                short = err if len(err) <= 200 else err[:197] + "…"
                controller.log.emit(f"  ✗ {short}")
                continue

            deleted.append({"url": url, "name": name})
            controller.log.emit("  ✓ deleted")

        summary = {
            "deleted": deleted,
            "failed": failed,
            "cancelled": controller.cancelled,
            "total": total,
        }
        controller.finished.emit(summary)
        return summary

    return _do_bulk_delete


async def _gui_create_empty_playlist(page, name: str) -> None:
    """GUI variant of ``importer._create_empty_playlist`` that raises on
    failure instead of blocking on ``input()``. Mirrors the working DOM dance.
    """
    from config import BASE_URL, LONG_TIMEOUT

    logger.info("Creating new playlist '%s'", name)
    await page.goto(
        f"{BASE_URL}/my/playlists/all",
        wait_until="domcontentloaded",
        timeout=LONG_TIMEOUT,
    )
    await page.wait_for_timeout(3000)

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
        raise RuntimeError(
            "Could not find the 'Create Playlist' button on the playlists "
            "page. Make sure you're signed in and try again."
        )
    await page.wait_for_timeout(3000)

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
        raise RuntimeError(
            "Could not find the 'Curate a playlist' option in the create-"
            "playlist dialog."
        )

    box = await curate_handle.bounding_box()
    if not box:
        raise RuntimeError(
            "Could not locate the 'Curate a playlist' element on screen."
        )
    await page.mouse.click(
        box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    )
    await page.wait_for_timeout(3000)

    name_input = await page.query_selector('input[placeholder*="Playlist" i]')
    if not name_input:
        name_input = await page.query_selector('[role="dialog"] input[type="text"]')
    if name_input:
        await name_input.fill("")
        await name_input.type(name, delay=30)
        await page.wait_for_timeout(500)
    else:
        await page.keyboard.type(name, delay=30)

    save_btn = await page.query_selector(
        '[role="dialog"] music-button:has-text("Save")'
    )
    if not save_btn:
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
            await page.mouse.click(
                box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
            )
        else:
            await save_btn.click()
    else:
        await page.keyboard.press("Enter")

    import re as _re
    try:
        await page.wait_for_url(
            _re.compile(r".*/my/playlists/[0-9a-f]{8}-"),
            timeout=8000,
        )
    except Exception:
        await page.wait_for_timeout(3000)
    logger.info("Playlist '%s' created", name)
    url = page.url
    if not _re.search(r"/my/playlists/[0-9a-f]{8}-", url):
        logger.warning("Playlist created but could not determine its URL (page is at %s)", url)
        url = ""
    return url


def _build_create_coroutine(
    songs: list[dict],
    playlist_name: str,
    threshold: int,
    controller: CreatePlaylistController,
):
    """Return ``async fn(page)`` that creates the playlist and adds songs.

    Mirrors ``importer.create_playlist_from_songs`` + ``search.hybrid_select``
    but routes progress through *controller* signals instead of stdout, and
    uses a Future-based callback for ambiguous matches so the GUI thread can
    drive a picker dialog without blocking the coroutine.
    """
    async def _do_create(page):
        from auth import _detect_logged_in, _suppress_dialogs
        from config import BASE_URL, LONG_TIMEOUT
        from importer import _add_song_to_playlist
        from search import score_match, search_songs

        controller._loop = asyncio.get_running_loop()
        total = len(songs)

        controller.progress.emit(0, total, "Checking Amazon Music login…")
        await page.goto(
            BASE_URL, wait_until="domcontentloaded", timeout=LONG_TIMEOUT
        )
        await page.wait_for_timeout(2000)
        if not await _detect_logged_in(page):
            raise RuntimeError(
                "Not logged in to Amazon Music. Log in via the Chrome window, "
                "then try Create Playlist again."
            )
        await _suppress_dialogs(page)

        controller.progress.emit(
            0, total, f"Creating playlist '{playlist_name}'…"
        )
        playlist_url = await _gui_create_empty_playlist(page, playlist_name)
        controller.log.emit(f"Created playlist '{playlist_name}'.")
        try:
            import playlist_cache as _pc
            cached = _pc.load_cached_playlists() or []
            if playlist_url and not any(p.get("url") == playlist_url for p in cached):
                cached.append({"name": playlist_name, "url": playlist_url})
                _pc.save_cached_playlists(cached)
        except Exception:
            logger.exception("Could not update playlist cache after creation")

        async def ask_user(t: str, a: str, cands: list) -> int | None:
            loop = asyncio.get_running_loop()
            fut: asyncio.Future = loop.create_future()
            controller._pending_future = fut
            payload = {
                "title": t,
                "artist": a,
                "candidates": [
                    {
                        "title": c.title,
                        "artist": c.artist,
                        "album": c.album,
                        "score": c.score,
                    }
                    for c in cands
                ],
            }
            controller.ask_match.emit(payload)
            return await fut

        added = 0
        skipped = 0
        skipped_list: list[tuple[str, str]] = []

        generic_artists = {
            "various artists", "various", "soundtrack",
            "soundtrack/cast album", "original soundtrack", "ost", "cast",
        }

        for idx, song in enumerate(songs, 1):
            if controller.cancelled:
                controller.log.emit("Cancelled by user.")
                break

            title = song["title"]
            artist = song["artist"]
            label = f"{title} — {artist}" if artist else title
            controller.progress.emit(idx, total, f"[{idx}/{total}] {label}")

            artist_clean = (artist or "").strip()
            if artist_clean.lower() in generic_artists:
                query = title
            else:
                query = f"{title} {artist}".strip()

            try:
                cands = await search_songs(page, query)
            except Exception as e:
                logger.exception("Search failed for %s", label)
                controller.log.emit(f"  Search error for {label}: {e}")
                skipped += 1
                skipped_list.append((title, artist))
                continue

            if not cands:
                controller.log.emit(f"  No results for {label}.")
                skipped += 1
                skipped_list.append((title, artist))
                continue

            for c in cands:
                c.score = score_match(title, artist, c)
            cands.sort(key=lambda c: c.score, reverse=True)
            best = cands[0]

            if best.score >= threshold:
                chosen = 0
                controller.log.emit(
                    f"  Auto-picked: {best.title} — {best.artist} "
                    f"(score {best.score})"
                )
            else:
                if controller.cancelled:
                    break
                pick_idx = await ask_user(title, artist, cands[:8])
                if controller.cancelled or pick_idx is None:
                    controller.log.emit(f"  Skipped {label}.")
                    skipped += 1
                    skipped_list.append((title, artist))
                    continue
                chosen = pick_idx
                picked = cands[chosen]
                controller.log.emit(
                    f"  You picked: {picked.title} — {picked.artist} "
                    f"(score {picked.score})"
                )

            picked = cands[chosen]
            try:
                ok = await _add_song_to_playlist(page, picked, playlist_name)
            except Exception as e:
                logger.exception("Add song failed for %s", label)
                controller.log.emit(f"  Add failed for {label}: {e}")
                skipped += 1
                skipped_list.append((title, artist))
                continue

            if ok:
                added += 1
                controller.log.emit(f"  Added.")
            else:
                skipped += 1
                skipped_list.append((title, artist))
                controller.log.emit(
                    f"  Could not add {label} — Amazon Music rejected the row."
                )

        summary = {
            "name": playlist_name,
            "playlist_url": playlist_url,
            "added": added,
            "skipped": skipped,
            "total": total,
            "skipped_list": skipped_list,
            "cancelled": controller.cancelled,
        }
        controller.finished.emit(summary)
        return summary

    return _do_create


def _build_classical_shuffle_generate_coroutine(
    values: dict,
    controller: GenerateShufflePlaylistController,
):
    """Return ``async fn(page)`` that builds a Classical Shuffle playlist
    on Amazon Music end-to-end.

    Step 6 of [[project_classical_shuffle_playlist_gen_plan]]. The flow:
    pool from the scope → composer filter (via a fresh
    ``_build_playlist_composer_map``) → duration-target sample → for each
    picked Performance, read its ASIN slice from the cached xlsx →
    ``_gui_create_empty_playlist`` → loop ``add_track_by_asin`` with
    per-track failure isolation → file the new playlist in the org tree.

    Per-track failures are logged and skipped (rather than aborting) so a
    single bad row doesn't waste the whole add session. Cancel is checked
    before each track and before each performance. If zero tracks land on
    Amazon Music, the org-tree filing is skipped — an empty playlist with
    no audible content isn't worth tracking.
    """
    scope_path = list(values.get("scope_path") or [])
    name = (values.get("name") or "").strip()
    target_seconds = int(values.get("target_seconds") or 0)
    composers = values.get("composers")
    if isinstance(composers, list):
        composer_filter: set[str] | None = {c for c in composers if c}
    else:
        composer_filter = None
    piece_types = values.get("piece_types")
    if isinstance(piece_types, list):
        piece_type_filter: set[str] | None = {p for p in piece_types if p}
    else:
        piece_type_filter = None
    output_folder_path = list(values.get("output_folder_path") or [])

    async def _do_generate(page):
        from auth import _detect_logged_in, _suppress_dialogs
        from config import BASE_URL, LONG_TIMEOUT
        from importer import add_track_by_asin

        summary: dict = {
            "name": name,
            "playlist_url": "",
            "added": 0,
            "failed": 0,
            "total_tracks": 0,
            "performances": 0,
            "cancelled": False,
            "filed": False,
        }

        # 1. Build the pool (off-thread file I/O, but cheap relative to
        # the browser flow that follows). Load org once here too — we'll
        # reuse it for the composer-tag map and the post-create filing.
        try:
            org = load_org()
        except Exception as e:
            logger.exception("Generator: could not load org")
            raise RuntimeError(f"Could not load organization.json: {e}")

        controller.progress.emit(0, 1, "Building Classical performance pool…")
        pool, skipped, restricted = _build_classical_performance_pool(
            org, scope_path
        )
        if skipped:
            controller.log.emit(
                f"Skipped {len(skipped)} playlist"
                f"{'s' if len(skipped) != 1 else ''} during pool build."
            )
        if restricted:
            controller.log.emit(
                f"Filtered {restricted} known web-restricted "
                f"performance{'s' if restricted != 1 else ''}."
            )
        if not pool:
            raise RuntimeError(
                "No exportable performances found in this scope. Make "
                "sure the playlists have been exported (with ASIN + "
                "duration columns) before generating."
            )

        # 2. Composer filter via per-playlist tag map. Single-composer
        # scopes pass composers=None from the dialog, meaning "no extra
        # filter — the scope itself is the filter." When a list is
        # provided, keep only perfs whose playlist's composer tags
        # intersect the selection.
        if composer_filter is not None:
            tag_map = _build_playlist_composer_map(org)
            before = len(pool)
            pool = [
                p for p in pool
                if tag_map.get(p.playlist_url, set()) & composer_filter
            ]
            if not pool:
                raise RuntimeError(
                    f"No performances match the selected composer"
                    f"{'s' if len(composer_filter) != 1 else ''} "
                    f"({before} were in the pool before filtering)."
                )

        # 2b. Piece-type filter via per-playlist tag map. Mirrors the
        # composer filter one level deeper in the Classical hierarchy.
        # The two filters compose with AND — {Beethoven, Brahms} ×
        # {Symphonies, Violin Concertos} keeps only perfs that hit both
        # axes.
        if piece_type_filter is not None:
            type_map = _build_playlist_piece_type_map(org)
            before = len(pool)
            pool = [
                p for p in pool
                if type_map.get(p.playlist_url, set()) & piece_type_filter
            ]
            if not pool:
                raise RuntimeError(
                    f"No performances match the selected piece type"
                    f"{'s' if len(piece_type_filter) != 1 else ''} "
                    f"({before} were in the pool before filtering)."
                )

        # 3. Sample to duration target.
        picked = _sample_for_duration_target(pool, target_seconds)
        if not picked:
            raise RuntimeError(
                "Sampler returned no performances. Check the duration "
                "target and the pool size."
            )
        total_secs = sum(p.duration_seconds for p in picked)
        controller.log.emit(
            f"Sampled {len(picked)} performance"
            f"{'s' if len(picked) != 1 else ''} "
            f"(~{total_secs // 60} min total)."
        )

        # 3b. Anti-cluster pass: reorder so two recordings of the same
        # work don't land adjacent (e.g. Stockholm Beethoven 8 immediately
        # followed by Philadelphia Beethoven 8 — statistically possible
        # under uniform sampling when 3 recordings of one work are in the
        # pool). Keyed on the first track's piece prefix; perfs with no
        # titles map to empty string and are treated as a single bucket
        # (rare for classical scrapes, harmless if it happens).
        def _work_key(p: Performance) -> str:
            return _piece_prefix(p.titles[0]) if p.titles else ""
        before_clusters = sum(
            1 for a, b in zip(picked, picked[1:])
            if _work_key(a) and _work_key(a) == _work_key(b)
        )
        if before_clusters:
            picked = _spread_adjacent_by_key(picked, _work_key)
            after_clusters = sum(
                1 for a, b in zip(picked, picked[1:])
                if _work_key(a) and _work_key(a) == _work_key(b)
            )
            controller.log.emit(
                f"Anti-cluster: broke up {before_clusters - after_clusters} "
                f"of {before_clusters} adjacent same-work pair"
                f"{'s' if before_clusters != 1 else ''}"
                + (f" ({after_clusters} unavoidable — one work dominates)"
                   if after_clusters else "")
                + "."
            )

        # 4. Read per-track ASIN slices up front so per-perf xlsx I/O
        # doesn't interleave with browser actions. Build the URL→xlsx
        # index once. Performances whose xlsx is missing or whose ASIN
        # columns aren't populated get dropped here with a log line.
        url_index = _build_url_to_xlsx_index(OUTPUT_DIR)
        prepared: list[tuple[Performance, list[tuple[str, str]]]] = []
        for perf in picked:
            xlsx_path = url_index.get(perf.playlist_url)
            if not xlsx_path:
                controller.log.emit(
                    f"  ✗ no cached xlsx for '{perf.playlist_name}' — "
                    "skipping performance."
                )
                continue
            try:
                asin_slice = _read_song_asins(
                    xlsx_path, perf.start_row, perf.track_count,
                )
            except Exception:
                logger.exception(
                    "Generator: failed to read ASIN slice for %s",
                    xlsx_path,
                )
                controller.log.emit(
                    f"  ✗ error reading ASINs from "
                    f"'{os.path.basename(xlsx_path)}' — skipping perf."
                )
                continue
            if not asin_slice or not any(t for t, _ in asin_slice):
                controller.log.emit(
                    f"  ✗ no ASINs available for '{perf.album}' by "
                    f"{perf.artist} — skipping performance."
                )
                continue
            prepared.append((perf, asin_slice))

        total_tracks = sum(len(s) for _, s in prepared)
        summary["performances"] = len(prepared)
        summary["total_tracks"] = total_tracks
        if not prepared or total_tracks == 0:
            raise RuntimeError(
                "No tracks with ASINs available after slicing the sampled "
                "performances. Re-run the 2026-06-02 backfill?"
            )

        # 5. Login check + new empty playlist.
        controller.progress.emit(
            0, total_tracks, "Checking Amazon Music login…",
        )
        await page.goto(
            BASE_URL, wait_until="domcontentloaded", timeout=LONG_TIMEOUT,
        )
        await page.wait_for_timeout(2000)
        if not await _detect_logged_in(page):
            raise RuntimeError(
                "Not logged in to Amazon Music. Log in via the Chrome "
                "window, then try Generate again."
            )
        await _suppress_dialogs(page)

        if controller.cancelled:
            summary["cancelled"] = True
            controller.finished.emit(summary)
            return summary

        controller.progress.emit(
            0, total_tracks, f"Creating playlist '{name}'…",
        )
        playlist_url = await _gui_create_empty_playlist(page, name)
        summary["playlist_url"] = playlist_url
        controller.log.emit(f"Created playlist '{name}'.")
        try:
            import playlist_cache as _pc
            cached = _pc.load_cached_playlists() or []
            if playlist_url and not any(
                p.get("url") == playlist_url for p in cached
            ):
                cached.append({"name": name, "url": playlist_url})
                _pc.save_cached_playlists(cached)
        except Exception:
            logger.exception(
                "Generator: could not update playlist cache after creation"
            )

        # 6. Add tracks loop. Performances are already in shuffled order
        # (from the sampler); track order within a performance comes from
        # the xlsx's natural row order so movements play in sequence.
        added = 0
        failed = 0
        idx = 0
        for perf_i, (perf, asin_slice) in enumerate(prepared, 1):
            if controller.cancelled:
                controller.log.emit("Cancelled by user.")
                break
            perf_label = (
                f"[{perf_i}/{len(prepared)}] {perf.album} — {perf.artist}"
            )
            controller.log.emit(perf_label)
            for track_i, (track_asin, album_asin) in enumerate(asin_slice, 1):
                if controller.cancelled:
                    controller.log.emit("Cancelled by user.")
                    break
                idx += 1
                track_label = (
                    f"[{idx}/{total_tracks}] perf {perf_i}/{len(prepared)} "
                    f"track {track_i}/{len(asin_slice)}"
                )
                controller.progress.emit(idx, total_tracks, track_label)
                if not track_asin or not album_asin:
                    failed += 1
                    controller.log.emit(
                        f"  ✗ track {track_i}: missing ASIN "
                        f"(track={track_asin!r} album={album_asin!r})"
                    )
                    continue
                source_title = (
                    perf.titles[track_i - 1]
                    if track_i - 1 < len(perf.titles)
                    else ""
                )
                try:
                    ok = await add_track_by_asin(
                        page, album_asin, track_asin, name,
                        source_playlist_url=perf.playlist_url,
                        source_playlist_name=perf.playlist_name,
                        source_row_index=perf.start_row + track_i,
                        source_title=source_title,
                        source_artist=perf.artist,
                        source_album=perf.album,
                    )
                except Exception as e:
                    logger.exception(
                        "Generator: add_track_by_asin raised "
                        "(track=%s album=%s)", track_asin, album_asin,
                    )
                    failed += 1
                    controller.log.emit(
                        f"  ✗ track {track_i}: {e}"
                    )
                    continue
                if ok:
                    added += 1
                    controller.log.emit(f"  ✓ track {track_i} added")
                else:
                    failed += 1
                    controller.log.emit(
                        f"  ✗ track {track_i}: Amazon Music rejected "
                        f"the deep-link"
                    )
            if controller.cancelled:
                break

        summary["added"] = added
        summary["failed"] = failed
        summary["cancelled"] = controller.cancelled

        # 7. File the new playlist in the org tree — only if anything
        # actually landed on Amazon, otherwise the org entry would point
        # at an empty playlist nobody asked for. Also requires a URL we
        # successfully captured during create.
        if added > 0 and playlist_url:
            try:
                org_after = load_org()
                add_item(
                    org_after, output_folder_path,
                    {"kind": "playlist", "url": playlist_url, "name": name},
                )
                save_org(org_after)
                summary["filed"] = True
                controller.log.emit(
                    f"Filed playlist under "
                    f"{'/'.join(output_folder_path) or '/'}."
                )
            except Exception as e:
                logger.exception("Generator: could not file in org tree")
                controller.log.emit(
                    f"  ✗ org-tree filing failed: {e} "
                    f"(playlist still exists on Amazon Music)"
                )

        # End-of-run summary: lands in both amazonmusic.log and the GUI panel
        # so post-hoc log review can confirm the run finished as intended
        # (vs. silently crashed mid-add) without having to count track lines.
        status = "cancelled" if controller.cancelled else "complete"
        bits = [
            f"{added}/{total_tracks} tracks",
            f"{len(prepared)} performance"
            f"{'s' if len(prepared) != 1 else ''}",
        ]
        if failed:
            bits.append(f"{failed} failed")
        summary_line = (
            f"Classical Shuffle {status}: "
            + ", ".join(bits)
            + f" (target {target_seconds // 60}m, "
            f"sampled ~{total_secs // 60}m)"
        )
        logger.info(summary_line)
        controller.log.emit(summary_line)

        controller.finished.emit(summary)
        return summary

    return _do_generate


# ---------------------------------------------------------------------------
# Playback: Now-Playing dock + mode picker + worker coroutine.
# ---------------------------------------------------------------------------
class PlayModeDialog(QDialog):
    """Tiny picker that asks for straight or shuffle playback.

    When *multi_playlist* is True an extra checkbox lets the user shuffle the
    order of playlists in the queue while keeping tracks within each playlist
    in their original order.
    """

    def __init__(
        self,
        parent: QWidget | None,
        name: str,
        multi_playlist: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Play playlist")
        self.setMinimumWidth(360)

        header = QLabel(
            f"How would you like to play <b>{name or 'this playlist'}</b>?"
        )
        header.setTextFormat(Qt.TextFormat.RichText)
        header.setWordWrap(True)

        self._straight = QRadioButton("Straight (playlist order)")
        self._shuffle = QRadioButton("Shuffle tracks")
        self._straight.setChecked(True)

        from PyQt6.QtWidgets import QCheckBox
        self._shuffle_queue: QCheckBox | None = None
        if multi_playlist:
            self._shuffle_queue = QCheckBox("Shuffle playlist order")
            self._shuffle_queue.setToolTip(
                "Play playlists in a random order, but keep tracks within "
                "each playlist in their original sequence."
            )

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(header)
        layout.addWidget(self._straight)
        layout.addWidget(self._shuffle)
        if self._shuffle_queue is not None:
            layout.addSpacing(4)
            layout.addWidget(self._shuffle_queue)
        layout.addWidget(btns)

    def shuffle(self) -> bool:
        return self._shuffle.isChecked()

    def shuffle_queue(self) -> bool:
        return bool(self._shuffle_queue and self._shuffle_queue.isChecked())


class AddToSpreadsheetDialog(QDialog):
    """Picks an existing .xlsx in ./output/ or creates a new one, for
    appending the currently-playing song."""

    def __init__(
        self,
        parent: QWidget | None,
        track_title: str,
        track_artist: str,
        track_album: str,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add to spreadsheet")
        self.setMinimumWidth(480)

        layout = QVBoxLayout(self)

        header = QLabel(
            f"Save <b>{track_title}</b>"
            + (f" — {track_artist}" if track_artist else "")
            + (f" <span style='color:#888'>({track_album})</span>"
               if track_album else "")
            + "?"
        )
        header.setTextFormat(Qt.TextFormat.RichText)
        header.setWordWrap(True)
        layout.addWidget(header)

        # Existing vs new.
        self._existing_rb = QRadioButton("Add to existing spreadsheet:")
        self._new_rb = QRadioButton("Create new spreadsheet:")
        self._existing_rb.setChecked(True)
        self._existing_rb.toggled.connect(self._refresh_enabled)

        self._existing_combo = QComboBox()
        for entry in _scan_output_dir():
            label = entry.get("display_name") or entry["name"]
            self._existing_combo.addItem(label, entry["path"])
        if self._existing_combo.count() == 0:
            # No existing sheets — force "New".
            self._existing_rb.setEnabled(False)
            self._new_rb.setChecked(True)

        self._new_name = QLineEdit("saved_songs.xlsx")

        # Layout the picker.
        form = QFormLayout()
        form.addRow(self._existing_rb, self._existing_combo)
        form.addRow(self._new_rb, self._new_name)
        layout.addLayout(form)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

        self._refresh_enabled()

    def _refresh_enabled(self) -> None:
        self._existing_combo.setEnabled(self._existing_rb.isChecked())
        self._new_name.setEnabled(self._new_rb.isChecked())

    def values(self) -> dict:
        if self._existing_rb.isChecked() and self._existing_combo.count() > 0:
            return {
                "is_new": False,
                "path": self._existing_combo.currentData() or "",
            }
        # New.
        name = (self._new_name.text() or "").strip()
        if not name:
            name = "saved_songs.xlsx"
        if not name.lower().endswith(".xlsx"):
            name += ".xlsx"
        return {"is_new": True, "path": os.path.join(OUTPUT_DIR, name)}


class ClassicalShuffleFilterDialog(QDialog):
    """Composer + piece-type filter for the live "Classical Shuffle
    (recursive)" entry point.

    Mirrors the composer/piece-type halves of
    ``ClassicalShufflePlaylistDialog`` — same scope semantics, same
    helpers (``_composers_in_scope`` / ``_piece_types_in_scope``), same
    None-vs-list convention in ``values()`` — but with no duration /
    name / output-folder fields, since the live shuffle doesn't build a
    persistent playlist. Caller is expected to skip the dialog entirely
    when neither axis offers a choice (both pinned by scope).
    """

    def __init__(
        self,
        parent: QWidget | None,
        *,
        scope_path: list[str],
        org: dict,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Classical Shuffle")
        self.setMinimumWidth(440)

        self._scope_path: list[str] = list(scope_path)
        self._scope_composer = _composer_from_org_path(self._scope_path)
        self._scope_piece_type = _piece_type_from_org_path(self._scope_path)

        scope_label = "/".join(self._scope_path) if self._scope_path else "/"
        header = QLabel(f"Source: <b>{scope_label}</b>")
        header.setTextFormat(Qt.TextFormat.RichText)

        # --- Composer multi-select (hidden when scope is single-composer) ---
        self._composer_group = QGroupBox("Composers")
        self._composer_checks: list[QCheckBox] = []
        if self._scope_composer is None:
            composers_layout = QVBoxLayout(self._composer_group)
            composers = _composers_in_scope(org, self._scope_path)
            for c in composers:
                cb = QCheckBox(c)
                cb.setChecked(True)
                composers_layout.addWidget(cb)
                self._composer_checks.append(cb)
            sel_row = QHBoxLayout()
            sel_all = QPushButton("Select all")
            sel_all.clicked.connect(self._select_all_composers)
            sel_none = QPushButton("Select none")
            sel_none.clicked.connect(self._select_none_composers)
            sel_row.addStretch(1)
            sel_row.addWidget(sel_all)
            sel_row.addWidget(sel_none)
            composers_layout.addLayout(sel_row)
            if not composers:
                composers_layout.addWidget(QLabel(
                    "<i>(No composers found under this scope.)</i>"
                ))
        else:
            self._composer_group.hide()

        # --- Piece-type multi-select (hidden when scope is single-type) ---
        self._piece_type_group = QGroupBox("Piece types")
        self._piece_type_checks: list[QCheckBox] = []
        if self._scope_piece_type is None:
            pt_layout = QVBoxLayout(self._piece_type_group)
            piece_types = _piece_types_in_scope(org, self._scope_path)
            for pt in piece_types:
                cb = QCheckBox(pt)
                cb.setChecked(True)
                pt_layout.addWidget(cb)
                self._piece_type_checks.append(cb)
            sel_row = QHBoxLayout()
            sel_all = QPushButton("Select all")
            sel_all.clicked.connect(self._select_all_piece_types)
            sel_none = QPushButton("Select none")
            sel_none.clicked.connect(self._select_none_piece_types)
            sel_row.addStretch(1)
            sel_row.addWidget(sel_all)
            sel_row.addWidget(sel_none)
            pt_layout.addLayout(sel_row)
            if not piece_types:
                pt_layout.addWidget(QLabel(
                    "<i>(No piece-type folders found under this scope.)</i>"
                ))
        else:
            self._piece_type_group.hide()

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        btns.button(QDialogButtonBox.StandardButton.Ok).setText("Shuffle")
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(header)
        if self._scope_composer is None:
            layout.addWidget(self._composer_group, 1)
        if self._scope_piece_type is None:
            layout.addWidget(self._piece_type_group, 1)
        layout.addWidget(btns)

    @pyqtSlot()
    def _select_all_composers(self) -> None:
        for cb in self._composer_checks:
            cb.setChecked(True)

    @pyqtSlot()
    def _select_none_composers(self) -> None:
        for cb in self._composer_checks:
            cb.setChecked(False)

    @pyqtSlot()
    def _select_all_piece_types(self) -> None:
        for cb in self._piece_type_checks:
            cb.setChecked(True)

    @pyqtSlot()
    def _select_none_piece_types(self) -> None:
        for cb in self._piece_type_checks:
            cb.setChecked(False)

    def selected_composers(self) -> list[str] | None:
        if self._scope_composer is not None:
            return None
        return [cb.text() for cb in self._composer_checks if cb.isChecked()]

    def selected_piece_types(self) -> list[str] | None:
        if self._scope_piece_type is not None:
            return None
        return [cb.text() for cb in self._piece_type_checks if cb.isChecked()]


class ClassicalShufflePlaylistDialog(QDialog):
    """Configure a generated Classical Shuffle playlist for iPhone listening.

    Step 5 of the playlist generator (see
    [[project_classical_shuffle_playlist_gen_plan]]). Collects user input
    only — the Step 6 build coroutine consumes ``values()`` to actually
    sample the pool, create the Amazon Music playlist, and add tracks via
    the `add_track_by_asin` deep-link flow verified in Step 3.

    Scope semantics mirror the existing "Classical Shuffle (recursive)"
    entry: the dialog is launched with the right-clicked folder's
    ``scope_path``. When that path goes through a single composer (e.g.
    ``["Classical", "Beethoven"]``), the composer multi-select is hidden
    because there is no choice to make. The piece-type multi-select is
    hidden independently for single-piece-type scopes
    (``["Classical", "Beethoven", "Symphonies"]``) — so a broad scope
    shows both filters, a single-composer scope shows just piece types,
    and a single-piece-type scope shows neither.

    The web-restricted checkbox proposed in the original UX sketch was
    DROPPED after the 2026-06-01 iOS smoke test — iOS Amazon Music has
    full catalog access, so the web-only substitution doesn't apply to
    the generated playlist's actual playback target.
    """

    _NAME_PREFIX = "Classical Shuffle"
    # Cap each multi-select's checkbox area. ~200px ≈ 7-8 checkboxes
    # before the scrollbar kicks in — enough to see context, not so big
    # that the dialog overflows the screen even on a long composer list.
    _CHECKLIST_MAX_HEIGHT = 200

    def __init__(
        self,
        parent: QWidget | None,
        *,
        scope_path: list[str],
        org: dict,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Generate Classical Shuffle Playlist")
        self.setMinimumWidth(520)

        self._scope_path: list[str] = list(scope_path)
        self._org = org
        self._scope_composer = _composer_from_org_path(self._scope_path)
        self._scope_piece_type = _piece_type_from_org_path(self._scope_path)
        # Default the output folder to the top-level "Shuffle Classical"
        # folder when it exists — that's where generated playlists are
        # collected. Fall back to the scope itself if the user has renamed
        # or removed that folder so the picker still starts somewhere
        # sensible (and add_item won't raise on a missing path).
        if get_node(org, ["Shuffle Classical"]) is not None:
            self._output_folder_path: list[str] = ["Shuffle Classical"]
        else:
            self._output_folder_path = list(self._scope_path)

        scope_label = "/".join(self._scope_path) if self._scope_path else "/"
        header = QLabel(f"Source: <b>{scope_label}</b>")
        header.setTextFormat(Qt.TextFormat.RichText)

        # --- Composer multi-select (hidden when scope is single-composer) ---
        # A scope like ["Classical"] can produce 20+ composers, which used
        # to stretch the dialog past the screen and hide the OK button.
        # The checkbox list now sits in a height-capped QScrollArea; the
        # Select-all/none row stays fixed below it inside the groupbox.
        self._composer_group = QGroupBox("Composers")
        self._composer_checks: list[QCheckBox] = []
        if self._scope_composer is None:
            composers_layout = QVBoxLayout(self._composer_group)
            composers = _composers_in_scope(org, self._scope_path)
            checks_host = QWidget()
            checks_host_layout = QVBoxLayout(checks_host)
            checks_host_layout.setContentsMargins(0, 0, 0, 0)
            for c in composers:
                cb = QCheckBox(c)
                cb.setChecked(True)
                cb.toggled.connect(self._on_composer_toggled)
                checks_host_layout.addWidget(cb)
                self._composer_checks.append(cb)
            if not composers:
                # Scope passes the "Classical somewhere in path" guard but
                # contains no composer subtree — degenerate but recoverable.
                checks_host_layout.addWidget(QLabel(
                    "<i>(No composers found under this scope.)</i>"
                ))
            checks_host_layout.addStretch(1)
            composer_scroll = QScrollArea()
            composer_scroll.setWidgetResizable(True)
            composer_scroll.setWidget(checks_host)
            composer_scroll.setMaximumHeight(self._CHECKLIST_MAX_HEIGHT)
            composer_scroll.setHorizontalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAlwaysOff
            )
            composers_layout.addWidget(composer_scroll, 1)
            sel_row = QHBoxLayout()
            sel_all = QPushButton("Select all")
            sel_all.clicked.connect(self._select_all_composers)
            sel_none = QPushButton("Select none")
            sel_none.clicked.connect(self._select_none_composers)
            sel_row.addStretch(1)
            sel_row.addWidget(sel_all)
            sel_row.addWidget(sel_none)
            composers_layout.addLayout(sel_row)
        else:
            self._composer_group.hide()

        # --- Piece-type multi-select (hidden when scope is single-type) ---
        # Independent of composer scope: ``Classical/Beethoven`` hides the
        # composer group but still benefits from a piece-type filter
        # (Beethoven Symphonies vs Beethoven Piano Sonatas). Same scroll
        # wrap as the composer group above.
        self._piece_type_group = QGroupBox("Piece types")
        self._piece_type_checks: list[QCheckBox] = []
        if self._scope_piece_type is None:
            pt_layout = QVBoxLayout(self._piece_type_group)
            piece_types = _piece_types_in_scope(org, self._scope_path)
            pt_checks_host = QWidget()
            pt_checks_host_layout = QVBoxLayout(pt_checks_host)
            pt_checks_host_layout.setContentsMargins(0, 0, 0, 0)
            for pt in piece_types:
                cb = QCheckBox(pt)
                cb.setChecked(True)
                cb.toggled.connect(self._on_piece_type_toggled)
                pt_checks_host_layout.addWidget(cb)
                self._piece_type_checks.append(cb)
            if not piece_types:
                pt_checks_host_layout.addWidget(QLabel(
                    "<i>(No piece-type folders found under this scope.)</i>"
                ))
            pt_checks_host_layout.addStretch(1)
            pt_scroll = QScrollArea()
            pt_scroll.setWidgetResizable(True)
            pt_scroll.setWidget(pt_checks_host)
            pt_scroll.setMaximumHeight(self._CHECKLIST_MAX_HEIGHT)
            pt_scroll.setHorizontalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAlwaysOff
            )
            pt_layout.addWidget(pt_scroll, 1)
            sel_row = QHBoxLayout()
            sel_all = QPushButton("Select all")
            sel_all.clicked.connect(self._select_all_piece_types)
            sel_none = QPushButton("Select none")
            sel_none.clicked.connect(self._select_none_piece_types)
            sel_row.addStretch(1)
            sel_row.addWidget(sel_all)
            sel_row.addWidget(sel_none)
            pt_layout.addLayout(sel_row)
        else:
            self._piece_type_group.hide()

        # --- Duration target ---
        self._duration = QDoubleSpinBox()
        self._duration.setRange(0.5, 8.0)
        self._duration.setSingleStep(0.5)
        self._duration.setDecimals(1)
        self._duration.setSuffix(" hours")
        self._duration.setValue(2.0)

        # --- Output name ---
        self._name = QLineEdit()
        self._name.setPlaceholderText("Playlist name on Amazon Music")

        # --- Output folder picker ---
        out_row = QHBoxLayout()
        self._output_folder_label = QLabel(
            self._format_folder_path(self._output_folder_path)
        )
        self._output_folder_label.setTextFormat(Qt.TextFormat.RichText)
        pick_btn = QPushButton("Pick folder…")
        pick_btn.clicked.connect(self._pick_output_folder)
        out_row.addWidget(self._output_folder_label, 1)
        out_row.addWidget(pick_btn)

        # --- Form layout ---
        form = QFormLayout()
        form.addRow("Duration target:", self._duration)
        form.addRow("Playlist name:", self._name)
        form.addRow("File under:", out_row)

        note = QLabel(
            "<i>Samples random performances from the source pool until "
            "cumulative duration meets your target (one overshoot allowed). "
            "Creates a new Amazon Music playlist and adds tracks by ASIN "
            "deep-link — plays correctly on the iOS Amazon Music app.</i>"
        )
        note.setTextFormat(Qt.TextFormat.RichText)
        note.setWordWrap(True)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        btns.button(QDialogButtonBox.StandardButton.Ok).setText("Generate")
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(header)
        if self._scope_composer is None:
            layout.addWidget(self._composer_group, 1)
        if self._scope_piece_type is None:
            layout.addWidget(self._piece_type_group, 1)
        layout.addLayout(form)
        layout.addWidget(note)
        layout.addWidget(btns)

        # Defensive screen-fit cap. The checkbox scroll areas above already
        # bound the tall content, but a screen-relative ceiling keeps the
        # dialog reachable if a future change adds more rows to the form
        # or the user runs at a higher DPI scale than expected.
        screen = self.screen() or QApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry().height()
            self.setMaximumHeight(max(400, avail - 80))

        # Pick the date prefix + letter suffix once at dialog open so the
        # default name's day-letter stays stable even if the user toggles
        # filter checkboxes (which retrigger _refresh_default_name).
        self._date_str, self._date_letter = self._compute_date_prefix()

        # Prefill the default name now that the composer checkboxes exist.
        self._refresh_default_name()

    # ---- Composer-select helpers ----------------------------------------

    @pyqtSlot()
    def _select_all_composers(self) -> None:
        for cb in self._composer_checks:
            cb.setChecked(True)

    @pyqtSlot()
    def _select_none_composers(self) -> None:
        for cb in self._composer_checks:
            cb.setChecked(False)

    @pyqtSlot(bool)
    def _on_composer_toggled(self, _checked: bool) -> None:
        self._refresh_default_name()

    # ---- Piece-type-select helpers --------------------------------------

    @pyqtSlot()
    def _select_all_piece_types(self) -> None:
        for cb in self._piece_type_checks:
            cb.setChecked(True)

    @pyqtSlot()
    def _select_none_piece_types(self) -> None:
        for cb in self._piece_type_checks:
            cb.setChecked(False)

    @pyqtSlot(bool)
    def _on_piece_type_toggled(self, _checked: bool) -> None:
        self._refresh_default_name()

    # ---- Output-folder picker ------------------------------------------

    @pyqtSlot()
    def _pick_output_folder(self) -> None:
        dlg = _FolderPickerDialog(
            self,
            title="Pick output folder",
            prompt="File the generated playlist under:",
        )
        if dlg.exec() == QDialog.DialogCode.Accepted:
            picked = dlg.chosen_path()
            if picked is not None:
                self._output_folder_path = list(picked)
                self._output_folder_label.setText(
                    self._format_folder_path(self._output_folder_path)
                )

    @staticmethod
    def _format_folder_path(path: list[str]) -> str:
        return f"<b>/  (Root)</b>" if not path else f"<b>{'/'.join(path)}</b>"

    # ---- Name generation -----------------------------------------------

    def _selected_composers(self) -> list[str] | None:
        # None signals "no composer filter" — either single-composer scope
        # (the scope itself is the filter) or the caller can interpret it
        # as "include everything in scope". Step 6 will distinguish.
        if self._scope_composer is not None:
            return None
        return [cb.text() for cb in self._composer_checks if cb.isChecked()]

    def _selected_piece_types(self) -> list[str] | None:
        # None signals "no piece-type filter" — either single-piece-type
        # scope (scope is the filter) or "include everything in scope".
        if self._scope_piece_type is not None:
            return None
        return [cb.text() for cb in self._piece_type_checks if cb.isChecked()]

    def _compute_date_prefix(self) -> tuple[str, str]:
        """Return today's YYYYMMDD and the first unused letter suffix.

        Scans the cached playlist list for names already shaped like
        ``YYYYMMDD[letter]-Classical Shuffle...`` for today's date and
        picks the next free slot in the sequence ``"", "b", "c", ...``.
        The first playlist of the day gets no letter; the second gets
        ``b``; and so on. Letters are reserved by the *cached prefix*, not
        the full name — so a qualified ``...Beethoven Symphonies`` rules
        out the bare ``20260610-Classical Shuffle`` too. That's the right
        call: the user's whole purpose is to disambiguate same-day runs.
        """
        import re
        date_str = datetime.now().strftime("%Y%m%d")
        used: set[str] = set()
        try:
            from playlist_cache import load_cached_playlists
            cached = load_cached_playlists() or []
        except Exception:
            cached = []
        pat = re.compile(
            rf"^{date_str}([a-z]?)-classical shuffle\b",
            re.IGNORECASE,
        )
        for pl in cached:
            name = (pl.get("name") or "").strip()
            m = pat.match(name)
            if m:
                used.add(m.group(1).lower())
        letters = [""] + [chr(c) for c in range(ord("b"), ord("z") + 1)]
        for letter in letters:
            if letter not in used:
                return date_str, letter
        # Past 26 same-day runs — give up and reuse 'z'. The user can
        # type their own name; this is a backstop, not a hard limit.
        return date_str, "z"

    def _refresh_default_name(self) -> None:
        # Don't clobber a user-typed name. QLineEdit.isModified() returns
        # True only when the USER has edited; setText() resets the flag.
        if self._name.isModified() and self._name.text().strip():
            return
        date_part = f"{self._date_str}{self._date_letter}"

        # Build per-axis qualifier fragments. Each axis contributes its
        # scope value when the scope pins it down, or a comma-joined
        # selection when the user has narrowed (but not all-or-empty).
        # The len <= 3 gate matches the composer-side heuristic — past
        # that the playlist name gets unreadable; the user can type their
        # own if they want.
        def _qual(scope_value, selected_list, all_options):
            if scope_value is not None:
                return scope_value
            picked = selected_list or []
            include = (
                bool(picked)
                and set(picked) != set(all_options)
                and len(picked) <= 3
            )
            return ", ".join(picked) if include else ""

        composer_qual = _qual(
            self._scope_composer,
            self._selected_composers() or [],
            [cb.text() for cb in self._composer_checks],
        )
        type_qual = _qual(
            self._scope_piece_type,
            self._selected_piece_types() or [],
            [cb.text() for cb in self._piece_type_checks],
        )

        # Space-join non-empty quals — "Beethoven Symphonies" reads as a
        # natural phrase. When both axes are broad, fall back to the
        # bare date-prefix + name.
        qualifier = " ".join(q for q in (composer_qual, type_qual) if q)
        base = f"{date_part}-{self._NAME_PREFIX}"
        default = f"{base} - {qualifier}" if qualifier else base
        self._name.setText(default)
        self._name.setModified(False)

    # ---- Public accessor for the build coroutine ------------------------

    def values(self) -> dict:
        """Snapshot of user choices for the Step 6 build coroutine."""
        return {
            "scope_path": list(self._scope_path),
            "name": self._name.text().strip(),
            "target_seconds": int(round(self._duration.value() * 3600)),
            "composers": self._selected_composers(),
            "piece_types": self._selected_piece_types(),
            "output_folder_path": list(self._output_folder_path),
        }


def _append_song_to_sheet(
    path: str,
    title: str,
    artist: str,
    album: str,
    is_new: bool,
) -> dict:
    """Append a single song to a workbook, deduping case-insensitively on
    (title, artist). Creates a Local-tagged workbook with the standard
    columns when ``is_new`` is True."""
    from openpyxl import Workbook, load_workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from spreadsheet_tags import (
        TAG_LOCAL, TAG_SHEET, set_tag, set_updated,
    )

    norm_t = (title or "").strip().lower()
    norm_a = (artist or "").strip().lower()

    if is_new or not os.path.exists(path):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        wb = Workbook()
        ws = wb.active
        ws.title = "Songs"
        headers = ["#", "Song Name", "Artist", "Album", "Release Date",
                   "Genre", "Duration"]
        header_font = Font(bold=True, color="FFFFFF", size=11)
        header_fill = PatternFill(
            start_color="4472C4", end_color="4472C4", fill_type="solid",
        )
        for col_idx, header in enumerate(headers, start=1):
            cell = ws.cell(row=1, column=col_idx, value=header)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal="center")
        ws.cell(row=2, column=1, value=1)
        ws.cell(row=2, column=2, value=title)
        ws.cell(row=2, column=3, value=artist)
        ws.cell(row=2, column=4, value=album)
        ws.freeze_panes = "A2"
        wb.save(path)
        set_tag(path, TAG_LOCAL)
        set_updated(path)
        return {"path": path, "added": True, "duplicate": False, "created": True}

    wb = load_workbook(path)
    sheet_name = next(
        (s for s in wb.sheetnames if s != TAG_SHEET),
        wb.sheetnames[0] if wb.sheetnames else None,
    )
    if sheet_name is None:
        wb.close()
        raise RuntimeError(f"No data sheet found in {path}")
    ws = wb[sheet_name]

    # Read header row to find Title/Artist/Album columns.
    headers = []
    for c in range(1, ws.max_column + 1):
        v = ws.cell(row=1, column=c).value
        headers.append(str(v) if v is not None else "")
    ti = _pick_header_index(headers, _TITLE_HEADER_ALIASES)
    ai = _pick_header_index(headers, _ARTIST_HEADER_ALIASES)
    bi = _pick_header_index(headers, _ALBUM_HEADER_ALIASES)
    if ti is None or ai is None:
        wb.close()
        raise RuntimeError(
            f"Could not find Title/Artist columns in {os.path.basename(path)}"
        )

    # Dedupe against existing rows.
    for r in range(2, ws.max_row + 1):
        rt = ws.cell(row=r, column=ti + 1).value
        ra = ws.cell(row=r, column=ai + 1).value
        if (str(rt or "").strip().lower() == norm_t
                and str(ra or "").strip().lower() == norm_a):
            wb.close()
            return {"path": path, "added": False, "duplicate": True,
                    "created": False}

    new_row = ws.max_row + 1
    # Number column (column 1) — only fill if header looks like "#".
    if headers and headers[0].strip() in ("#", "No.", "No"):
        ws.cell(row=new_row, column=1, value=new_row - 1)
    ws.cell(row=new_row, column=ti + 1, value=title)
    ws.cell(row=new_row, column=ai + 1, value=artist)
    if bi is not None:
        ws.cell(row=new_row, column=bi + 1, value=album)
    wb.save(path)
    set_updated(path)
    return {"path": path, "added": True, "duplicate": False, "created": False}


class NowPlayingDock(QDockWidget):
    """Persistent dock that shows the currently playing playlist + track and
    surfaces transport controls. Hidden when no playback is active."""

    previous_requested = pyqtSignal()
    play_pause_requested = pyqtSignal()
    next_requested = pyqtSignal()
    stop_requested = pyqtSignal()
    seek_requested = pyqtSignal(int)  # target position in seconds
    add_to_sheet_requested = pyqtSignal()  # user clicked "+ Save"
    prev_playlist_requested = pyqtSignal()  # user clicked ⏪ — go back in queue
    skip_playlist_requested = pyqtSignal()  # user clicked ⏩ — advance queue

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Now Playing", parent)
        self.setAllowedAreas(
            Qt.DockWidgetArea.BottomDockWidgetArea
            | Qt.DockWidgetArea.TopDockWidgetArea
        )

        # Queue state — defaults so _set_controls_enabled is safe to call
        # before begin() (e.g. during initial hide). Updated by begin() and
        # set_queue_position().
        self._queue_total = 1
        self._queue_index = 1
        self._shuffle = False
        self._shuffle_queue_mode = False
        # Classical Shuffle mode: there is no fixed queue, but ⏪/⏩ should
        # still be visible — wired to "previous performance" / "skip
        # performance" instead of queue navigation.
        self._perf_shuffle = False

        body = QWidget()
        # Vertical-stack layout: text labels on top (free to wrap to multiple
        # lines without pushing into anything), then the progress slider, then
        # transport controls. This is more dock-area-friendly than the prior
        # side-by-side layout, where long classical titles fought with the
        # button row for horizontal space and visually overlapped.
        outer = QVBoxLayout(body)
        outer.setContentsMargins(8, 4, 8, 4)
        outer.setSpacing(2)

        # Labels — word-wrap, ignore horizontal sizeHint so a long unwrapped
        # title can't push the dock wider than the available area.
        self._header_label = QLabel("")
        self._header_label.setTextFormat(Qt.TextFormat.RichText)
        self._header_label.setWordWrap(True)
        self._header_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self._track_label = QLabel("")
        self._track_label.setStyleSheet(
            "color: white; font-weight: bold;"
        )
        self._track_label.setWordWrap(True)
        self._track_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )

        # Progress row: slider + "MM:SS / MM:SS" label.
        progress_row = QHBoxLayout()
        progress_row.setContentsMargins(0, 0, 0, 0)
        progress_row.setSpacing(6)
        self._progress_slider = QSlider(Qt.Orientation.Horizontal)
        self._progress_slider.setRange(0, 0)
        self._progress_slider.setMaximumHeight(14)
        self._progress_slider.setSingleStep(1)
        self._user_seeking = False
        self._progress_slider.sliderPressed.connect(self._on_seek_start)
        self._progress_slider.sliderReleased.connect(self._on_seek_release)
        self._progress_slider.valueChanged.connect(self._on_seek_value_changed)
        # Local interpolation state: between server polls (~5–7s), tick the
        # display forward at 1s/wallclock-s so the bar moves smoothly.
        self._progress_anchor_pos = 0
        self._progress_anchor_time = 0.0
        self._progress_duration = 0
        self._progress_last_poll_pos = -1
        self._progress_paused = False
        self._progress_tick_timer = QTimer(self)
        self._progress_tick_timer.setInterval(500)
        self._progress_tick_timer.timeout.connect(self._tick_progress)
        self._time_label = QLabel("")
        self._time_label.setStyleSheet("color: #aaa;")
        f_time = self._time_label.font()
        f_time.setPointSizeF(max(7.0, f_time.pointSizeF() * 0.85))
        self._time_label.setFont(f_time)
        progress_row.addWidget(self._progress_slider, 1)
        progress_row.addWidget(self._time_label)

        # Transport row: ⏪  ⏯  ⏩   |   ⏹ Stop (track = double-arrows)
        self._prev_btn = self._make_transport_btn(
            "\u23EA", "Previous track", self.previous_requested
        )
        self._play_pause_btn = self._make_transport_btn(
            "\u23EF", "Play / Pause", self.play_pause_requested
        )
        self._next_btn = self._make_transport_btn(
            "\u23E9", "Next track", self.next_requested
        )
        # Prev/next-playlist buttons — visible when queue len > 1.
        self._prev_playlist_btn = self._make_transport_btn(
            "⏮", "Go back to previous playlist in queue",
            self.prev_playlist_requested,
        )
        self._prev_playlist_btn.setVisible(False)
        self._skip_playlist_btn = self._make_transport_btn(
            "⏭", "Skip to next playlist in queue",
            self.skip_playlist_requested,
        )
        self._skip_playlist_btn.setVisible(False)
        self._stop_btn = QPushButton("Stop")
        self._stop_btn.setMinimumWidth(86)
        self._stop_btn.setToolTip("Pause music and close this view")
        self._stop_btn.clicked.connect(self._on_stop_clicked)

        # Volume slider — vertical, compact. Drives Chrome's per-app
        # Windows audio session via pycaw, polled by `_volume_timer`.
        self._volume_slider = QSlider(Qt.Orientation.Vertical)
        self._volume_slider.setRange(0, 100)
        self._volume_slider.setValue(50)
        self._volume_slider.setEnabled(False)
        self._volume_slider.setFixedHeight(72)
        self._volume_slider.setFixedWidth(20)
        self._volume_slider.setToolTip("Chrome audio volume")
        self._volume_user_seeking = False
        self._volume_slider.sliderPressed.connect(self._on_volume_seek_start)
        self._volume_slider.sliderReleased.connect(self._on_volume_seek_release)
        self._volume_timer = QTimer(self)
        self._volume_timer.setInterval(1500)
        self._volume_timer.timeout.connect(self._poll_chrome_volume)

        # "+ Save" — add the current track to a spreadsheet.
        self._add_to_sheet_btn = QPushButton("+ Save")
        self._add_to_sheet_btn.setMinimumWidth(80)
        self._add_to_sheet_btn.setToolTip(
            "Add the currently-playing song to a spreadsheet"
        )
        self._add_to_sheet_btn.clicked.connect(self.add_to_sheet_requested)

        # Controls row: ⏪ ⏮ ⏯ ⏭ ⏩ on the left (transport order), then a
        # stretch, then volume / save / stop on the right.
        controls_row = QHBoxLayout()
        controls_row.setContentsMargins(0, 0, 0, 0)
        controls_row.setSpacing(4)
        controls_row.addWidget(self._prev_playlist_btn)
        controls_row.addWidget(self._prev_btn)
        controls_row.addWidget(self._play_pause_btn)
        controls_row.addWidget(self._next_btn)
        controls_row.addWidget(self._skip_playlist_btn)
        controls_row.addStretch(1)
        controls_row.addWidget(
            self._volume_slider, 0, Qt.AlignmentFlag.AlignVCenter,
        )
        controls_row.addSpacing(8)
        controls_row.addWidget(self._add_to_sheet_btn)
        controls_row.addSpacing(8)
        controls_row.addWidget(self._stop_btn)

        outer.addWidget(self._header_label)
        outer.addWidget(self._track_label)
        outer.addLayout(progress_row)
        outer.addLayout(controls_row)

        self.setWidget(body)
        self.hide()

    def _make_transport_btn(self, glyph: str, tooltip: str, sig) -> QPushButton:
        btn = QPushButton(glyph)
        btn.setToolTip(tooltip)
        btn.setFixedWidth(44)
        # Bigger glyph so the unicode arrows render well at GUI font size.
        f = btn.font()
        f.setPointSizeF(f.pointSizeF() * 1.4)
        btn.setFont(f)
        btn.clicked.connect(sig)
        return btn

    def _set_controls_enabled(self, enabled: bool) -> None:
        self._prev_btn.setEnabled(enabled)
        self._play_pause_btn.setEnabled(enabled)
        self._next_btn.setEnabled(enabled)
        # Prev/next-playlist buttons are meaningful in either a multi-item
        # queue OR Classical Shuffle (where they map to prev/skip
        # performance). The shuffle loop ignores prev_playlist when no
        # history exists, so it's fine to leave the button enabled.
        self._prev_playlist_btn.setEnabled(
            enabled and (
                self._perf_shuffle
                or (self._queue_total > 1 and self._queue_index > 1)
            )
        )
        self._skip_playlist_btn.setEnabled(
            enabled and (
                self._perf_shuffle
                or (self._queue_total > 1
                    and self._queue_index < self._queue_total)
            )
        )
        self._stop_btn.setEnabled(enabled)
        # The save button is enabled iff a track is showing AND playback
        # is alive — we toggle this from update_track too.
        self._add_to_sheet_btn.setEnabled(enabled and bool(self._current_title))
        # Volume slider stays as-set by update_volume; don't touch its
        # enabled state from here so it reflects whether we found a slider.

    @property
    def _current_title(self) -> str:
        return getattr(self, "_track_title", "") or ""

    def begin(
        self,
        name: str,
        shuffle: bool,
        queue_total: int = 1,
        shuffle_queue: bool = False,
        perf_shuffle: bool = False,
    ) -> None:
        self._queue_total = max(1, int(queue_total))
        self._queue_index = 1
        self._shuffle = bool(shuffle)
        self._shuffle_queue_mode = bool(shuffle_queue)
        self._perf_shuffle = bool(perf_shuffle)
        # Tooltips depend on whether ⏪/⏩ are queue-nav or perf-skip.
        if self._perf_shuffle:
            self._prev_playlist_btn.setToolTip("Previous performance")
            self._skip_playlist_btn.setToolTip("Skip to next performance")
        else:
            self._prev_playlist_btn.setToolTip(
                "Go back to previous playlist in queue"
            )
            self._skip_playlist_btn.setToolTip(
                "Skip to next playlist in queue"
            )
        show_perf_skip = self._queue_total > 1 or self._perf_shuffle
        self._prev_playlist_btn.setVisible(show_perf_skip)
        self._skip_playlist_btn.setVisible(show_perf_skip)
        self._render_header(name)
        self._track_label.setText("Loading…")
        self._progress_slider.setRange(0, 0)
        self._progress_slider.setValue(0)
        self._time_label.setText("")
        # Reset interpolation state for the new playback.
        self._progress_anchor_pos = 0
        self._progress_anchor_time = time.monotonic()
        self._progress_duration = 0
        self._progress_last_poll_pos = -1
        self._progress_paused = False
        # Reset cached track + meta for the new playback.
        self._track_title = ""
        self._track_artist = ""
        self._track_album = ""
        self._set_controls_enabled(True)
        self._stop_btn.setText("Stop")
        # Read the current Chrome volume right away, then poll periodically
        # so external changes (volume mixer, hotkeys) reflect in the slider.
        self._poll_chrome_volume()
        self._volume_timer.start()
        self.show()
        self.raise_()

    def current_track_meta(self) -> tuple[str, str, str]:
        """Return ``(title, artist, album)`` for the currently displayed track,
        or empty strings if none."""
        return (
            getattr(self, "_track_title", "") or "",
            getattr(self, "_track_artist", "") or "",
            getattr(self, "_track_album", "") or "",
        )

    def _render_header(self, name: str) -> None:
        shuffle = getattr(self, "_shuffle", False)
        shuffle_q = getattr(self, "_shuffle_queue_mode", False)
        if shuffle and shuffle_q:
            mode = "shuffle · playlists shuffled"
        elif shuffle:
            mode = "shuffle"
        elif shuffle_q:
            mode = "straight · playlists shuffled"
        else:
            mode = "straight"
        prefix = ""
        if getattr(self, "_queue_total", 1) > 1:
            prefix = (
                f"<span style='color:#888'>"
                f"[{self._queue_index}/{self._queue_total}]</span> "
            )
        self._header_label.setText(
            f"{prefix}<b>{name}</b> &middot; "
            f"<span style='color:#666'>{mode}</span>"
        )

    @pyqtSlot(int, int, str)
    def set_queue_position(self, index: int, total: int, name: str) -> None:
        """Update the header to reflect that we've advanced to playlist *index*
        of *total* (1-based). Disables the skip button on the last item."""
        self._queue_total = max(1, int(total))
        self._queue_index = max(1, min(int(index), self._queue_total))
        show_perf_skip = self._queue_total > 1 or self._perf_shuffle
        self._prev_playlist_btn.setVisible(show_perf_skip)
        self._skip_playlist_btn.setVisible(show_perf_skip)
        self._prev_playlist_btn.setEnabled(
            self._perf_shuffle
            or (self._queue_total > 1 and self._queue_index > 1)
        )
        self._skip_playlist_btn.setEnabled(
            self._perf_shuffle
            or (self._queue_total > 1 and self._queue_index < self._queue_total)
        )
        self._render_header(name)

    @staticmethod
    def _fmt_time(seconds: int) -> str:
        seconds = max(0, int(seconds))
        m, s = divmod(seconds, 60)
        if m >= 60:
            h, m = divmod(m, 60)
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m}:{s:02d}"

    @pyqtSlot(int, int)
    def update_progress(self, position: int, duration: int) -> None:
        # Called once per server poll (~5–7 s). We anchor here, then a
        # local QTimer interpolates 1 s of progress per wallclock second
        # between calls — see _tick_progress().
        if duration <= 0:
            self._progress_duration = 0
            self._progress_paused = False
            self._progress_last_poll_pos = -1
            self._progress_tick_timer.stop()
            self._progress_slider.setRange(0, 0)
            self._progress_slider.blockSignals(True)
            self._progress_slider.setValue(0)
            self._progress_slider.blockSignals(False)
            self._time_label.setText("")
            return

        # If the server reported the SAME position twice in a row, music is
        # paused — freeze the display and stop interpolating.
        if (self._progress_last_poll_pos >= 0
                and position == self._progress_last_poll_pos):
            self._progress_paused = True
        else:
            self._progress_paused = False
            self._progress_anchor_pos = position
            self._progress_anchor_time = time.monotonic()
        self._progress_duration = duration
        self._progress_last_poll_pos = position

        if not self._progress_tick_timer.isActive():
            self._progress_tick_timer.start()

        if self._user_seeking:
            return
        self._set_progress_display(position, duration)

    def _set_progress_display(self, position: int, duration: int) -> None:
        if self._progress_slider.maximum() != duration:
            self._progress_slider.setRange(0, duration)
        self._progress_slider.blockSignals(True)
        self._progress_slider.setValue(min(position, duration))
        self._progress_slider.blockSignals(False)
        self._time_label.setText(
            f"{self._fmt_time(position)} / {self._fmt_time(duration)}"
        )

    @pyqtSlot()
    def _tick_progress(self) -> None:
        if (self._user_seeking or self._progress_paused
                or self._progress_duration <= 0):
            return
        elapsed = time.monotonic() - self._progress_anchor_time
        estimated = min(
            self._progress_anchor_pos + elapsed,
            self._progress_duration,
        )
        self._set_progress_display(int(estimated), self._progress_duration)

    @pyqtSlot()
    def _on_seek_start(self) -> None:
        self._user_seeking = True

    @pyqtSlot()
    def _on_seek_release(self) -> None:
        self._user_seeking = False
        target = self._progress_slider.value()
        # Reflect optimistically; server may snap a second or two off.
        dur = self._progress_slider.maximum()
        if dur > 0:
            self._time_label.setText(
                f"{self._fmt_time(target)} / {self._fmt_time(dur)}"
            )
        self.seek_requested.emit(target)

    @pyqtSlot(int)
    def _on_seek_value_changed(self, value: int) -> None:
        # Keep the time label live while dragging, but only if the user is
        # the one moving the slider.
        if not self._user_seeking:
            return
        dur = self._progress_slider.maximum()
        if dur > 0:
            self._time_label.setText(
                f"{self._fmt_time(value)} / {self._fmt_time(dur)}"
            )

    @pyqtSlot(str, str)
    def update_track(self, title: str, artist: str) -> None:
        self._track_title = title or ""
        self._track_artist = artist or ""
        # Save button only makes sense when we know what's playing.
        self._add_to_sheet_btn.setEnabled(bool(self._track_title))
        # Track-changed only fires on a real track change, so wipe the
        # progress bar back to 0 immediately. Otherwise the previous
        # track's interpolated value lingers until the next poll lands
        # (~5–7s), giving the appearance of a brand-new song "stuck" at
        # something like 9:01.
        self._progress_anchor_pos = 0
        self._progress_anchor_time = time.monotonic()
        self._progress_duration = 0
        self._progress_last_poll_pos = -1
        self._progress_paused = False
        self._progress_tick_timer.stop()
        self._progress_slider.setRange(0, 0)
        self._progress_slider.blockSignals(True)
        self._progress_slider.setValue(0)
        self._progress_slider.blockSignals(False)
        self._time_label.setText("")
        if not title:
            self._track_label.setText("(no track info)")
            return
        if artist:
            self._track_label.setText(f"♪ {title} — {artist}")
        else:
            self._track_label.setText(f"♪ {title}")

    @pyqtSlot(str, str, str)
    def update_track_meta(self, title: str, artist: str, album: str) -> None:
        # Album is auxiliary; we just remember it for the Save button.
        self._track_title = title or ""
        self._track_artist = artist or ""
        self._track_album = album or ""

    @pyqtSlot()
    def _poll_chrome_volume(self) -> None:
        """Read Chrome's current audio-session volume and update the slider.
        Skips while the user is dragging so we don't fight them."""
        v = _get_chrome_volume()
        if v is None:
            self._volume_slider.setEnabled(False)
            return
        self._volume_slider.setEnabled(True)
        if self._volume_user_seeking:
            return
        scaled = int(round(v * 100))
        self._volume_slider.blockSignals(True)
        self._volume_slider.setValue(scaled)
        self._volume_slider.blockSignals(False)

    @pyqtSlot()
    def _on_volume_seek_start(self) -> None:
        self._volume_user_seeking = True

    @pyqtSlot()
    def _on_volume_seek_release(self) -> None:
        self._volume_user_seeking = False
        target = self._volume_slider.value() / 100.0
        n = _set_chrome_volume(target)
        if n == 0:
            logger.warning(
                "Volume change ignored — no chrome.exe audio session found."
            )

    @pyqtSlot(str)
    def update_status(self, msg: str) -> None:
        # Reuse the track row when no track has been read yet so the dock
        # tells the user *something* during navigation / login.
        if msg:
            self._track_label.setText(msg)

    def end(self) -> None:
        self._volume_timer.stop()
        self._progress_tick_timer.stop()
        self._set_controls_enabled(False)
        self._stop_btn.setText("Stopped")
        # Reset queue state so a stale skip button doesn't bleed into the next
        # playback session.
        self._queue_total = 1
        self._queue_index = 1
        self._perf_shuffle = False
        self._prev_playlist_btn.setVisible(False)
        self._skip_playlist_btn.setVisible(False)
        self.hide()

    @pyqtSlot()
    def _on_stop_clicked(self) -> None:
        # Disable transport buttons so users can't queue actions during the
        # pause/teardown window. The dock will hide once the coroutine returns.
        self._set_controls_enabled(False)
        self._stop_btn.setText("Stopping…")
        self.stop_requested.emit()


class PlaybackController(QObject):
    """Bridges the worker-thread coroutine and the Now-Playing dock.

    Transport actions (stop / play_pause / next / previous) are pushed onto
    an ``asyncio.Queue`` that the coroutine consumes between polls. The queue
    lives on the worker loop; the GUI thread schedules ``put`` calls via
    ``call_soon_threadsafe`` so we never block on cross-thread I/O.
    """

    track_changed = pyqtSignal(str, str)   # (title, artist)
    track_meta = pyqtSignal(str, str, str) # (title, artist, album) — full info
    progress = pyqtSignal(int, int)        # (position_seconds, duration_seconds)
    status = pyqtSignal(str)               # phase strings ("Loading…")
    log = pyqtSignal(str)                  # human-readable line
    # Queue: (1-based index of currently-playing item, queue total, item name).
    playlist_advanced = pyqtSignal(int, int, str)
    finished = pyqtSignal(object)          # final summary dict

    def __init__(self) -> None:
        super().__init__()
        self.cancelled = False
        self._loop: asyncio.AbstractEventLoop | None = None
        # Created on the worker loop by the coroutine.
        self._action_queue: asyncio.Queue | None = None

    def request_action(self, action) -> None:
        """Queue a transport action — string verb, or ``(verb, payload)`` tuple
        for actions that carry a parameter (e.g. ``("seek", 42)``)."""
        logger.info("Playback: action requested: %s", action)
        loop = self._loop
        q = self._action_queue
        if loop is None or q is None:
            logger.warning(
                "Playback: action '%s' arrived before coroutine was ready; "
                "dropping.", action,
            )
            return
        loop.call_soon_threadsafe(q.put_nowait, action)

    def request_stop(self) -> None:
        # Set the flag synchronously so any check between polls sees it
        # immediately, in addition to enqueuing the stop action.
        self.cancelled = True
        self.request_action("stop")


_PLAYER_ACTION_JS = r"""
    (action) => {
        // Walks the persistent player's shadow trees and clicks the button
        // matching the requested transport action. Player containers are
        // various custom elements ("music-player", "music-transport", etc.)
        // and the actual buttons live inside their shadow roots.
        const PLAYER_SEL = (
            'music-player, music-transport, music-horizontal-player, '
            + '[id*="TransportBar"], [class*="TransportBar"], '
            + '[class*="transportBar"]'
        );
        const matchers = {
            // play_pause: click whichever face is visible (they swap).
            play_pause: {
                aria: ['play', 'pause', 'playback'],
                icons: ['play', 'pause', 'playback'],
            },
            // pause_only: ONLY click if currently playing (pause button shown).
            pause_only: {
                aria: ['pause'],
                icons: ['pause'],
            },
            next: {
                aria: ['next', 'skip forward', 'skip next', 'forward'],
                icons: ['next', 'forward', 'skip-forward', 'skip_next',
                        'skip-next', 'fast-forward', 'fastforward'],
            },
            previous: {
                aria: ['previous', 'skip backward', 'skip previous',
                       'previous track', 'previous song', 'rewind',
                       'go back', 'skip back'],
                icons: ['previous', 'backward', 'skip-backward',
                        'skip_previous', 'skip-previous', 'rewind',
                        'prev'],
            },
        };
        const m = matchers[action];
        if (!m) return { hit: null, diag: [] };
        const ariaWanted = m.aria.map(s => s.toLowerCase());
        const iconWanted = m.icons.map(s => s.toLowerCase());

        const clickable = (el) => {
            const tag = (el.tagName || '').toLowerCase();
            if (tag === 'music-button' || tag === 'button') return true;
            const role = (el.getAttribute && el.getAttribute('role') || '').toLowerCase();
            return role === 'button';
        };
        const ariaOf = (el) => (el.getAttribute && el.getAttribute('aria-label') || '').toLowerCase();
        const iconOf = (el) => (el.getAttribute && el.getAttribute('icon-name') || '').toLowerCase();
        // Whole-word / whole-token matching so 'play' doesn't match
        // 'playlist' and 'back' doesn't match 'background'. Multi-word
        // keywords ('skip forward') match contiguous words.
        const tokenize = (s) => (s || '').split(/[^a-z0-9]+/).filter(Boolean);
        const containsPhrase = (text, phrase) => {
            const ph = tokenize(phrase);
            if (!ph.length) return false;
            const toks = tokenize(text);
            outer: for (let i = 0; i + ph.length <= toks.length; i++) {
                for (let j = 0; j < ph.length; j++) {
                    if (toks[i + j] !== ph[j]) continue outer;
                }
                return true;
            }
            return false;
        };
        const matches = (el) => {
            const aria = ariaOf(el);
            const icon = iconOf(el);
            for (const w of ariaWanted) {
                if (w && containsPhrase(aria, w)) return true;
            }
            for (const w of iconWanted) {
                if (w && containsPhrase(icon, w)) return true;
            }
            return false;
        };

        // Amazon's player swaps play/pause via twin buttons where the
        // inactive one is left in the DOM with display:none (or zero-size
        // / visibility:hidden). Calling .click() on the hidden one
        // succeeds at the JS level but does nothing visible — that's the
        // "play/pause appears frozen" symptom from the 2026-05-20 log.
        // Prefer visible matches; fall back to any match if nothing
        // visible matches (so we don't regress in DOM layouts we haven't
        // seen yet).
        const isVisible = (el) => {
            try {
                if (!el || !el.getBoundingClientRect) return false;
                const rect = el.getBoundingClientRect();
                if (rect.width === 0 && rect.height === 0) return false;
                const win = el.ownerDocument && el.ownerDocument.defaultView;
                if (win) {
                    const style = win.getComputedStyle(el);
                    if (style.display === 'none') return false;
                    if (style.visibility === 'hidden') return false;
                    if (parseFloat(style.opacity) === 0) return false;
                }
                return true;
            } catch (e) {
                return true; // fail-open
            }
        };

        // Walk a root collecting every clickable element (including across
        // shadow boundaries). Used both for matching and for the diagnostic
        // dump on failure.
        const collectClickables = (root, walked, out) => {
            if (!root || walked.has(root)) return;
            walked.add(root);
            const all = root.querySelectorAll ? root.querySelectorAll('*') : [];
            for (const el of all) {
                if (clickable(el)) out.push(el);
                if (el.shadowRoot) collectClickables(el.shadowRoot, walked, out);
            }
        };

        const findPlayers = (root, walked, out) => {
            if (!root || walked.has(root)) return;
            walked.add(root);
            try {
                const direct = root.querySelectorAll ? root.querySelectorAll(PLAYER_SEL) : [];
                for (const p of direct) out.push(p);
            } catch (e) { /* ignore */ }
            const all = root.querySelectorAll ? root.querySelectorAll('*') : [];
            for (const el of all) {
                if (el.shadowRoot) findPlayers(el.shadowRoot, walked, out);
            }
        };

        const players = [];
        findPlayers(document, new Set(), players);

        // Collect candidate buttons inside player containers first; fall back
        // to the whole document if no players were found.
        const candidates = [];
        const seen = new Set();
        const walked = new Set();
        if (players.length) {
            for (const p of players) {
                collectClickables(p, walked, candidates);
                if (p.shadowRoot) collectClickables(p.shadowRoot, walked, candidates);
            }
        } else {
            collectClickables(document, walked, candidates);
        }

        // Dedupe.
        const uniq = [];
        for (const el of candidates) {
            if (seen.has(el)) continue;
            seen.add(el);
            uniq.push(el);
        }

        // Two-pass click: prefer visible matches, fall back to any match.
        const tryClick = (el) => {
            try {
                el.click();
                const label = (el.getAttribute('aria-label')
                    || el.getAttribute('icon-name')
                    || 'match').toString().slice(0, 60);
                return { hit: label, diag: [] };
            } catch (e) {
                return null;
            }
        };
        for (const el of uniq) {
            if (matches(el) && isVisible(el)) {
                const r = tryClick(el);
                if (r) return r;
            }
        }
        for (const el of uniq) {
            if (matches(el)) {
                const r = tryClick(el);
                if (r) return r;
            }
        }

        // No match — return a diagnostic of what we saw so the caller can log
        // it and we can update the matchers. Cap to keep logs sane.
        const diag = [];
        for (const el of uniq.slice(0, 40)) {
            const tag = (el.tagName || '').toLowerCase();
            const aria = ariaOf(el);
            const icon = iconOf(el);
            if (!aria && !icon) continue;
            diag.push(`${tag}[aria=${aria || '-'},icon=${icon || '-'}]`);
        }
        return {
            hit: null,
            diag: diag,
            playerCount: players.length,
            candidateCount: uniq.length,
        };
    }
"""


async def _click_player_button(page, action: str) -> dict:
    """Click the requested transport button inside the persistent player.

    Returns a dict ``{"hit": str|None, "diag": [..], "playerCount": int,
    "candidateCount": int}``. ``hit`` is a short label of what got clicked, or
    None when nothing matched — in that case ``diag`` lists the aria-label /
    icon-name of every clickable element seen so we can diagnose why.
    """
    try:
        result = await page.evaluate(_PLAYER_ACTION_JS, action)
    except Exception as e:
        logger.exception("Player action '%s' failed", action)
        raise RuntimeError(f"Player action failed: {e}") from e
    if not isinstance(result, dict):
        return {"hit": result, "diag": []}
    return result


_SHUFFLE_OFF_JS = r"""
    () => {
        // Find the persistent-player shuffle toggle and ensure it's OFF.
        // Amazon's button aria-label describes the ACTION the button will
        // perform when clicked, not its current state:
        //   "Turn On Shuffle"   = currently OFF — leave alone.
        //   "Turn Off Shuffle"  = currently ON  — click to disable.
        // Restrict the walk to player containers so we don't grab the
        // playlist-page shuffle button (which is a "start shuffled"
        // launcher, not a session toggle).
        const PLAYER_SEL = (
            'music-player, music-transport, music-horizontal-player, '
            + '[id*="TransportBar"], [class*="TransportBar"], '
            + '[class*="transportBar"]'
        );
        const findPlayers = (root, walked, out) => {
            if (!root || walked.has(root)) return;
            walked.add(root);
            try {
                const direct = root.querySelectorAll
                    ? root.querySelectorAll(PLAYER_SEL) : [];
                for (const p of direct) out.push(p);
            } catch (e) { /* ignore */ }
            const all = root.querySelectorAll ? root.querySelectorAll('*') : [];
            for (const el of all) {
                if (el.shadowRoot) findPlayers(el.shadowRoot, walked, out);
            }
        };
        const collectButtons = (root, walked, out) => {
            if (!root || walked.has(root)) return;
            walked.add(root);
            const all = root.querySelectorAll ? root.querySelectorAll('*') : [];
            for (const el of all) {
                const tag = (el.tagName || '').toLowerCase();
                if (tag === 'button' || tag === 'music-button') out.push(el);
                if (el.shadowRoot) collectButtons(el.shadowRoot, walked, out);
            }
        };
        const players = [];
        findPlayers(document, new Set(), players);
        const candidates = [];
        const walked = new Set();
        if (players.length) {
            for (const p of players) {
                collectButtons(p, walked, candidates);
                if (p.shadowRoot) collectButtons(p.shadowRoot, walked, candidates);
            }
        } else {
            collectButtons(document, walked, candidates);
        }
        const seen = new Set();
        let target = null;
        for (const el of candidates) {
            if (seen.has(el)) continue;
            seen.add(el);
            const aria = (
                (el.getAttribute && el.getAttribute('aria-label')) || ''
            ).toLowerCase();
            if (aria === 'turn on shuffle' || aria === 'turn off shuffle') {
                target = el;
                break;
            }
        }
        if (!target) {
            return {
                found: false, clicked: false, before: null,
                playerCount: players.length,
            };
        }
        const before = (target.getAttribute('aria-label') || '').toLowerCase();
        if (before === 'turn off shuffle') {
            try {
                target.click();
                return { found: true, clicked: true, before: before };
            } catch (e) {
                return {
                    found: true, clicked: false, before: before,
                    error: String(e),
                };
            }
        }
        return { found: true, clicked: false, before: before };
    }
"""


async def _ensure_shuffle_off(page) -> dict:
    """Ensure Amazon's persistent-player shuffle toggle is OFF.

    Returns ``{"found": bool, "clicked": bool, "before": str|None}``.
    ``clicked=True`` means the toggle was on and we just disabled it;
    ``found=False`` means the player bar wasn't on the page yet (e.g.
    nothing has played this session) and the caller should retry later.
    """
    try:
        result = await page.evaluate(_SHUFFLE_OFF_JS)
    except Exception as e:
        logger.exception("_ensure_shuffle_off failed")
        return {"found": False, "clicked": False, "error": str(e)}
    if not isinstance(result, dict):
        return {"found": False, "clicked": False, "raw": result}
    return result


_SEEK_FIND_JS = r"""
    () => {
        const PLAYER_HINT = /(player|transport|now[-_ ]?playing|playback|footer)/i;
        const seen = new Set();
        const deepWalkAll = (root, fn) => {
            if (!root || seen.has(root)) return;
            seen.add(root);
            const all = root.querySelectorAll ? root.querySelectorAll('*') : [];
            for (const el of all) {
                fn(el);
                if (el.shadowRoot) deepWalkAll(el.shadowRoot, fn);
            }
        };
        const findPlayerAncestor = (el) => {
            let cur = el; let hops = 0;
            while (cur && hops++ < 40) {
                const tag = (cur.tagName || '').toLowerCase();
                const id = cur.id || '';
                const cls = (cur.getAttribute && cur.getAttribute('class')) || '';
                const aria = (cur.getAttribute && cur.getAttribute('aria-label')) || '';
                if (PLAYER_HINT.test(tag) || PLAYER_HINT.test(id)
                    || PLAYER_HINT.test(cls) || PLAYER_HINT.test(aria)) {
                    return true;
                }
                cur = cur.parentNode;
                if (cur && cur.host) cur = cur.host;
            }
            return false;
        };
        const sliders = [];
        deepWalkAll(document, (el) => {
            if (!el.getAttribute) return;
            const role = (el.getAttribute('role') || '').toLowerCase();
            const tag = (el.tagName || '').toLowerCase();
            const type = (el.getAttribute('type') || '').toLowerCase();
            const isSlider = role === 'slider'
                || (tag === 'input' && type === 'range');
            if (!isSlider) return;
            let n = NaN, m = NaN;
            const now = el.getAttribute('aria-valuenow');
            const max = el.getAttribute('aria-valuemax');
            if (now != null && max != null) {
                n = parseFloat(now); m = parseFloat(max);
            }
            if ((!isFinite(n) || !isFinite(m)) && 'value' in el && 'max' in el) {
                n = parseFloat(el.value); m = parseFloat(el.max);
            }
            if (!(isFinite(m) && m > 0)) return;
            sliders.push({ el, dur: m, kind: tag === 'input' ? 'input' : 'aria' });
        });
        const ranked = sliders.filter(s => findPlayerAncestor(s.el));
        const pick = ranked[0] || sliders[0];
        if (!pick) return { ok: false, error: 'no-slider' };
        const r = pick.el.getBoundingClientRect();
        return {
            ok: true,
            kind: pick.kind,
            duration: pick.dur,
            rect: { x: r.x, y: r.y, width: r.width, height: r.height },
        };
    }
"""


async def _seek_player(page, target_seconds: int) -> dict:
    """Find the playback slider and seek to ``target_seconds``.

    Strategy: locate the slider, then click on its track at the x-coordinate
    that corresponds to ``target_seconds``. Works for both ``<input
    type="range">`` and elements with ``role="slider"``.
    """
    info = await page.evaluate(_SEEK_FIND_JS)
    if not info or not info.get("ok"):
        return {
            "ok": False,
            "error": (info or {}).get("error", "slider lookup failed"),
        }
    rect = info["rect"]
    duration = float(info["duration"])
    if duration <= 0 or rect["width"] <= 0:
        return {"ok": False, "error": f"bad geometry (dur={duration}, w={rect['width']})"}
    target = max(0, min(target_seconds, duration))
    frac = target / duration
    # Inset a couple of pixels from the edges so we don't land outside the
    # slider's hit-test area.
    inset = 2 if rect["width"] > 8 else 0
    x = rect["x"] + inset + frac * (rect["width"] - 2 * inset)
    y = rect["y"] + rect["height"] / 2
    try:
        await page.mouse.click(x, y)
    except Exception as e:
        return {"ok": False, "error": f"click failed: {e}"}
    return {"ok": True, "mode": info["kind"]}


# --- Chrome per-app volume control via pycaw -------------------------------
# Reading/writing the underlying <audio> element didn't reach the speakers
# reliably (Amazon's player likely uses Web Audio / MSE on top), so we drive
# Chrome's Windows audio session directly. Per-app volume is what shows up
# in Settings → System → Sound → Volume mixer.

def _chrome_audio_controls() -> list:
    """Enumerate Windows audio sessions and return the ISimpleAudioVolume
    controls for every chrome.exe session. Empty list if none found or pycaw
    is unavailable."""
    try:
        from pycaw.pycaw import AudioUtilities, ISimpleAudioVolume
    except ImportError:
        logger.warning(
            "pycaw not installed — volume slider disabled. "
            "Run: pip install pycaw"
        )
        return []
    try:
        sessions = AudioUtilities.GetAllSessions()
    except Exception as e:
        logger.warning("Could not enumerate audio sessions: %s", e)
        return []
    controls = []
    for session in sessions:
        try:
            proc = session.Process
            if proc and proc.name().lower() == "chrome.exe":
                ctl = session._ctl.QueryInterface(ISimpleAudioVolume)
                controls.append(ctl)
        except Exception:
            continue
    return controls


def _get_chrome_volume() -> float | None:
    """Return Chrome's session volume in 0.0..1.0, or None if no session."""
    for ctl in _chrome_audio_controls():
        try:
            return float(ctl.GetMasterVolume())
        except Exception:
            continue
    return None


def _set_chrome_volume(target: float) -> int:
    """Set every Chrome audio session's master volume to ``target`` (0..1).
    Returns the number of sessions successfully written."""
    target = max(0.0, min(1.0, target))
    n = 0
    for ctl in _chrome_audio_controls():
        try:
            ctl.SetMasterVolume(target, None)
            if target > 0:
                try:
                    ctl.SetMute(False, None)
                except Exception:
                    pass
            n += 1
        except Exception:
            continue
    return n


async def _list_playlists_coroutine(page) -> dict:
    """Scrape the user's full playlist list from Amazon Music. Saves to the
    playlist cache (`./output/playlists.json`) as a side-effect."""
    from auth import _detect_logged_in
    from config import BASE_URL, LONG_TIMEOUT
    from scraper import list_playlists

    await page.goto(
        BASE_URL, wait_until="domcontentloaded", timeout=LONG_TIMEOUT
    )
    await page.wait_for_timeout(1500)
    if not await _detect_logged_in(page):
        raise RuntimeError(
            "Not logged in to Amazon Music. Log in via the Chrome window, "
            "then click 'Fetch from Amazon' again."
        )
    playlists = await list_playlists(page)
    return {"count": len(playlists), "urls": [p["url"] for p in playlists]}


def _build_play_coroutine(
    name: str,
    url: str,
    shuffle: bool,
    controller: PlaybackController,
):
    """Single-playlist convenience wrapper around :func:`_build_queue_coroutine`."""
    return _build_queue_coroutine(
        [{"name": name, "url": url}], shuffle, controller,
    )


def _build_queue_coroutine(
    items: list[dict],
    shuffle: bool,
    controller: PlaybackController,
    shuffle_queue: bool = False,
):
    """Return ``async fn(page)`` that plays *items* in order, advancing to the
    next when the current playlist finishes (detected via
    :func:`scraper.wait_until_off_playlist`) or when a ``skip_playlist``
    action arrives. Each item is a ``{"name": str, "url": str}`` dict.

    When *shuffle_queue* is True the playlist order is randomised before
    playback starts (tracks within each playlist still play in the order
    determined by *shuffle*).

    The same poll loop emits track / progress / status updates as the original
    single-playlist version. Stop, play_pause, next, previous, seek,
    skip_playlist, and prev_playlist actions are consumed from the
    controller's queue.
    """
    items = list(items)
    if shuffle_queue and len(items) > 1:
        random.shuffle(items)

    async def _do_play(page):
        from auth import _detect_logged_in, _suppress_dialogs
        from config import BASE_URL, LONG_TIMEOUT
        from scraper import (
            get_now_playing, play_playlist, scrape_playlist,
            wait_until_off_playlist,
        )

        loop = asyncio.get_running_loop()
        controller._loop = loop
        controller._action_queue = asyncio.Queue()
        action_queue = controller._action_queue
        total = len(items)

        # One-time login / dialog suppression up front so the per-item loop
        # only deals with playlist-specific work.
        controller.status.emit("Checking Amazon Music login…")
        await page.goto(
            BASE_URL, wait_until="domcontentloaded", timeout=LONG_TIMEOUT
        )
        await page.wait_for_timeout(1500)
        if controller.cancelled:
            return {"name": items[0]["name"] if items else "",
                    "stopped_by_user": True, "completed": 0}
        if not await _detect_logged_in(page):
            raise RuntimeError(
                "Not logged in to Amazon Music. Log in via the Chrome window, "
                "then try Play again."
            )
        await _suppress_dialogs(page)

        last_track: tuple[str, str] | None = None
        completed = 0
        last_played_name = ""

        queue_pos = 0  # 0-based; supports going backwards for prev_playlist
        while queue_pos < total and not controller.cancelled:
            idx = queue_pos + 1  # 1-based for display / logging
            item = items[queue_pos]

            name = item.get("name") or "Playlist"
            url = item.get("url") or ""
            last_played_name = name
            is_last = (idx == total)

            controller.playlist_advanced.emit(idx, total, name)

            # Auto-advance needs a track set to compare against. Skip the
            # scrape on the last item — there's nothing to advance to.
            track_set: set[tuple[str, str]] = set()
            if not is_last:
                controller.status.emit(
                    f"[{idx}/{total}] Loading tracks for '{name}'…"
                )
                try:
                    playlist = await scrape_playlist(page, playlist_url=url)
                    track_set = {
                        ((s.title or "").lower().strip(),
                         (s.artist or "").lower().strip())
                        for s in playlist.songs
                    }
                    controller.log.emit(
                        f"[{idx}/{total}] {len(track_set)} tracks loaded "
                        f"for '{name}'."
                    )
                except Exception as e:
                    controller.log.emit(
                        f"Couldn't scrape '{name}' for auto-advance: {e}. "
                        "Use ⏩ to skip when this playlist finishes."
                    )
                if controller.cancelled:
                    break

            controller.status.emit(f"[{idx}/{total}] Opening '{name}'…")
            open_failed = False
            try:
                fired = await play_playlist(
                    page, playlist_url=url, shuffle=shuffle
                )
                controller.log.emit(
                    f"[{idx}/{total}] Started playback ({fired})."
                )
            except RuntimeError as e:
                # Playlist opened but auto-click failed. Don't bail — the user
                # can press Play in the browser and we'll pick it up.
                controller.log.emit(
                    f"Couldn't auto-start '{name}': {e}. Click Play in the "
                    "browser window — this view will pick up the track."
                )
            except Exception as e:
                controller.log.emit(
                    f"Couldn't open '{name}': {e}. Skipping."
                )
                open_failed = True

            if open_failed:
                queue_pos += 1
                continue

            # Background task that signals end-of-playlist by completing.
            advance_task: asyncio.Task | None = None
            if not is_last and track_set:
                advance_task = asyncio.create_task(
                    wait_until_off_playlist(page, track_set)
                )

            # Reset per-playlist track display so the next track gets logged
            # fresh even if Amazon resumes a track with the same title.
            last_track = None
            empty_polls = 0
            diag_logged = False
            should_advance = False
            go_back = False

            while not controller.cancelled and not should_advance and not go_back:
                # 1. Read the player bar.
                np = None
                np_err: str | None = None
                try:
                    np = await asyncio.wait_for(
                        get_now_playing(page), timeout=5.0
                    )
                except asyncio.TimeoutError:
                    np_err = "timeout (>5s)"
                except Exception as e:
                    np_err = f"{type(e).__name__}: {e}"
                if controller.cancelled:
                    break
                if np and np.get("title"):
                    empty_polls = 0
                    diag_logged = False
                    title = (np.get("title") or "").strip()
                    artist = (np.get("artist") or "").strip()
                    album = (np.get("album") or "").strip()
                    track = (title, artist)
                    if track != last_track:
                        controller.track_changed.emit(title, artist)
                        controller.track_meta.emit(title, artist, album)
                        controller.log.emit(
                            f"Now playing: {title}"
                            + (f" — {artist}" if artist else "")
                            + f" (via {np.get('source', '?')})"
                        )
                        last_track = track
                    else:
                        controller.track_meta.emit(title, artist, album)
                    pos = np.get("position")
                    dur = np.get("duration")
                    if pos is not None and dur is not None:
                        controller.progress.emit(int(pos), int(dur))
                    else:
                        controller.progress.emit(0, 0)
                else:
                    empty_polls += 1
                    if empty_polls == 5 and last_track is None:
                        controller.status.emit(
                            "No track detected yet — click Play in the "
                            "browser if it didn't auto-start."
                        )
                    if not diag_logged and last_track is None:
                        diag_logged = True
                        if np_err is not None:
                            controller.log.emit(
                                f"now-playing: read failed ({np_err})"
                            )
                        elif isinstance(np, dict) and np.get("diag"):
                            for line in np["diag"][:5]:
                                controller.log.emit(
                                    f"now-playing diag: {line}"
                                )
                        else:
                            controller.log.emit(
                                f"now-playing: no track and no diag "
                                f"(np={np!r})"
                            )

                # 2. Auto-advance check: did the off-playlist task fire?
                if advance_task is not None and advance_task.done():
                    try:
                        reason = advance_task.result()
                    except Exception as e:
                        reason = f"error:{e}"
                    controller.log.emit(
                        f"[{idx}/{total}] '{name}' finished "
                        f"(reason: {reason}). Advancing…"
                    )
                    should_advance = True
                    advance_task = None
                    break

                # 3. Wait up to ~2s for an action; on timeout, poll again.
                try:
                    action = await asyncio.wait_for(
                        action_queue.get(), timeout=2.0
                    )
                except asyncio.TimeoutError:
                    continue

                # 4. Process this action and any others queued behind it.
                while action is not None:
                    if action == "stop":
                        controller.cancelled = True
                        try:
                            await _click_player_button(page, "pause_only")
                        except Exception as e:
                            controller.log.emit(
                                f"Pause-on-stop failed: {e}"
                            )
                        break
                    elif action == "prev_playlist":
                        if queue_pos > 0:
                            controller.log.emit(
                                f"[{idx}/{total}] ⏪ Going back to previous "
                                "playlist."
                            )
                            queue_pos -= 1
                            go_back = True
                        else:
                            controller.log.emit(
                                "Already at the first playlist."
                            )
                        break
                    elif action == "skip_playlist":
                        if is_last:
                            controller.log.emit(
                                "Skip pressed on last playlist — stopping."
                            )
                            controller.cancelled = True
                            try:
                                await _click_player_button(
                                    page, "pause_only"
                                )
                            except Exception as e:
                                controller.log.emit(
                                    f"Pause-on-skip-end failed: {e}"
                                )
                        else:
                            controller.log.emit(
                                f"[{idx}/{total}] Skip → advancing to next "
                                "playlist."
                            )
                            should_advance = True
                        break
                    elif action in ("play_pause", "next", "previous"):
                        try:
                            result = await _click_player_button(page, action)
                            hit = result.get("hit")
                            if hit:
                                controller.log.emit(
                                    f"Clicked {action} ({hit})."
                                )
                            else:
                                diag = result.get("diag") or []
                                pc = result.get("playerCount")
                                cc = result.get("candidateCount")
                                summary = (
                                    f"Couldn't find a {action} button "
                                    f"(players={pc}, candidates={cc})."
                                )
                                if diag:
                                    summary += (
                                        " Saw: " + " | ".join(diag[:20])
                                    )
                                controller.log.emit(summary)
                        except Exception as e:
                            controller.log.emit(f"{action} failed: {e}")
                    elif (
                        isinstance(action, tuple)
                        and len(action) == 2
                        and action[0] == "seek"
                    ):
                        target = int(action[1])
                        try:
                            result = await _seek_player(page, target)
                            if result.get("ok"):
                                controller.log.emit(
                                    f"Seeked to {target}s "
                                    f"({result.get('mode', '?')})."
                                )
                            else:
                                controller.log.emit(
                                    f"Seek to {target}s failed: "
                                    f"{result.get('error', 'unknown')}"
                                )
                        except Exception as e:
                            controller.log.emit(f"Seek failed: {e}")
                    else:
                        controller.log.emit(
                            f"Unknown action ignored: {action!r}"
                        )
                    # Drain any other actions queued behind this one.
                    try:
                        action = action_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        action = None

            # Tear down the auto-advance task before moving on (or exiting).
            if advance_task is not None and not advance_task.done():
                advance_task.cancel()
                try:
                    await advance_task
                except (asyncio.CancelledError, Exception):
                    pass

            if go_back:
                # queue_pos already decremented; loop will re-enter that item.
                continue

            completed += 1
            if controller.cancelled:
                break
            queue_pos += 1

        logger.info(
            "Playback loop exited (cancelled=%s, completed=%d/%d)",
            controller.cancelled, completed, total,
        )
        return {
            "name": last_played_name,
            "stopped_by_user": True,
            "completed": completed,
            "total": total,
        }

    return _do_play


# ---------------------------------------------------------------------------
# Classical Shuffle playback coroutine
# ---------------------------------------------------------------------------
def _classical_perf_match(np: dict | None, perf: "Performance"):
    """Decide whether the now-playing track *np* belongs to *perf*.

    Returns True (in performance), False (definitely outside), or None
    (transient — caller should not change state on this poll).

    Matching is title-membership against ``perf.titles`` plus an artist
    word-set sanity check. We deliberately do NOT match on album because
    Amazon's player widget reports the *playlist name* as the album field
    (not the source album), so player-album never matches xlsx-album.

    Artists are compared as word-sets in BOTH directions: the smaller set
    must be a subset of the larger. This handles two real failure modes:
      • Amazon reorders multi-artist credits (xlsx 'A, B, C & D' vs player
        'A, C, D & B') — sets ignore order.
      • Track-level credits drop album-level collaborators (e.g., a violin
        concerto's slow movement on a mixed album credits only violinist +
        orchestra, not the pianist who plays elsewhere on the album). Then
        np_words ⊊ perf_words. The reverse — player adding a composer like
        'Ludwig van Beethoven' to a per-track credit — gives perf_words ⊊
        np_words. Either direction must pass.
    """
    if not np or not (np.get("title") or "").strip():
        return None  # nothing playing — transient
    np_title = (np.get("title") or "").strip().lower()
    if not perf.titles:
        # Pool builder couldn't read titles — fall back to artist alone.
        perf_titles_set: set[str] = set()
    else:
        perf_titles_set = set(perf.titles)
    if perf_titles_set and np_title not in perf_titles_set:
        return False

    np_artist = (np.get("artist") or "").strip()
    perf_artist = (perf.artist or "").strip()
    if perf_artist and not np_artist:
        return None  # artist not yet populated; let it settle

    # Prefer the precomputed union of artist tokens across the run (set by
    # the grouper). Falls back to tokenizing the perf.artist string when
    # absent — covers older callers / unit tests that construct Performance
    # directly without populating artist_tokens.
    perf_phrases = perf.artist_tokens or _artist_tokens(perf_artist)
    np_phrases = _artist_tokens(np_artist)
    if perf_phrases and np_phrases:
        # Non-empty PHRASE intersection. The xlsx artist field is often
        # sparse ("Berliner Philharmoniker" alone) while the player adds
        # conductor + composer ("Berliner Philharmoniker, Herbert von
        # Karajan & Ludwig van Beethoven"). Bidirectional subset rejects
        # this (perf has Janowitz from a later mvt, np adds Karajan and
        # Beethoven — neither subset of the other). Phrase-level
        # intersection requires at least one full-name match, which catches
        # the orchestra/conductor/soloist phrase shared between the xlsx
        # row and the player credit. Unrelated artists like 'Some Pianist'
        # vs 'Different Pianist' share no full-name phrase → reject.
        if not (perf_phrases & np_phrases):
            return False
    return True


class PickResult(NamedTuple):
    """Return value of `_pick_next_perf`. Caller is responsible for:
      - Pushing the previous `current_perf` to history when
        `came_from_back` is False (when True, the ⏪ handler already
        moved current_perf onto `future`).
      - Emitting `log_msg` if non-None.
      - Skipping the recency push when `is_replay` is True.
      - Breaking the loop when `stop_reason` is not None.
    """
    perf: "Performance | None"
    is_replay: bool
    came_from_back: bool
    log_msg: str | None
    stop_reason: str | None  # 'pool_empty' if pool is exhausted, else None


def _pick_next_perf(
    *,
    replay_perf: "Performance | None",
    future: "deque[Performance]",
    pool: list,
    recency: "deque[tuple[str, str, str]] | None",
) -> PickResult:
    """Pure pick logic for the Classical Shuffle loop.

    Mutates `future` in place (drains dropped perfs off the top + pops
    on a forward walk) but has no other side effects, so it's directly
    testable. See `test_classical_shuffle_pick.py`.

    Priority:
      1. `replay_perf` set (⏪) → backward replay. came_from_back=True;
         caller skips the history push since the ⏪ handler already
         pushed current_perf to future.
      2. `future` non-empty → forward walk (⏩ or natural-end). Treated
         as a replay for recency purposes (perf was recently played).
      3. Random pick from pool minus recency window.

    Returns a PickResult — see its docstring for caller responsibilities.
    """
    if replay_perf is not None:
        return PickResult(
            perf=replay_perf,
            is_replay=True,
            came_from_back=True,
            log_msg=None,
            stop_reason=None,
        )
    # Drain dropped perfs off the top of future (a perf might have
    # been bailout-removed from pool while we were on a ⏪ replay).
    while future and future[-1] not in pool:
        future.pop()
    if future:
        perf = future.pop()
        return PickResult(
            perf=perf,
            is_replay=True,
            came_from_back=False,
            log_msg=(
                f"  Walking forward to: "
                f"{perf.album or '(no album)'} — "
                f"{perf.artist or '(no artist)'} "
                f"({len(future)} more queued)"
            ),
            stop_reason=None,
        )
    if not pool:
        return PickResult(
            perf=None,
            is_replay=False,
            came_from_back=False,
            log_msg=None,
            stop_reason="pool_empty",
        )
    recent_set = set(recency) if recency is not None else set()
    eligible = [
        p for p in pool
        if (p.playlist_url, p.artist.lower(), p.album.lower())
        not in recent_set
    ]
    if not eligible:
        eligible = pool  # capped window; should not happen
    return PickResult(
        perf=random.choice(eligible),
        is_replay=False,
        came_from_back=False,
        log_msg=None,
        stop_reason=None,
    )


def _build_classical_shuffle_coroutine(
    performances: list["Performance"],
    folder_label: str,
    controller: PlaybackController,
):
    """Return ``async fn(page)`` that loops Classical Shuffle indefinitely.

    Each iteration: pick a random performance not in the recency window,
    navigate to its playlist, scrape the live tracklist (so reorders since
    export are tolerated), find the row for the performance's (artist, album),
    click it via `scroll_and_click_row_at_index`, then poll `get_now_playing`
    until the current track's (artist, album) drifts off the performance —
    then advance.

    Stop, play_pause, next, previous, and seek transport actions are honored
    from the controller's queue; prev_playlist / skip_playlist are not
    meaningful here (no fixed queue) and are ignored with a log line.
    """
    pool = list(performances)

    async def _do_play(page):
        from auth import _detect_logged_in, _suppress_dialogs
        from config import BASE_URL, LONG_TIMEOUT
        from scraper import (
            get_now_playing, scroll_and_click_and_detect_substitution,
        )
        import web_restricted

        loop = asyncio.get_running_loop()
        controller._loop = loop
        controller._action_queue = asyncio.Queue()
        action_queue = controller._action_queue

        controller.status.emit("Checking Amazon Music login…")
        await page.goto(
            BASE_URL, wait_until="domcontentloaded", timeout=LONG_TIMEOUT
        )
        await page.wait_for_timeout(1500)
        if controller.cancelled:
            return {"performances_played": 0, "stopped_by_user": True}
        if not await _detect_logged_in(page):
            raise RuntimeError(
                "Not logged in to Amazon Music. Log in via the Chrome window, "
                "then try again."
            )
        await _suppress_dialogs(page)

        # Disable Amazon's player-session shuffle if it's currently on —
        # otherwise our row-clicks get absorbed into a shuffled session and
        # the player picks tracks at random from each playlist instead of
        # the specific row we asked for.
        try:
            sh = await _ensure_shuffle_off(page)
            if sh.get("clicked"):
                controller.log.emit(
                    "Amazon player shuffle was on — disabled it."
                )
                logger.info(
                    "classical-shuffle: Amazon shuffle toggle disabled "
                    "at startup."
                )
            elif sh.get("found"):
                logger.info(
                    "classical-shuffle: Amazon shuffle already off (%s).",
                    sh.get("before"),
                )
            else:
                logger.info(
                    "classical-shuffle: Amazon shuffle button not found "
                    "yet (playerCount=%s) — will assume off.",
                    sh.get("playerCount"),
                )
        except Exception as e:
            logger.warning(
                "classical-shuffle: shuffle-off probe failed: %s", e
            )

        # Recency: never re-pick a performance until this many others have
        # played. Aim for ~3/4 of the pool so a small library still gives good
        # variety; cap so we never deadlock and never wait on >50 picks.
        max_window = max(0, min(len(pool) * 3 // 4, 50, len(pool) - 1))
        recency: deque[tuple[str, str, str]] | None = (
            deque(maxlen=max_window) if max_window > 0 else None
        )

        played = 0
        last_summary = ""
        # Multi-step history so ⏪ (prev_playlist) can walk backwards through
        # past picks. Bounded so memory stays sane during long sessions.
        history: deque["Performance"] = deque(maxlen=20)
        # Forward stack — when ⏪ rewinds past a perf, that perf is pushed
        # here so ⏩ (or a natural end-of-perf advance) can walk forward
        # back through what was just rewound, like a browser forward
        # button. Drains as the user walks forward; new random picks
        # happen only after future is empty. Bug surfaced 2026-05-24:
        # user pressed ⏪⏪⏪ then ⏩, got a new random pick instead of
        # walking forward back through the just-rewound perfs.
        future: deque["Performance"] = deque(maxlen=20)
        current_perf: "Performance | None" = None
        # If set, the next iteration uses this perf instead of a fresh random
        # pick (and skips the recency push). Used by the ⏪ handler.
        replay_perf: "Performance | None" = None

        while not controller.cancelled:
            # 1. Decide the next perf via the pure helper. See
            # `_pick_next_perf` docstring above.
            pick = _pick_next_perf(
                replay_perf=replay_perf,
                future=future,
                pool=pool,
                recency=recency,
            )
            if pick.stop_reason == "pool_empty":
                controller.log.emit(
                    "All performances in this folder are unavailable "
                    "(catalog-delisted). Stopping."
                )
                break
            assert pick.perf is not None  # stop_reason guards the None case
            perf = pick.perf
            replay_perf = None  # consumed (if it was set)
            is_replay = pick.is_replay
            came_from_back = pick.came_from_back
            if pick.log_msg:
                controller.log.emit(pick.log_msg)
            # Push the just-finished perf onto history so ⏪ pressed during
            # *this* perf walks back to it. Skip when we came from ⏪
            # (that handler already moved current_perf onto `future`,
            # not history). Skip the very first pick (nothing to push
            # yet). Skip perfs that got dropped from the pool by the
            # bailout below (we don't want ⏪ to revive known-broken
            # perfs — see Bug 1 in the 2026-05-20 log).
            if (not came_from_back and current_perf is not None
                    and current_perf in pool):
                history.append(current_perf)
            current_perf = perf
            played += 1
            summary = (
                f"Performance {played}: "
                f"{perf.album or '(no album)'} — "
                f"{perf.artist or '(no artist)'} "
                f"({perf.track_count} trk, "
                f"from '{perf.playlist_name}')"
            )
            last_summary = summary
            controller.status.emit(summary)
            controller.log.emit(f"▶ {summary}")
            controller.playlist_advanced.emit(played, 0, perf.playlist_name)
            logger.info(
                "classical-shuffle: starting perf %d — %s — %s "
                "(start_row=%d, %d tracks). Expected titles: %s",
                played, perf.artist, perf.album, perf.start_row,
                perf.track_count, list(perf.titles),
            )

            # 2. Navigate + wait for tracks to render.
            try:
                await page.goto(
                    perf.playlist_url,
                    wait_until="domcontentloaded",
                    timeout=LONG_TIMEOUT,
                )
                await page.wait_for_selector(
                    'music-image-row[primary-text]',
                    state="attached", timeout=LONG_TIMEOUT,
                )
                await page.wait_for_timeout(2500)
            except Exception as e:
                controller.log.emit(
                    f"Couldn't open '{perf.playlist_name}': {e}. "
                    "Skipping this performance."
                )
                continue
            if controller.cancelled:
                break

            # 3. Trust the xlsx-derived start_row directly — skipping the
            #    live tracklist scrape (which costs ~30–60s for a 9-track
            #    playlist via _collect_all_tracks). If the playlist was
            #    edited since export, the (artist, album) verification in
            #    the polling loop below will detect a wrong click via the
            #    "never entered performance" 30s bailout, and we'll move on.
            start_idx = perf.start_row

            # 4. Click the start row + watch the playLibraryPlaylist API
            # response for an ASIN substitution. If Amazon's backend
            # silently returns a different ASIN than we asked for, this
            # row is web-restricted (the desktop app would play it fine,
            # but the web player can't). See feedback_amazon_music_dom.md
            # Trap 4 for the full investigation. We record it to
            # web_restricted.json so the spreadsheet detail view can flag
            # the row and the user can choose to remove it.
            click_result = await scroll_and_click_and_detect_substitution(
                page, start_idx,
            )
            ok = click_result.get("ok", False)
            if not ok:
                # Row-click failed at the DOM level (couldn't even reach
                # the music-playback-button). Same conceptual failure as
                # the 10s "couldn't lock onto" bailout further below: this
                # perf is unplayable as scored by the xlsx (most often
                # because the xlsx is stale — rows were deleted from the
                # playlist since the last export). Drop from the pool so
                # random picks AND ⏪ replays both stop revisiting it. The
                # push-side `current_perf in pool` guard then keeps it off
                # the history on the next iteration. Without this, the
                # same broken perf cascades via ⏪ (see the 2026-05-20
                # evening log: Sanderling/Gilels PC1 row 7 hit at perfs 4,
                # 7, 9).
                controller.log.emit(
                    f"  Couldn't click row {start_idx + 1} in "
                    f"'{perf.playlist_name}' (playlist may be shorter "
                    "than the cached xlsx — try re-exporting). "
                    "Dropping from pool, advancing."
                )
                logger.info(
                    "classical-shuffle: dropping perf (%s, %s) from "
                    "pool — row-click failed (xlsx start_row=%d, "
                    "playlist=%s)",
                    perf.artist, perf.album, start_idx,
                    perf.playlist_name,
                )
                try:
                    pool.remove(perf)
                except ValueError:
                    pass
                continue

            # Fast-fail on confirmed ASIN substitution. This saves ~10s
            # vs the get_now_playing bailout below — and gives us the
            # ASIN pair to persist for the spreadsheet flag.
            if click_result.get("substituted") is True:
                req_asin = click_result.get("requested_asin") or ""
                sub_asin = click_result.get("substituted_asin") or ""
                expected_title = perf.titles[0] if perf.titles else ""
                controller.log.emit(
                    f"  '{perf.album}' by {perf.artist} is web-restricted "
                    f"on Amazon Music (the same track plays fine in the "
                    f"Windows desktop app). Recorded for review; "
                    "advancing."
                )
                logger.info(
                    "classical-shuffle: web-restricted ASIN — requested "
                    "%s, Amazon substituted %s. Playlist=%s row=%d "
                    "title=%r artist=%r. Dropping from pool.",
                    req_asin, sub_asin, perf.playlist_name,
                    start_idx + 1, expected_title, perf.artist,
                )
                try:
                    web_restricted.add(
                        playlist_url=perf.playlist_url,
                        playlist_name=perf.playlist_name,
                        row_index=start_idx + 1,
                        title=expected_title,
                        artist=perf.artist,
                        album=perf.album,
                        requested_asin=req_asin,
                        substituted_asin=sub_asin,
                    )
                except Exception:
                    logger.exception(
                        "classical-shuffle: web_restricted.add failed "
                        "(non-fatal, continuing)"
                    )
                try:
                    pool.remove(perf)
                except ValueError:
                    pass
                continue

            # 5. Mark this performance as played in the recency window even
            # before the inner watch — so a stop or scrape failure won't cause
            # immediate replays if the user resumes. Skip the push for ⏪
            # replays so we don't lengthen the recency window with the
            # replay itself.
            if recency is not None and not is_replay:
                recency.append(
                    (perf.playlist_url, perf.artist.lower(), perf.album.lower())
                )

            # 6. Poll now-playing; advance when the track leaves this perf.
            last_track: tuple[str, str] | None = None
            in_perf = False
            mismatch_streak = 0
            # If we never lock onto a track that matches `perf`, bail out
            # after ENTERED_TIMEOUT_S. Two known causes:
            #   • Catalog-delisted row → Amazon silently falls back to row 1
            #     (see feedback_amazon_music_dom.md Trap 4).
            #   • Per-track credits drift from album-level credits in ways
            #     `_classical_perf_match` still rejects (rare since the
            #     2026-05-20 bidirectional artist fix, but possible).
            # ~5 polls at our 2s cadence is enough to confirm past the ~2s
            # click-to-update lag. On bailout we ALSO drop this perf from
            # the pool so future picks skip it for the rest of the session.
            ENTERED_TIMEOUT_S = 10.0
            entered_deadline = loop.time() + ENTERED_TIMEOUT_S
            advance = False
            # Diagnostic: dedupe per-poll "not yet in perf" logs by title so
            # we don't spam the same line ~once per second.
            diag_logged_titles: set[str] = set()

            while not controller.cancelled and not advance:
                np: dict | None = None
                try:
                    np = await asyncio.wait_for(
                        get_now_playing(page), timeout=5.0
                    )
                except asyncio.TimeoutError:
                    pass
                except Exception:
                    pass
                if controller.cancelled:
                    break

                # Decide whether we're still in this performance — we use
                # this BEFORE updating the dock so the lag-poll tracks (the
                # song the player widget was showing for the previous click,
                # plus any transitional in-between state) don't flash up in
                # the Now Playing display while we're waiting for Amazon to
                # catch up to the row we just clicked.
                m = _classical_perf_match(np, perf)
                if m is True:
                    if not in_perf:
                        np_title = (np.get("title") if np else "") or ""
                        logger.info(
                            "classical-shuffle: entered perf — title=%r",
                            np_title,
                        )
                    in_perf = True
                    mismatch_streak = 0
                elif m is False:
                    np_title = (np.get("title") if np else "") or ""
                    np_artist = (np.get("artist") if np else "") or ""
                    if in_perf:
                        mismatch_streak += 1
                        if mismatch_streak == 1:
                            logger.info(
                                "classical-shuffle: title left perf — "
                                "title=%r artist=%r",
                                np_title, np_artist,
                            )
                        if mismatch_streak >= 2:
                            controller.log.emit(
                                f"  Performance ended (track drifted off "
                                f"({perf.album})). Advancing…"
                            )
                            advance = True
                            break
                    else:
                        # Haven't entered the performance yet — log unique
                        # titles seen so we can diagnose any title mismatch
                        # without spamming the same line every 2s.
                        if np_title and np_title not in diag_logged_titles:
                            diag_logged_titles.add(np_title)
                            np_album = (np.get("album") if np else "") or ""
                            logger.info(
                                "classical-shuffle: not yet in perf — "
                                "expected one of %s by artist=%r; got "
                                "title=%r artist=%r album=%r",
                                list(perf.titles), perf.artist,
                                np_title, np_artist, np_album,
                            )
                        if loop.time() > entered_deadline:
                            controller.log.emit(
                                f"  Couldn't lock onto '{perf.album}' by "
                                f"{perf.artist} within "
                                f"{ENTERED_TIMEOUT_S:.0f}s "
                                "(row may be catalog-delisted, or "
                                "track-level credits drifted from album "
                                "metadata). Dropping from pool, advancing."
                            )
                            logger.info(
                                "classical-shuffle: dropping perf "
                                "(%s, %s) from pool — never entered after "
                                "%.0fs. Got titles: %s",
                                perf.artist, perf.album,
                                ENTERED_TIMEOUT_S,
                                sorted(diag_logged_titles),
                            )
                            try:
                                pool.remove(perf)
                            except ValueError:
                                pass
                            # Note: this perf isn't in `history` yet — it
                            # gets pushed at the top of the NEXT iteration.
                            # The pool-membership guard at the push site
                            # (search "current_perf in pool") prevents the
                            # push; the prev_playlist handler also skips
                            # not-in-pool entries defensively.
                            advance = True
                            break

                # Update the dock display only once we're locked into the
                # perf (in_perf stays True through the streak-based perf-end
                # transition, so the user still sees the drifting track for
                # the brief streak window before we advance).
                if in_perf and np and np.get("title"):
                    title = (np.get("title") or "").strip()
                    artist = (np.get("artist") or "").strip()
                    album = (np.get("album") or "").strip()
                    track = (title, artist)
                    if track != last_track:
                        controller.track_changed.emit(title, artist)
                        controller.track_meta.emit(title, artist, album)
                        controller.log.emit(
                            f"  Now playing: {title}"
                            + (f" — {artist}" if artist else "")
                        )
                        last_track = track
                    else:
                        controller.track_meta.emit(title, artist, album)
                    pos = np.get("position")
                    dur = np.get("duration")
                    if pos is not None and dur is not None:
                        controller.progress.emit(int(pos), int(dur))
                    else:
                        controller.progress.emit(0, 0)

                # Wait up to 2s for an action; on timeout, poll again.
                try:
                    action = await asyncio.wait_for(
                        action_queue.get(), timeout=2.0
                    )
                except asyncio.TimeoutError:
                    continue

                # Drain actions.
                while action is not None:
                    if action == "stop":
                        controller.cancelled = True
                        try:
                            await _click_player_button(page, "pause_only")
                        except Exception as e:
                            controller.log.emit(f"Pause-on-stop failed: {e}")
                        break
                    elif action in ("play_pause", "next", "previous"):
                        try:
                            result = await _click_player_button(page, action)
                            hit = result.get("hit")
                            if hit:
                                controller.log.emit(f"Clicked {action} ({hit}).")
                            else:
                                controller.log.emit(
                                    f"Couldn't find a {action} button."
                                )
                        except Exception as e:
                            controller.log.emit(f"{action} failed: {e}")
                    elif (
                        isinstance(action, tuple)
                        and len(action) == 2
                        and action[0] == "seek"
                    ):
                        target = int(action[1])
                        try:
                            result = await _seek_player(page, target)
                            if result.get("ok"):
                                controller.log.emit(
                                    f"Seeked to {target}s "
                                    f"({result.get('mode', '?')})."
                                )
                            else:
                                controller.log.emit(
                                    f"Seek to {target}s failed: "
                                    f"{result.get('error', 'unknown')}"
                                )
                        except Exception as e:
                            controller.log.emit(f"Seek failed: {e}")
                    elif action == "skip_playlist":
                        # The pick loop will emit either "Walking forward
                        # to: ..." (if future is non-empty) or pick fresh
                        # at random and emit the "▶ Performance N:" line
                        # — so a single "Skip" acknowledgment here would
                        # contradict the follow-up. Keep it generic.
                        controller.log.emit("  Skip performance →")
                        advance = True
                        break
                    elif action == "prev_playlist":
                        # Belt-and-suspenders: if a dropped perf slipped onto
                        # history (e.g., due to a future regression in the
                        # push-side guard above), skip past it. Replaying a
                        # not-in-pool perf would just bail out after 10s.
                        while history and history[-1] not in pool:
                            history.pop()
                        if not history:
                            controller.log.emit(
                                "  No previous performance to go back to."
                            )
                        else:
                            target = history.pop()
                            controller.log.emit(
                                f"  Replaying previous performance: "
                                f"{target.album or '(no album)'} — "
                                f"{target.artist or '(no artist)'} "
                                f"({len(history)} more in history)"
                            )
                            # Push current onto the forward stack so ⏩ (or
                            # natural end-of-perf advance) can walk back
                            # forward to it. Same in-pool guard as
                            # history.append. No-op if current_perf was
                            # itself a replay (still in pool, so it gets
                            # pushed — that's fine, ⏩ will skip past it
                            # if it later gets bailout-dropped).
                            if (current_perf is not None
                                    and current_perf in pool):
                                future.append(current_perf)
                            replay_perf = target
                            # Drop current_perf from recency so it can be
                            # picked again soon (we're undoing this play).
                            # No-op if current_perf was itself a replay
                            # (never pushed).
                            if recency is not None and recency:
                                try:
                                    recency.remove((
                                        current_perf.playlist_url,
                                        current_perf.artist.lower(),
                                        current_perf.album.lower(),
                                    ))
                                except ValueError:
                                    pass
                            advance = True
                            break
                    else:
                        controller.log.emit(
                            f"Unknown action ignored: {action!r}"
                        )
                    try:
                        action = action_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        action = None

        logger.info(
            "Classical Shuffle exited (cancelled=%s, performances_played=%d)",
            controller.cancelled, played,
        )
        return {
            "name": last_summary or folder_label,
            "performances_played": played,
            "stopped_by_user": True,
        }

    return _do_play


# ---------------------------------------------------------------------------
# Compare: dialog + task + results dialog.
# ---------------------------------------------------------------------------
class CompareDialog(QDialog):
    def __init__(self, parent: QWidget | None = None,
                 default_first: str = "") -> None:
        super().__init__(parent)
        self.setWindowTitle("Compare Spreadsheets")
        self.setMinimumWidth(520)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Pick two spreadsheets to compare. Matches on (title, artist)."
        ))

        form = QFormLayout()
        self._a = QComboBox()
        _populate_spreadsheet_combo(self._a)
        if default_first:
            for i in range(self._a.count()):
                if os.path.abspath(self._a.itemData(i)) == os.path.abspath(default_first):
                    self._a.setCurrentIndex(i)
                    break
        self._b = QComboBox()
        _populate_spreadsheet_combo(self._b, exclude_path=self._a.currentData() or "")
        self._a.currentIndexChanged.connect(self._on_a_changed)
        form.addRow("First:", self._a)
        form.addRow("Second:", self._b)
        layout.addLayout(form)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    @pyqtSlot(int)
    def _on_a_changed(self, _idx: int) -> None:
        current_b = self._b.currentData()
        _populate_spreadsheet_combo(self._b, exclude_path=self._a.currentData() or "")
        if current_b:
            for i in range(self._b.count()):
                if self._b.itemData(i) == current_b:
                    self._b.setCurrentIndex(i)
                    break

    def values(self) -> dict:
        return {
            "a": self._a.currentData() or "",
            "b": self._b.currentData() or "",
        }


def _run_compare_task(a_path: str, b_path: str) -> dict:
    from utils import normalize

    def _read(path: str) -> list[dict]:
        wb = load_workbook(path, read_only=True, data_only=True)
        try:
            ws = wb.active
            songs: list[dict] = []
            for row in ws.iter_rows(min_row=2, values_only=True):
                if not row or not row[1]:
                    continue
                title = str(row[1])
                artist = str(row[2] or "") if len(row) > 2 else ""
                songs.append({
                    "title": title,
                    "artist": artist,
                    "key": (normalize(title), normalize(artist)),
                })
            return songs
        finally:
            wb.close()

    songs_a = _read(a_path)
    songs_b = _read(b_path)
    keys_a = {s["key"] for s in songs_a}
    keys_b = {s["key"] for s in songs_b}
    lookup_a = {s["key"]: s for s in songs_a}
    lookup_b = {s["key"]: s for s in songs_b}

    def _sort(keys, lookup):
        return sorted(
            ({"title": lookup[k]["title"], "artist": lookup[k]["artist"]}
             for k in keys),
            key=lambda s: (s["title"].lower(), s["artist"].lower()),
        )

    common = _sort(keys_a & keys_b, lookup_a)
    only_a = _sort(keys_a - keys_b, lookup_a)
    only_b = _sort(keys_b - keys_a, lookup_b)

    logger.info("Compare: %d common, %d only in '%s', %d only in '%s'",
                len(common), len(only_a), os.path.basename(a_path),
                len(only_b), os.path.basename(b_path))
    return {
        "a_path": a_path,
        "b_path": b_path,
        "a_total": len(songs_a),
        "b_total": len(songs_b),
        "common": common,
        "only_a": only_a,
        "only_b": only_b,
    }


class CompareResultsDialog(QDialog):
    def __init__(self, result: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Compare Results")
        self.resize(720, 560)

        name_a = os.path.basename(result["a_path"])
        name_b = os.path.basename(result["b_path"])

        layout = QVBoxLayout(self)
        summary = QLabel(
            f"<b>{name_a}</b>: {result['a_total']} songs &nbsp;·&nbsp; "
            f"<b>{name_b}</b>: {result['b_total']} songs<br>"
            f"In common: {len(result['common'])} &nbsp;·&nbsp; "
            f"Only in {name_a}: {len(result['only_a'])} &nbsp;·&nbsp; "
            f"Only in {name_b}: {len(result['only_b'])}"
        )
        summary.setTextFormat(Qt.TextFormat.RichText)
        summary.setWordWrap(True)
        layout.addWidget(summary)

        tabs = QTabWidget()

        def _make_list(songs: list[dict]) -> QListWidget:
            lst = QListWidget()
            lst.setUniformItemSizes(True)
            for s in songs:
                QListWidgetItem(f"{s['title']} — {s['artist']}", lst)
            return lst

        tabs.addTab(_make_list(result["common"]),
                    f"In common ({len(result['common'])})")
        tabs.addTab(_make_list(result["only_a"]),
                    f"Only in {name_a} ({len(result['only_a'])})")
        tabs.addTab(_make_list(result["only_b"]),
                    f"Only in {name_b} ({len(result['only_b'])})")
        layout.addWidget(tabs)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        btns.rejected.connect(self.reject)
        btns.accepted.connect(self.accept)
        # QDialogButtonBox.Close uses rejected by default; wire both.
        btns.clicked.connect(lambda _: self.accept())
        layout.addWidget(btns)


# ---------------------------------------------------------------------------
# Database export / import
# ---------------------------------------------------------------------------
_DB_EXTENSIONS = {".xlsx", ".json"}


def _collect_output_files() -> list[str]:
    """Return paths of all exportable files anywhere under OUTPUT_DIR
    (recursively), so the full org-tree folder structure — not just the top
    level — is captured by Export Database. Excel lock/temp files (``~$...``)
    are skipped."""
    if not os.path.isdir(OUTPUT_DIR):
        return []
    out: list[str] = []
    for dirpath, _dirs, files in os.walk(OUTPUT_DIR):
        for f in files:
            if f.startswith("~$"):
                continue
            if os.path.splitext(f)[1].lower() in _DB_EXTENSIONS:
                out.append(os.path.join(dirpath, f))
    return sorted(out)


class ImportDatabaseDialog(QDialog):
    """Preview an archive and choose how to handle conflicts before importing."""

    def __init__(self, zip_path: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Import Database")
        self.setMinimumWidth(480)
        self._zip_path = zip_path
        self._names: list[str] = []
        self._conflicts: list[str] = []
        self._valid = False

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                self._names = [
                    info.filename for info in zf.infolist()
                    if not info.is_dir()
                    and os.path.splitext(info.filename)[1].lower() in _DB_EXTENSIONS
                ]
            existing = {
                os.path.relpath(p, OUTPUT_DIR).replace(os.sep, "/")
                for p in _collect_output_files()
            }
            self._conflicts = [
                n for n in self._names if n.replace("\\", "/") in existing
            ]
            self._valid = True
        except Exception as exc:
            layout.addWidget(QLabel(f"Could not read archive:\n{exc}"))
            btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
            btns.rejected.connect(self.reject)
            layout.addWidget(btns)
            return

        xlsx = [n for n in self._names if n.lower().endswith(".xlsx")]
        jsons = [n for n in self._names if n.lower().endswith(".json")]

        summary = QLabel(
            f"<b>{len(self._names)}</b> file(s) in archive — "
            f"{len(xlsx)} spreadsheet(s), {len(jsons)} config file(s)."
        )
        summary.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(summary)

        if self._conflicts:
            conflict_label = QLabel(
                f"<b>{len(self._conflicts)}</b> file(s) already exist in your "
                f"library and would conflict:"
            )
            conflict_label.setTextFormat(Qt.TextFormat.RichText)
            layout.addWidget(conflict_label)

            conflict_list = QListWidget()
            conflict_list.setMaximumHeight(120)
            for name in self._conflicts:
                conflict_list.addItem(name)
            layout.addWidget(conflict_list)

            self._overwrite_cb = QCheckBox("Overwrite conflicting files")
            layout.addWidget(self._overwrite_cb)
        else:
            layout.addWidget(QLabel("No conflicts — all files are new."))
            self._overwrite_cb = None

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btns.button(QDialogButtonBox.StandardButton.Ok).setText("Import")
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def overwrite_conflicts(self) -> bool:
        return self._overwrite_cb is not None and self._overwrite_cb.isChecked()

    def importable_names(self) -> list[str]:
        return self._names


# ---------------------------------------------------------------------------
# Login helpers
# ---------------------------------------------------------------------------
async def _login_check_coroutine(page):
    """Navigate to Amazon Music and return whether the user is signed in."""
    from auth import _detect_logged_in, _suppress_dialogs
    from config import BASE_URL, LONG_TIMEOUT
    await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=LONG_TIMEOUT)
    await page.wait_for_timeout(2000)
    logged_in = await _detect_logged_in(page)
    if logged_in:
        await _suppress_dialogs(page)
    return {"logged_in": logged_in}


class LoginWaitDialog(QDialog):
    """Shown after Chrome connects when the user isn't yet signed in."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Sign in to Amazon Music")
        self.setMinimumWidth(400)

        layout = QVBoxLayout(self)
        layout.setSpacing(16)

        icon_label = QLabel()
        icon_path = _resource_path("AMM_Icon.png")
        if os.path.exists(icon_path):
            pix = QPixmap(icon_path).scaledToHeight(
                64, Qt.TransformationMode.SmoothTransformation
            )
            icon_label.setPixmap(pix)
            icon_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            layout.addWidget(icon_label)

        msg = QLabel(
            "Amazon Music is open in Chrome.\n\n"
            "Please sign in, then click <b>Done</b>."
        )
        msg.setTextFormat(Qt.TextFormat.RichText)
        msg.setWordWrap(True)
        msg.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(msg)

        btns = QDialogButtonBox()
        done_btn = btns.addButton("Done", QDialogButtonBox.ButtonRole.AcceptRole)
        cancel_btn = btns.addButton("Cancel", QDialogButtonBox.ButtonRole.RejectRole)
        done_btn.setDefault(True)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)


class DeletePlaylistDialog(QDialog):
    """Confirm deletion of a playlist; if a cached xlsx exists, offer to
    delete it too via a checkbox (default checked)."""

    def __init__(
        self, parent: QWidget | None, name: str, xlsx_paths: list[str]
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Delete playlist")
        self.setMinimumWidth(420)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        msg = QLabel(
            f"Permanently delete '{name}' from Amazon Music?\n\n"
            "This cannot be undone."
        )
        msg.setWordWrap(True)
        layout.addWidget(msg)

        if xlsx_paths:
            self._xlsx_check = QCheckBox(
                f"Also delete cached spreadsheet"
                f"{'s' if len(xlsx_paths) > 1 else ''} from disk:"
            )
            self._xlsx_check.setChecked(True)
            layout.addWidget(self._xlsx_check)
            display = "\n".join(
                f"  • {os.path.relpath(p, OUTPUT_DIR)}" for p in xlsx_paths
            )
            file_label = QLabel(display)
            file_label.setStyleSheet("color: gray;")
            layout.addWidget(file_label)
        else:
            self._xlsx_check = None

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Yes
            | QDialogButtonBox.StandardButton.Cancel
        )
        cancel_btn = btns.button(QDialogButtonBox.StandardButton.Cancel)
        if cancel_btn is not None:
            cancel_btn.setDefault(True)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def delete_xlsx(self) -> bool:
        return self._xlsx_check is not None and self._xlsx_check.isChecked()


class WebRestrictedReviewDialog(QDialog):
    """Review the log of web-restricted tracks (see web_restricted.py).

    Displays one row per entry in web_restricted_tracks.json. The user can:
      - "Remove from Amazon Music" — runs the existing delete-songs flow on
        the playlist, then clears the entry from the log on success. Emits
        `remove_requested(playlist_url, playlist_name, songs, asin)` for
        MainWindow to dispatch via the worker; the dialog refreshes after.
      - "Clear from list" — drops the entry from the JSON without touching
        Amazon (use this when you re-exported the playlist and the row is
        gone, or when you've decided to keep the misroute and stop being
        warned).
      - "Refresh" — re-read the JSON from disk.

    The dialog is non-modal so the user can switch to other windows
    (e.g., the spreadsheet detail view) while reviewing.
    """

    # Caller (MainWindow) handles the actual Amazon-side deletion via its
    # task worker. Payload: (playlist_url, playlist_name, songs, asin)
    # where songs is a list of one {title, artist} dict.
    remove_requested = pyqtSignal(str, str, object, str)

    # Columns in the table.
    COL_PLAYLIST = 0
    COL_ROW = 1
    COL_TITLE = 2
    COL_ARTIST = 3
    COL_ALBUM = 4
    COL_DETECTED = 5
    # Identity of the entry stored on each row so refresh+actions stay correct
    # across user sort. Tuple: (playlist_url, requested_asin).
    ROLE_ENTRY_KEY = Qt.ItemDataRole.UserRole + 20

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Review web-restricted tracks")
        self.setMinimumSize(800, 420)
        # Non-modal: user can keep using the main window while reviewing.
        self.setModal(False)

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        intro = QLabel(
            "These tracks misroute when played from the Amazon Music web "
            "player — Amazon's backend silently substitutes a different track. "
            "The same rows play correctly in the Windows desktop app. Use "
            "the buttons below to remove a row from the playlist on Amazon, "
            "or to clear an entry from this list."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet("color: gray;")
        layout.addWidget(intro)

        self._table = QTableWidget(0, 6)
        self._table.setHorizontalHeaderLabels(
            ["Playlist", "Row", "Title", "Artist", "Album", "Detected"]
        )
        self._table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self._table.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self._table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self._table.setSortingEnabled(True)
        self._table.verticalHeader().setVisible(False)
        h = self._table.horizontalHeader()
        h.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        # Sensible default widths — title and artist deserve the most room.
        widths = [180, 50, 260, 180, 180, 140]
        for c, w in enumerate(widths):
            self._table.setColumnWidth(c, w)
        layout.addWidget(self._table, 1)

        btn_row = QHBoxLayout()
        self._refresh_btn = QPushButton("Refresh")
        self._refresh_btn.clicked.connect(self.refresh)
        self._remove_btn = QPushButton("Remove from Amazon Music…")
        self._remove_btn.setToolTip(
            "Delete the selected row(s) from the Amazon Music playlist "
            "(requires Chrome connected). Clears the entry from this list "
            "after successful deletion."
        )
        self._remove_btn.clicked.connect(self._on_remove_clicked)
        self._clear_btn = QPushButton("Clear from list")
        self._clear_btn.setToolTip(
            "Drop the selected entry from this review log without touching "
            "Amazon Music. Use if you re-exported the playlist or want to "
            "stop being warned about this row."
        )
        self._clear_btn.clicked.connect(self._on_clear_clicked)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(self._refresh_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(self._clear_btn)
        btn_row.addWidget(self._remove_btn)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

        self.refresh()

    def refresh(self) -> None:
        """Reload from web_restricted.json and repopulate the table."""
        import web_restricted
        try:
            entries = web_restricted.load()
        except Exception:
            logger.exception("Could not load web_restricted log")
            entries = []
        # Sort newest-first by detected_at.
        entries.sort(
            key=lambda e: (e.get("detected_at") or ""), reverse=True,
        )
        self._table.setSortingEnabled(False)
        try:
            self._table.setRowCount(len(entries))
            for r, e in enumerate(entries):
                pl = QTableWidgetItem(e.get("playlist_name") or "")
                pl.setToolTip(e.get("playlist_url") or "")
                row_num = e.get("row_index")
                ri = QTableWidgetItem()
                if isinstance(row_num, int):
                    ri.setData(Qt.ItemDataRole.DisplayRole, row_num)
                else:
                    ri.setText("")
                title_item = QTableWidgetItem(e.get("title") or "")
                artist_item = QTableWidgetItem(e.get("artist") or "")
                album_item = QTableWidgetItem(e.get("album") or "")
                detected_item = QTableWidgetItem(e.get("detected_at") or "")
                # Stash identity on column 0 so we can look up the entry
                # regardless of the user's current sort order.
                key = (
                    (e.get("playlist_url") or "").strip(),
                    (e.get("requested_asin") or "").strip().upper(),
                )
                pl.setData(self.ROLE_ENTRY_KEY, key)
                self._table.setItem(r, self.COL_PLAYLIST, pl)
                self._table.setItem(r, self.COL_ROW, ri)
                self._table.setItem(r, self.COL_TITLE, title_item)
                self._table.setItem(r, self.COL_ARTIST, artist_item)
                self._table.setItem(r, self.COL_ALBUM, album_item)
                self._table.setItem(r, self.COL_DETECTED, detected_item)
        finally:
            self._table.setSortingEnabled(True)
        # Disable the action buttons until at least one entry is selected;
        # also enable Refresh/Close regardless.
        self._table.selectionModel().selectionChanged.connect(
            self._update_action_buttons,
        )
        self._update_action_buttons()

    def _update_action_buttons(self, *_args) -> None:
        has_sel = bool(self._table.selectionModel().selectedRows())
        self._remove_btn.setEnabled(has_sel)
        self._clear_btn.setEnabled(has_sel)

    def _selected_entries(self) -> list[dict]:
        """Return the original entry dicts for currently-selected rows.

        Uses the stashed (playlist_url, requested_asin) key to look up the
        entry from the live JSON — robust to user sort and the row's
        on-screen position."""
        import web_restricted
        all_entries = web_restricted.load()
        index = {
            (
                (e.get("playlist_url") or "").strip(),
                (e.get("requested_asin") or "").strip().upper(),
            ): e
            for e in all_entries
        }
        out: list[dict] = []
        for r in self._table.selectionModel().selectedRows():
            item = self._table.item(r.row(), self.COL_PLAYLIST)
            if item is None:
                continue
            key = item.data(self.ROLE_ENTRY_KEY)
            if not isinstance(key, tuple):
                continue
            entry = index.get(key)
            if entry is not None:
                out.append(entry)
        return out

    @pyqtSlot()
    def _on_remove_clicked(self) -> None:
        selected = self._selected_entries()
        if not selected:
            return
        n = len(selected)
        titles = "\n".join(
            f"  • {e.get('title') or '(no title)'} — {e.get('artist') or ''}"
            for e in selected[:10]
        )
        if n > 10:
            titles += f"\n  … and {n - 10} more"
        reply = QMessageBox.warning(
            self,
            "Remove from Amazon Music",
            f"Remove {n} restricted track{'s' if n != 1 else ''} "
            f"from the playlist on Amazon Music?\n\n{titles}\n\n"
            "Requires Chrome connected. This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        # Emit one signal per entry — keeping the per-playlist-per-track
        # granularity simple. MainWindow's handler queues them through the
        # task worker. (If we batched into one signal per playlist, we'd
        # have to thread playlist-grouping through the task layer; the per-
        # signal approach reuses the existing delete_from_amazon plumbing
        # with minimal changes.)
        for e in selected:
            url = e.get("playlist_url") or ""
            name = e.get("playlist_name") or ""
            song = {
                "title": e.get("title") or "",
                "artist": e.get("artist") or "",
            }
            asin = (e.get("requested_asin") or "").upper()
            if url and song["title"]:
                self.remove_requested.emit(url, name, [song], asin)

    @pyqtSlot()
    def _on_clear_clicked(self) -> None:
        import web_restricted
        selected = self._selected_entries()
        if not selected:
            return
        for e in selected:
            url = e.get("playlist_url") or ""
            asin = (e.get("requested_asin") or "").upper()
            if url and asin:
                web_restricted.remove(playlist_url=url, requested_asin=asin)
        self.refresh()


class BulkDeletePlaylistDialog(QDialog):
    """Confirm bulk deletion of N playlists; if any cached xlsx exist across
    those playlists, offer to delete them too via a single checkbox."""

    def __init__(
        self,
        parent: QWidget | None,
        items: list[dict],
        xlsx_paths: list[str],
    ) -> None:
        super().__init__(parent)
        n = len(items)
        self.setWindowTitle("Delete playlists")
        self.setMinimumWidth(480)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        msg = QLabel(
            f"Permanently delete {n} playlist{'s' if n != 1 else ''} "
            "from Amazon Music?\n\nThis cannot be undone."
        )
        msg.setWordWrap(True)
        layout.addWidget(msg)

        # Show up to 12 names, then "and K more".
        max_show = 12
        shown = items[:max_show]
        more = n - len(shown)
        names = "\n".join(f"  • {it.get('name') or it.get('url') or ''}" for it in shown)
        if more > 0:
            names += f"\n  …and {more} more"
        names_label = QLabel(names)
        names_label.setStyleSheet("color: gray;")
        layout.addWidget(names_label)

        if xlsx_paths:
            self._xlsx_check = QCheckBox(
                f"Also delete {len(xlsx_paths)} cached "
                f"spreadsheet{'s' if len(xlsx_paths) != 1 else ''} "
                "from disk"
            )
            self._xlsx_check.setChecked(True)
            layout.addWidget(self._xlsx_check)
        else:
            self._xlsx_check = None

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Yes
            | QDialogButtonBox.StandardButton.Cancel
        )
        yes_btn = btns.button(QDialogButtonBox.StandardButton.Yes)
        if yes_btn is not None:
            yes_btn.setText(f"Delete {n}")
        cancel_btn = btns.button(QDialogButtonBox.StandardButton.Cancel)
        if cancel_btn is not None:
            cancel_btn.setDefault(True)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def delete_xlsx(self) -> bool:
        return self._xlsx_check is not None and self._xlsx_check.isChecked()


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------
USER_GUIDE_HTML = """
<h1>Amazon Music Manager — User Guide</h1>
<p>Amazon Music Manager exports your Amazon Music playlists to spreadsheets,
organizes them into folders, creates and edits playlists on Amazon, and plays
music — all by driving a real Chrome browser. This guide walks through the main
features.</p>

<h2>1. Getting started: connect Chrome</h2>
<p>Almost everything that touches Amazon Music needs a live browser connection.</p>
<ul>
<li>Click <b>Connect Chrome</b> on the toolbar (or press <b>Ctrl+Shift+C</b>).
A Chrome window opens and signs in to Amazon Music; the app drives it in the
background and minimizes it out of the way.</li>
<li>The first launch can occasionally do nothing if another Chrome is already
using the profile — just click <b>Connect Chrome</b> again.</li>
<li>While connected, the PC is kept awake so long operations don't drop the
connection if you step away (the monitor may still turn off — that's fine).</li>
<li>Use <b>File → Connect Chrome on startup</b> to auto-connect each launch.</li>
<li>Features that don't need Amazon (browsing spreadsheets, merging, searching)
work without connecting.</li>
</ul>

<h2>2. The layout</h2>
<ul>
<li><b>Organization sidebar (left):</b> your virtual folders — a tree of
playlists and spreadsheets you arrange yourself. An item can live in more than
one folder.</li>
<li><b>Main area (tabs):</b> <b>All Spreadsheets</b>, <b>All Playlists</b>, and
<b>Search</b>. Double-click a spreadsheet to open its detail view.</li>
<li><b>Spreadsheet detail:</b> the tracklist plus action buttons (Update,
Metadata, Filter, Duplicates, Curate, Create Playlist, Reconcile, …).</li>
</ul>

<h2>3. Export a playlist to a spreadsheet</h2>
<ul>
<li>Click <b>Export Playlist</b> (<b>Ctrl+E</b>), pick a playlist, and it's
scraped to a styled <tt>.xlsx</tt> in your library folder (see
&ldquo;Where your data lives&rdquo; below).</li>
<li>Right-click a playlist inside a folder → <b>Export to spreadsheet…</b> to
save it straight into that folder.</li>
<li><b>Update</b> (in the detail view) re-scrapes an Amazon-tagged spreadsheet
and adds only new songs — it remembers the source playlist, so no re-picking.</li>
</ul>

<h2>4. Work with spreadsheets</h2>
<ul>
<li><b>Search bar / live filter:</b> type in the detail view to filter the
tracklist instantly — no save needed. <b>Clear filter</b> restores it.</li>
<li><b>Merge / Compare:</b> combine multiple spreadsheets (<b>Ctrl+M</b>) or
diff two of them (<b>Ctrl+Shift+M</b>).</li>
<li><b>Duplicates:</b> find duplicate tracks within a spreadsheet.</li>
<li><b>Curate:</b> hand-pick songs page by page (supports ranges like
<tt>1,3,5-8</tt>) into a new spreadsheet.</li>
<li><b>Filter:</b> build a new playlist/spreadsheet from rows matching criteria.</li>
<li><b>Metadata:</b> enrich tracks via MusicBrainz. <b>Turn your VPN off</b> for
this — MusicBrainz blocks common VPN exit nodes.</li>
</ul>

<h2>5. Create an Amazon playlist</h2>
<ul>
<li><b>Create Playlist</b> pushes a spreadsheet's (or CSV's) songs into a new
Amazon playlist. Each song is searched and matched by score; high-confidence
matches are added automatically, ambiguous ones prompt you to choose.</li>
</ul>

<h2>6. Reconcile &amp; reorder (keep a spreadsheet and playlist in sync)</h2>
<p>The <b>Reconcile ▾</b> button (detail view, when Chrome is connected and the
spreadsheet has a source playlist) offers:</p>
<ul>
<li><b>Spreadsheet ← Playlist:</b> pull Amazon's current contents into the xlsx
(preview of adds/removes first).</li>
<li><b>Playlist ← Spreadsheet:</b> make Amazon match the xlsx (removes extras,
adds missing).</li>
<li><b>Reorder</b> either side to match the other's track order.</li>
</ul>

<h2>7. Organize into folders</h2>
<ul>
<li>Create folders in the sidebar and drag playlists/spreadsheets in. Items can
be filed in multiple folders.</li>
<li>Multi-select with Shift/Ctrl-click, then right-click for bulk
<b>Cut/Copy/Remove/Delete</b>. Right-click a folder for <b>New / Rename /
Delete folder</b>.</li>
<li>Bulk-add playlists to a folder, copy their URLs, or delete several from
Amazon at once (All Playlists view).</li>
</ul>

<h2>8. Play music</h2>
<ul>
<li>Play a single playlist straight through or shuffled.</li>
<li><b>Play all in folder (recursive)</b> builds a queue from a whole subtree
and auto-advances when each playlist ends.</li>
</ul>

<h2>9. Classical Shuffle (live)</h2>
<p>Right-click a folder named <b>Classical</b> (or any folder beneath it) →
<b>Classical Shuffle (recursive)</b>. It groups your classical tracks into
complete <i>performances</i> (a full multi-movement work by one recording) and
plays random performances back-to-back — so you never get a lone movement.</p>
<ul>
<li>A filter dialog lets you narrow by <b>composer</b> and/or <b>piece type</b>
(Symphonies, Concertos, …).</li>
<li>Use <b>⏪ / ⏩</b> to retrace: ⏪ steps back through what you've heard, and
⏩ walks forward again through what you rewound before resuming random play.</li>
</ul>

<h2>10. Generate a Classical Shuffle playlist (take it with you)</h2>
<p>Right-click a Classical folder → <b>Generate Classical Shuffle playlist…</b>
to pre-build a real Amazon playlist from the same performance pool — so you can
listen on your phone in the Amazon Music app.</p>
<ul>
<li>Pick a <b>duration target</b> (e.g. ~4 hours) instead of a track count.</li>
<li>Optionally filter by composer and piece type.</li>
<li>Name it (a sensible default is filled in) and choose an output folder
(defaults to <b>Shuffle Classical</b>).</li>
<li>It samples whole performances to fill the time, creates the playlist, and
adds each track. A progress window shows it working; each generation makes a
new playlist so your history is preserved.</li>
</ul>
<p><b>Tip:</b> the generator reads your <i>cached</i> spreadsheets. If you edit a
source playlist on Amazon, re-export (re-scrape) it first so the change is
picked up.</p>

<h2>11. Web-restricted tracks</h2>
<p>A few Amazon tracks are licensed for the desktop/mobile apps but not the web
player; clicking them on the web silently plays a different track. The app
detects these and skips them in Classical Shuffle. Review them under
<b>Tools → Review web-restricted tracks…</b>, where you can remove them from a
playlist or clear them from the list. (They play fine on your phone.)</p>

<h2>12. Delete things</h2>
<ul>
<li><b>Delete a playlist:</b> right-click it → <b>Delete playlist…</b>. Removes
it from Amazon and offers to delete the cached spreadsheet(s) too.</li>
<li><b>Delete songs:</b> select rows in a spreadsheet → right-click → remove
from the spreadsheet, from Amazon, or from both (a failed Amazon delete never
wipes the row).</li>
</ul>

<h2>13. Back up &amp; transfer your library</h2>
<ul>
<li><b>File → Export Database…</b> (<b>Ctrl+Shift+E</b>) saves every spreadsheet
<i>and</i> your full folder organization to a single <tt>.zip</tt>.</li>
<li><b>File → Import Database…</b> (<b>Ctrl+Shift+I</b>) restores that
<tt>.zip</tt> — folder structure and all — into this copy of the app. This is
how you move your library and organization to another PC or a fresh install:
Export on one, Import on the other.</li>
</ul>

<h2>Where your data lives</h2>
<p>Your library (spreadsheets, the folder tree, and settings) is stored under a
per-user data folder:
<tt>%LOCALAPPDATA%\\AmazonMusicManager\\output</tt> on an installed copy. The
Chrome sign-in session is kept separately in <tt>browser_data</tt>. Everything
stays on your machine. To back up by hand, copy that <tt>output</tt> folder —
or just use <b>Export Database</b>.</p>

<h2>Keyboard shortcuts</h2>
<ul>
<li><b>Ctrl+Shift+C</b> — Connect Chrome</li>
<li><b>Ctrl+E</b> — Export Playlist</li>
<li><b>Ctrl+M</b> — Merge Spreadsheets &nbsp;·&nbsp; <b>Ctrl+Shift+M</b> — Compare</li>
<li><b>Ctrl+Shift+E / Ctrl+Shift+I</b> — Export / Import Database</li>
</ul>

<h2>Troubleshooting</h2>
<ul>
<li><b>"Connect did nothing, worked on the second try":</b> Chrome was already
using the profile; clicking again handles it.</li>
<li><b>Metadata lookups fail:</b> disconnect your VPN (MusicBrainz blocks many
VPN nodes), then retry.</li>
<li><b>A change on Amazon isn't reflected:</b> the app reads cached spreadsheets
— re-export/Update the affected playlist to refresh it.</li>
<li><b>Browser disconnected mid-run:</b> keep the app connected; system sleep
is blocked while connected, but don't fully suspend/hibernate the PC.</li>
</ul>
"""


class HelpDialog(QDialog):
    """Non-modal user-guide window: scrollable rendered HTML + Close button."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Amazon Music Manager — User Guide")
        self.resize(760, 640)
        layout = QVBoxLayout(self)

        browser = QTextBrowser(self)
        browser.setOpenExternalLinks(True)
        browser.setHtml(USER_GUIDE_HTML)
        layout.addWidget(browser)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)


def _developer_photo_path() -> str | None:
    """Locate Developer.jpeg — in a PyInstaller bundle (via _resource_path),
    next to this file, or in the CWD. Returns the path if it exists, else None."""
    here = os.path.dirname(os.path.abspath(__file__))
    for candidate in (
        _resource_path("Developer.jpeg"),
        os.path.join(here, "Developer.jpeg"),
        "Developer.jpeg",
    ):
        if os.path.isfile(candidate):
            return candidate
    return None


class AboutDialog(QDialog):
    """Small modal About box: developer photo + app/author credits."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("About Amazon Music Manager")
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        # Developer photo (scaled, centered). Skip gracefully if missing.
        photo_path = _developer_photo_path()
        if photo_path:
            pix = QPixmap(photo_path)
            if not pix.isNull():
                # The source photo is stored landscape; rotate 90° clockwise so
                # it displays upright. (Done in code — original file untouched.)
                pix = pix.transformed(
                    QTransform().rotate(90),
                    Qt.TransformationMode.SmoothTransformation,
                )
                pix = pix.scaled(
                    240, 300,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                photo = QLabel(self)
                photo.setPixmap(pix)
                photo.setAlignment(Qt.AlignmentFlag.AlignCenter)
                layout.addWidget(photo)

        credits = QLabel(self)
        credits.setAlignment(Qt.AlignmentFlag.AlignCenter)
        credits.setTextFormat(Qt.TextFormat.RichText)
        credits.setText(
            "<div align='center'>"
            "<p style='font-size:16pt; font-weight:bold; margin-bottom:2px;'>"
            "Amazon Music Manager</p>"
            "<p style='margin-top:0;'>Version 1.0</p>"
            "<p><b>DevCon Productions</b><br>Cleveland, Ohio, USA</p>"
            "<p style='color:gray;'>Vibe coded with Claude Code</p>"
            "</div>"
        )
        layout.addWidget(credits)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)


class MainWindow(QMainWindow):
    STATE_DISCONNECTED = "disconnected"
    STATE_CONNECTING = "connecting"
    STATE_CHECKING_LOGIN = "checking_login"
    STATE_CONNECTED = "connected"
    STATE_DISCONNECTING = "disconnecting"

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Amazon Music Manager")
        self.resize(1200, 800)

        self._state = self.STATE_DISCONNECTED
        self._thread: QThread | None = None
        self._worker: BrowserWorker | None = None
        self._task_running = False
        # Create-playlist run state (lives for the duration of one task).
        self._create_controller: CreatePlaylistController | None = None
        self._create_progress: CreatePlaylistProgressDialog | None = None
        # Reconcile run state — holds context between the "scrape" task and the
        # "apply" step that runs after the preview dialog is confirmed.
        self._reconcile_path: str = ""
        self._reconcile_xlsx_songs: list[dict] = []
        self._reconcile_playlist_url: str = ""
        self._reconcile_playlist_name: str = ""
        # Set when an export is launched from the org-tree right-click menu.
        # On export completion, the new xlsx gets auto-filed in this folder.
        self._pending_export_folder: list[str] | None = None
        # Set when "Delete playlist" was launched with the "also delete cached
        # spreadsheet(s)" box checked. Consumed (and cleared) by the
        # delete_playlist completion branch.
        self._pending_delete_playlist_xlsx: list[str] = []
        # Bulk-delete run state (lives for the duration of one task).
        self._bulk_delete_controller: BulkDeleteController | None = None
        self._bulk_delete_progress: BulkDeleteProgressDialog | None = None
        # Classical-Shuffle generator run state (Step 6 of the
        # playlist-generator plan — see
        # [[project_classical_shuffle_playlist_gen_plan]]).
        self._generate_shuffle_controller: (
            GenerateShufflePlaylistController | None
        ) = None
        self._generate_shuffle_progress: (
            GenerateShufflePlaylistProgressDialog | None
        ) = None
        self._pending_bulk_delete_xlsx: list[str] = []
        # Playback run state.
        self._playback_controller: PlaybackController | None = None

        self._background = BackgroundTasks()
        self._background.task_completed.connect(self._on_task_completed)
        self._background.task_failed.connect(self._on_task_failed)

        self._tray: QSystemTrayIcon | None = None

        self._build_menu_bar()
        self._build_toolbar()
        self._build_central()
        self._build_log_dock()
        self._build_now_playing_dock()
        self._build_status_bar()
        self._wire_logging()
        self._setup_tray()

        self._apply_state(self.STATE_DISCONNECTED)

        settings = QSettings("VYSERIX", "AmazonMusicManager")
        geom = settings.value("mainwindow/geometry")
        if geom:
            self.restoreGeometry(geom)
        split = settings.value("mainwindow/splitter")
        if split:
            self._splitter.restoreState(split)

        if settings.value("connect/auto", False, type=bool):
            QTimer.singleShot(0, self._on_connect_clicked)

    # --- UI construction ---------------------------------------------------
    def _build_menu_bar(self) -> None:
        file_menu = self.menuBar().addMenu("&File")

        export_db_action = QAction("Export Database…", self)
        export_db_action.setShortcut(QKeySequence("Ctrl+Shift+E"))
        export_db_action.setStatusTip("Save all spreadsheets and config to a .zip archive")
        export_db_action.triggered.connect(self._on_export_database)
        file_menu.addAction(export_db_action)

        import_db_action = QAction("Import Database…", self)
        import_db_action.setShortcut(QKeySequence("Ctrl+Shift+I"))
        import_db_action.setStatusTip("Restore spreadsheets and config from a .zip archive")
        import_db_action.triggered.connect(self._on_import_database)
        file_menu.addAction(import_db_action)

        file_menu.addSeparator()

        settings = QSettings("VYSERIX", "AmazonMusicManager")
        autoconnect_action = QAction("Connect Chrome on startup", self)
        autoconnect_action.setCheckable(True)
        autoconnect_action.setChecked(
            settings.value("connect/auto", False, type=bool)
        )
        autoconnect_action.setStatusTip(
            "Automatically connect to Chrome when the app launches"
        )
        autoconnect_action.toggled.connect(self._on_autoconnect_toggled)
        file_menu.addAction(autoconnect_action)

        keepawake_action = QAction("Block idle sleep while connected", self)
        keepawake_action.setCheckable(True)
        keepawake_action.setChecked(
            settings.value("session/keep_awake", True, type=bool)
        )
        keepawake_action.setStatusTip(
            "Prevent Windows from sleeping (which drops the browser connection) "
            "while Chrome is connected. The screen can still turn off."
        )
        keepawake_action.toggled.connect(self._on_keepawake_toggled)
        file_menu.addAction(keepawake_action)

        tools_menu = self.menuBar().addMenu("&Tools")
        review_action = QAction("Review web-restricted tracks…", self)
        review_action.setStatusTip(
            "Inspect tracks Amazon misroutes on the web player and "
            "optionally remove them"
        )
        review_action.triggered.connect(self._on_review_web_restricted)
        tools_menu.addAction(review_action)

        help_menu = self.menuBar().addMenu("&Help")
        guide_action = QAction("User Guide", self)
        guide_action.setShortcut(QKeySequence.StandardKey.HelpContents)  # F1
        guide_action.setStatusTip("How to use Amazon Music Manager")
        guide_action.triggered.connect(self._on_show_user_guide)
        help_menu.addAction(guide_action)

        help_menu.addSeparator()
        about_action = QAction("About Amazon Music Manager", self)
        about_action.setStatusTip("Version and credits")
        about_action.triggered.connect(self._on_show_about)
        help_menu.addAction(about_action)

    def _on_show_about(self) -> None:
        """Open the modal About box."""
        AboutDialog(self).exec()

    def _on_show_user_guide(self) -> None:
        """Open the user-guide window (reuse a single instance if already open)."""
        existing = getattr(self, "_help_dialog", None)
        if existing is not None and existing.isVisible():
            existing.raise_()
            existing.activateWindow()
            return
        self._help_dialog = HelpDialog(self)
        self._help_dialog.show()

    def _build_toolbar(self) -> None:
        tb = QToolBar("Main")
        tb.setMovable(False)
        self.addToolBar(tb)

        self._connect_action = QAction("Connect Chrome", self)
        self._connect_action.setShortcut(QKeySequence("Ctrl+Shift+C"))
        self._connect_action.triggered.connect(self._on_connect_clicked)
        tb.addAction(self._connect_action)

        self._disconnect_action = QAction("Disconnect", self)
        self._disconnect_action.triggered.connect(self._on_disconnect_clicked)
        tb.addAction(self._disconnect_action)

        tb.addSeparator()

        self._export_action = QAction("Export Playlist", self)
        self._export_action.setShortcut(QKeySequence("Ctrl+E"))
        self._export_action.triggered.connect(self._on_export_clicked)
        tb.addAction(self._export_action)

        tb.addSeparator()

        self._merge_action = QAction("Merge Spreadsheets", self)
        self._merge_action.setShortcut(QKeySequence("Ctrl+M"))
        self._merge_action.triggered.connect(self._on_merge_clicked)
        tb.addAction(self._merge_action)

        self._compare_action = QAction("Compare Spreadsheets", self)
        self._compare_action.setShortcut(QKeySequence("Ctrl+Shift+M"))
        self._compare_action.triggered.connect(self._on_compare_clicked)
        tb.addAction(self._compare_action)

    def _build_central(self) -> None:
        self._splitter = QSplitter(Qt.Orientation.Horizontal)

        sidebar = QWidget()
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(8, 8, 8, 8)
        self._org_tree = OrganizationTreeWidget()
        self._org_tree.spreadsheet_activated.connect(
            self._on_spreadsheet_activated
        )
        self._org_tree.playlist_activated.connect(self._on_playlist_activated)
        self._org_tree.folder_selected.connect(self._on_folder_selected)
        self._org_tree.show_playlists.connect(self._on_show_playlists)
        self._org_tree.show_search.connect(self._on_show_search)
        self._org_tree.play_folder_requested.connect(self._on_play_folder)
        self._org_tree.classical_shuffle_requested.connect(
            self._start_classical_shuffle
        )
        self._org_tree.classical_shuffle_generate_requested.connect(
            self._open_classical_shuffle_generate_dialog
        )
        self._org_tree.playlist_export_to_folder_requested.connect(
            self._on_playlist_export_to_folder_requested
        )
        sidebar_layout.addWidget(self._org_tree)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(8, 8, 8, 8)
        self._content_stack = QStackedWidget()
        self._spreadsheet_list = SpreadsheetListWidget()
        self._spreadsheet_list.spreadsheet_activated.connect(
            self._on_spreadsheet_activated
        )
        self._spreadsheet_list.clipboard_requested.connect(
            self._on_list_clipboard_requested
        )
        self._spreadsheet_list.delete_requested.connect(
            self._org_tree._delete_spreadsheet_file
        )
        self._spreadsheet_list.rename_requested.connect(
            self._org_tree._rename_spreadsheet_file
        )
        self._spreadsheet_list.bulk_clipboard_requested.connect(
            self._on_list_bulk_clipboard_requested
        )
        self._spreadsheet_list.bulk_delete_requested.connect(
            self._on_list_bulk_delete_requested
        )
        self._org_tree.spreadsheet_renamed.connect(
            self._on_spreadsheet_renamed
        )
        self._org_tree.clipboard_changed.connect(
            self._on_tree_clipboard_changed
        )
        # Org mutations (folder CRUD, deletes) should refresh everything else.
        self._org_tree.org_changed.connect(self._spreadsheet_list.refresh)
        self._spreadsheet_detail = SpreadsheetDetailWidget()
        self._spreadsheet_detail.back_requested.connect(self._on_detail_back)
        self._spreadsheet_detail.update_requested.connect(self._on_update_requested)
        self._spreadsheet_detail.metadata_requested.connect(self._on_metadata_requested)
        self._spreadsheet_detail.filter_requested.connect(self._on_filter_requested)
        self._spreadsheet_detail.duplicates_requested.connect(
            self._on_duplicates_requested
        )
        self._spreadsheet_detail.curate_requested.connect(
            self._on_curate_requested
        )
        self._spreadsheet_detail.create_requested.connect(
            self._on_create_requested
        )
        self._spreadsheet_detail.rows_deleted.connect(self._on_rows_deleted)
        self._spreadsheet_detail.delete_from_amazon_requested.connect(
            self._on_delete_from_amazon_requested
        )
        self._spreadsheet_detail.delete_from_amazon_confirmed.connect(
            self._on_delete_from_amazon_confirmed
        )
        self._spreadsheet_detail.reconcile_from_playlist_requested.connect(
            self._on_reconcile_from_playlist_requested
        )
        self._spreadsheet_detail.reconcile_to_playlist_requested.connect(
            self._on_reconcile_to_playlist_requested
        )
        self._spreadsheet_detail.reorder_xlsx_from_playlist_requested.connect(
            self._on_reorder_xlsx_from_playlist_requested
        )
        self._spreadsheet_detail.reorder_playlist_from_xlsx_requested.connect(
            self._on_reorder_playlist_from_xlsx_requested
        )
        self._playlist_list = PlaylistListWidget()
        self._playlist_list.playlist_activated.connect(self._on_playlist_activated)
        self._playlist_list.fetch_requested.connect(self._on_fetch_playlists_clicked)
        self._playlist_list.export_requested.connect(self._on_playlist_export_requested)
        self._playlist_list.delete_requested.connect(self._on_delete_playlist_requested)
        self._playlist_list.add_to_folder_requested.connect(
            self._on_playlist_add_to_folder
        )
        self._playlist_list.bulk_add_to_folder_requested.connect(
            self._on_playlist_bulk_add_to_folder
        )
        self._playlist_list.bulk_delete_requested.connect(
            self._on_bulk_delete_playlists_requested
        )
        self._org_tree.org_changed.connect(self._playlist_list.refresh)
        self._search_view = SearchWidget()
        self._search_view.spreadsheet_activated.connect(
            self._on_spreadsheet_activated
        )
        self._content_stack.addWidget(self._spreadsheet_list)    # index 0
        self._content_stack.addWidget(self._spreadsheet_detail)  # index 1
        self._content_stack.addWidget(self._playlist_list)       # index 2
        self._content_stack.addWidget(self._search_view)         # index 3
        content_layout.addWidget(self._content_stack)

        self._splitter.addWidget(sidebar)
        self._splitter.addWidget(content)
        self._splitter.setStretchFactor(0, 0)
        self._splitter.setStretchFactor(1, 1)
        self._splitter.setSizes([260, 940])

        self.setCentralWidget(self._splitter)

    def _build_log_dock(self) -> None:
        self._log_view = QPlainTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setMaximumBlockCount(5000)

        self._log_dock = QDockWidget("Log", self)
        self._log_dock.setAllowedAreas(
            Qt.DockWidgetArea.BottomDockWidgetArea
            | Qt.DockWidgetArea.TopDockWidgetArea
        )
        self._log_dock.setWidget(self._log_view)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self._log_dock)

    def _build_now_playing_dock(self) -> None:
        self._now_playing = NowPlayingDock(self)
        self.addDockWidget(
            Qt.DockWidgetArea.BottomDockWidgetArea, self._now_playing
        )

    def _build_status_bar(self) -> None:
        self._status = QStatusBar()
        self.setStatusBar(self._status)

    def _wire_logging(self) -> None:
        self._log_emitter = QtLogEmitter()
        self._log_emitter.message.connect(self._append_log)
        handler = QtLogHandler(self._log_emitter)
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                              datefmt="%H:%M:%S")
        )
        logger.addHandler(handler)

    # --- State machine -----------------------------------------------------
    def _apply_state(self, state: str) -> None:
        self._state = state
        connected = state == self.STATE_CONNECTED
        # Export requires an active browser; disable any time we're not
        # cleanly connected, or while a task is running (see _task_running).
        self._export_action.setEnabled(connected and not self._task_running)
        # Merge/Compare don't need the browser — only gated by task_running.
        self._merge_action.setEnabled(not self._task_running)
        self._compare_action.setEnabled(not self._task_running)
        # Detail-view actions: Metadata/Filter are available when no task is
        # running; Update additionally needs Chrome connected.
        self._spreadsheet_detail.set_actions_enabled(
            not self._task_running, browser_connected=connected
        )
        # "Fetch from Amazon" needs the browser AND no other task running.
        self._playlist_list.set_fetch_enabled(connected and not self._task_running)
        if state == self.STATE_DISCONNECTED:
            self._connect_action.setEnabled(True)
            self._disconnect_action.setEnabled(False)
            self._status.showMessage("Disconnected")
        elif state == self.STATE_CONNECTING:
            self._connect_action.setEnabled(False)
            self._disconnect_action.setEnabled(False)
            self._status.showMessage("Launching Chrome…")
        elif state == self.STATE_CHECKING_LOGIN:
            self._connect_action.setEnabled(False)
            self._disconnect_action.setEnabled(False)
            self._status.showMessage("Checking Amazon sign-in…")
        elif state == self.STATE_CONNECTED:
            self._connect_action.setEnabled(False)
            self._disconnect_action.setEnabled(True)
            if not self._task_running:
                self._status.showMessage("Chrome connected")
        elif state == self.STATE_DISCONNECTING:
            self._connect_action.setEnabled(False)
            self._disconnect_action.setEnabled(False)
            self._status.showMessage("Closing Chrome…")

    # --- Slots -------------------------------------------------------------
    @pyqtSlot()
    def _on_connect_clicked(self) -> None:
        if self._thread is not None:
            return
        self._apply_state(self.STATE_CONNECTING)

        self._thread = QThread(self)
        self._worker = BrowserWorker()
        self._worker.keep_awake = QSettings(
            "VYSERIX", "AmazonMusicManager"
        ).value("session/keep_awake", True, type=bool)
        self._worker.moveToThread(self._thread)

        self._thread.started.connect(self._worker.run)
        self._worker.connected.connect(self._on_worker_connected)
        self._worker.failed.connect(self._on_worker_failed)
        self._worker.disconnected.connect(self._on_worker_disconnected)
        self._worker.task_completed.connect(self._on_task_completed)
        self._worker.task_failed.connect(self._on_task_failed)

        self._thread.start()

    @pyqtSlot(bool)
    def _on_keepawake_toggled(self, enabled: bool) -> None:
        """Persist the preference and apply it to the live session if connected."""
        QSettings("VYSERIX", "AmazonMusicManager").setValue(
            "session/keep_awake", enabled
        )
        worker = getattr(self, "_worker", None)
        if worker is not None:
            worker.set_keep_awake(enabled)

    @pyqtSlot(bool)
    def _on_autoconnect_toggled(self, enabled: bool) -> None:
        settings = QSettings("VYSERIX", "AmazonMusicManager")
        settings.setValue("connect/auto", enabled)

    @pyqtSlot()
    def _on_disconnect_clicked(self) -> None:
        if self._worker is None:
            return
        self._apply_state(self.STATE_DISCONNECTING)
        self._worker.request_stop()

    @pyqtSlot()
    def _on_worker_connected(self) -> None:
        self._apply_state(self.STATE_CHECKING_LOGIN)
        self._worker.submit_task(_login_check_coroutine, "login_check")

    @pyqtSlot(str)
    def _on_worker_failed(self, message: str) -> None:
        self._append_log(f"[ERROR] {message}")
        # disconnected will fire next and clean up the thread

    @pyqtSlot()
    def _on_worker_disconnected(self) -> None:
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(5000)
            self._thread = None
        self._worker = None
        self._apply_state(self.STATE_DISCONNECTED)

    @pyqtSlot(str)
    def _append_log(self, line: str) -> None:
        self._log_view.appendPlainText(line)

    @pyqtSlot()
    def _on_export_database(self) -> None:
        files = _collect_output_files()
        if not files:
            QMessageBox.information(self, "Export Database",
                                    "Nothing to export — the library is empty.")
            return
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        default_name = f"amm_backup_{ts}.zip"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Database", default_name,
            "ZIP archives (*.zip);;All files (*)"
        )
        if not path:
            return
        try:
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
                for fpath in files:
                    # Store the path relative to OUTPUT_DIR (forward slashes) so
                    # the org-tree folder structure is recreated on import,
                    # rather than flattening every file into one directory.
                    arc = os.path.relpath(fpath, OUTPUT_DIR).replace(os.sep, "/")
                    zf.write(fpath, arcname=arc)
            n = len(files)
            self._status.showMessage(
                f"Exported {n} file(s) to {os.path.basename(path)}", 8000
            )
            logger.info("Database exported to %s (%d files)", path, n)
        except Exception as exc:
            QMessageBox.critical(self, "Export Failed", str(exc))

    @pyqtSlot()
    def _on_import_database(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Import Database", "",
            "ZIP archives (*.zip);;All files (*)"
        )
        if not path:
            return
        dlg = ImportDatabaseDialog(path, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        names = dlg.importable_names()
        overwrite = dlg.overwrite_conflicts()
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        imported = skipped = 0
        try:
            out_root = os.path.abspath(OUTPUT_DIR)
            with zipfile.ZipFile(path, "r") as zf:
                for name in names:
                    dest = os.path.abspath(os.path.join(OUTPUT_DIR, name))
                    # Zip-slip guard: never write outside OUTPUT_DIR.
                    try:
                        if os.path.commonpath([out_root, dest]) != out_root:
                            skipped += 1
                            continue
                    except ValueError:
                        skipped += 1
                        continue
                    if os.path.exists(dest) and not overwrite:
                        skipped += 1
                        continue
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    with zf.open(name) as src, open(dest, "wb") as dst:
                        dst.write(src.read())
                    imported += 1
        except Exception as exc:
            QMessageBox.critical(self, "Import Failed", str(exc))
            return
        parts = [f"Imported {imported} file(s)"]
        if skipped:
            parts.append(f"skipped {skipped} conflict(s)")
        msg = ", ".join(parts) + "."
        self._status.showMessage(msg, 8000)
        logger.info("Database imported from %s: %s", path, msg)
        self._spreadsheet_list.refresh()
        self._org_tree.refresh()

    @pyqtSlot()
    def _on_export_clicked(self) -> None:
        if self._worker is None or self._task_running:
            return
        dialog = ExportDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        vals = dialog.values()
        if not vals["url"]:
            QMessageBox.warning(self, "Missing URL",
                                "Enter a playlist URL to export.")
            return
        if not vals["output"]:
            QMessageBox.warning(self, "Missing output path",
                                "Choose a file to save the spreadsheet to.")
            return

        self._task_running = True
        self._export_action.setEnabled(False)
        self._status.showMessage("Exporting playlist…")
        coro = _build_export_coroutine(vals["url"], vals["output"], vals["metadata"])
        self._worker.submit_task(coro, "export")

    @pyqtSlot(str, object)
    def _on_task_completed(self, task_name: str, result) -> None:
        # add_song and login_check are never gated by _task_running.
        if task_name not in ("add_song", "login_check"):
            self._task_running = False
        if task_name == "login_check":
            logged_in = bool((result or {}).get("logged_in"))
            if logged_in:
                self._apply_state(self.STATE_CONNECTED)
            else:
                dlg = LoginWaitDialog(self)
                if dlg.exec() == QDialog.DialogCode.Accepted:
                    self._status.showMessage("Verifying sign-in…")
                    self._worker.submit_task(_login_check_coroutine, "login_check")
                else:
                    self._on_disconnect_clicked()
            return
        elif task_name == "export":
            path = str(result) if result else ""
            logger.info("Export complete: %s", path)
            self._status.showMessage(
                f"Exported to {os.path.basename(path)}" if path else "Export complete",
                6000,
            )
            # Auto-file the new xlsx into the folder that triggered the export
            # (set when launched via "Export to spreadsheet…" on a playlist).
            target_folder = self._pending_export_folder
            self._pending_export_folder = None
            if path and target_folder is not None:
                try:
                    org = load_org()
                    node = get_node(org, target_folder)
                    if node is not None:
                        items = node.setdefault("items", [])
                        target_norm = _norm_path(path)
                        already = any(
                            e.get("kind") == "spreadsheet"
                            and _norm_path(
                                from_stored_path(e.get("path") or "")
                            ) == target_norm
                            for e in items
                        )
                        if not already:
                            items.append({
                                "kind": "spreadsheet",
                                "path": to_stored_path(path),
                                "name": os.path.basename(path),
                            })
                            save_org(org)
                except Exception:
                    logger.exception(
                        "Auto-filing exported xlsx into folder failed"
                    )
            self._spreadsheet_list.refresh()
            self._org_tree.refresh()
        elif task_name == "update":
            summary = result or {}
            added = summary.get("added", 0)
            total = summary.get("total", 0)
            path = summary.get("path", "")
            msg = (f"Added {added} new song(s); total {total}" if added
                   else f"Up to date ({total} songs)")
            self._status.showMessage(msg, 6000)
            logger.info("Update complete: %s", msg)
            self._spreadsheet_list.refresh()
            if path and self._spreadsheet_detail._current_path == path:
                # Reload the detail view so new rows show up.
                self._spreadsheet_detail.load(path)
        elif task_name == "metadata":
            summary = result or {}
            enriched = summary.get("enriched", 0)
            total = summary.get("total", 0)
            path = summary.get("path", "")
            msg = f"Metadata: {enriched}/{total} updated"
            self._status.showMessage(msg, 6000)
            self._spreadsheet_list.refresh()
            if path and self._spreadsheet_detail._current_path == path:
                self._spreadsheet_detail.load(path)
        elif task_name == "filter":
            summary = result or {}
            matched = summary.get("matched", 0)
            total = summary.get("total", 0)
            path = summary.get("path", "")
            msg = (f"Filter: {matched}/{total} songs saved to "
                   f"{os.path.basename(path)}" if matched
                   else "Filter: no songs matched — empty sheet saved")
            self._status.showMessage(msg, 8000)
            self._spreadsheet_list.refresh()
            self._org_tree.refresh()
        elif task_name == "merge":
            summary = result or {}
            added = summary.get("added", 0)
            target_total = summary.get("target_total", 0)
            path = summary.get("path", "")
            new_file = summary.get("new_file", False)
            if new_file:
                base = os.path.basename(path) if path else "new spreadsheet"
                msg = (f"Merged into {base}: {target_total} song(s) "
                       f"({added} from source)")
            else:
                msg = (f"Merge: added {added} song(s); target now has {target_total}"
                       if added else "Merge: no new songs (all already present)")
            self._status.showMessage(msg, 6000)
            self._spreadsheet_list.refresh()
            if path and self._spreadsheet_detail._current_path == path:
                self._spreadsheet_detail.load(path)
        elif task_name == "compare":
            dlg = CompareResultsDialog(result or {}, self)
            self._status.showMessage("Compare complete", 4000)
            dlg.exec()
        elif task_name == "duplicates":
            data = result or {}
            path = data.get("path", "")
            groups = data.get("groups", [])
            threshold = data.get("threshold", 0.85)
            if not groups:
                QMessageBox.information(
                    self, "Duplicates",
                    f"No potential duplicates found in "
                    f"'{os.path.basename(path)}'."
                )
                self._status.showMessage("Duplicates: none found", 4000)
            else:
                dlg = DuplicatesDialog(self, path, groups, threshold)
                if dlg.exec() == QDialog.DialogCode.Accepted:
                    summary = dlg.result_summary()
                    deleted = summary.get("deleted", 0)
                    dismissed = summary.get("dismissed", 0)
                    parts = []
                    if deleted:
                        parts.append(f"deleted {deleted} row"
                                     f"{'s' if deleted != 1 else ''}")
                    if dismissed:
                        parts.append(f"dismissed {dismissed} group"
                                     f"{'s' if dismissed != 1 else ''}")
                    self._status.showMessage(
                        "Duplicates: " + (", ".join(parts) if parts else "no changes"),
                        6000,
                    )
                    if (
                        path
                        and self._spreadsheet_detail._current_path == path
                    ):
                        self._spreadsheet_detail.load(path)
                    self._spreadsheet_list.refresh()
                else:
                    self._status.showMessage("Duplicates: cancelled", 4000)
        elif task_name == "play":
            summary = result or {}
            name = summary.get("name", "") or ""
            self._release_playback()
            self._status.showMessage(
                f"Stopped: {name}" if name else "Playback stopped", 4000
            )
        elif task_name == "list_playlists":
            count = int((result or {}).get("count", 0) or 0)
            fetched_urls = set((result or {}).get("urls", []) or [])
            self._playlist_list.set_fetch_enabled(True)
            try:
                from playlist_cache import _CACHE_FILE
                import json as _json
                from organization import (
                    sync_playlist_names, prune_playlists_not_in,
                )
                if os.path.exists(_CACHE_FILE):
                    with open(_CACHE_FILE, "r", encoding="utf-8") as _f:
                        _cache = _json.load(_f)
                    _org = load_org()
                    changed = sync_playlist_names(_org, _cache.get("playlists", []) or [])
                    if fetched_urls:
                        pruned = prune_playlists_not_in(_org, fetched_urls)
                        if pruned:
                            logger.info(
                                "Pruned %d stale playlist entr%s from organization.json",
                                pruned, "y" if pruned == 1 else "ies",
                            )
                            changed = True
                    if changed:
                        save_org(_org)
            except Exception:
                logger.exception("Could not sync/prune organization.json after fetch")
            self._playlist_list.refresh()
            self._org_tree.refresh()
            self._status.showMessage(
                f"Fetched {count} playlist{'s' if count != 1 else ''} "
                "from Amazon Music",
                6000,
            )
        elif task_name == "delete_playlist":
            summary = result or {}
            name = summary.get("name", "") or ""
            deleted_url = summary.get("url", "") or ""
            xlsx_to_delete = self._pending_delete_playlist_xlsx
            self._pending_delete_playlist_xlsx = []
            if deleted_url:
                try:
                    from playlist_cache import load_cached_playlists, save_cached_playlists
                    cached = load_cached_playlists() or []
                    cached = [p for p in cached if p.get("url") != deleted_url]
                    save_cached_playlists(cached)
                except Exception:
                    logger.exception("Could not update playlist cache after deletion")
                try:
                    from organization import remove_playlist_by_url
                    org = load_org()
                    removed = remove_playlist_by_url(org, deleted_url)
                    if removed:
                        save_org(org)
                        logger.info("Removed %d org entry/entries for %s", removed, deleted_url)
                except Exception:
                    logger.exception("Could not remove playlist from organization after deletion")
            xlsx_failures: list[tuple[str, str]] = []
            xlsx_removed = 0
            if xlsx_to_delete:
                try:
                    org = load_org()
                    for p in xlsx_to_delete:
                        self._org_tree._strip_spreadsheet_refs(
                            org.get("root", {}), _norm_path(p)
                        )
                    save_org(org)
                except Exception:
                    logger.exception("Could not strip xlsx refs from organization")
                for p in xlsx_to_delete:
                    try:
                        if os.path.exists(p):
                            os.remove(p)
                        xlsx_removed += 1
                    except OSError as e:
                        xlsx_failures.append((os.path.basename(p), str(e)))
                self._spreadsheet_list.refresh()
            self._playlist_list.refresh()
            self._org_tree.refresh()
            base = (
                f"Deleted playlist '{name}'" if name else "Playlist deleted"
            )
            if xlsx_removed:
                base += (
                    f" (also removed {xlsx_removed} cached "
                    f"spreadsheet{'s' if xlsx_removed != 1 else ''})"
                )
            self._status.showMessage(base, 6000)
            if xlsx_failures:
                joined = "\n".join(
                    f"  • {n}: {err}" for n, err in xlsx_failures[:8]
                )
                QMessageBox.warning(
                    self, "Some spreadsheets could not be deleted",
                    f"{len(xlsx_failures)} file(s) failed to delete:\n\n{joined}"
                )
        elif task_name == "bulk_delete_playlists":
            summary = result or {}
            deleted = summary.get("deleted") or []
            failed = summary.get("failed") or []
            cancelled = bool(summary.get("cancelled"))
            total = int(summary.get("total", 0) or 0)
            xlsx_to_delete = self._pending_bulk_delete_xlsx
            self._pending_bulk_delete_xlsx = []

            # Close the progress dialog if it's still open.
            if self._bulk_delete_progress is not None:
                try:
                    self._bulk_delete_progress.close()
                except Exception:
                    pass
                self._bulk_delete_progress = None
            self._bulk_delete_controller = None

            # Cleanup pass: cache + organization + xlsx files for the
            # playlists that actually got deleted from Amazon.
            deleted_urls = {(d.get("url") or "") for d in deleted if d.get("url")}
            if deleted_urls:
                try:
                    from playlist_cache import (
                        load_cached_playlists, save_cached_playlists,
                    )
                    cached = load_cached_playlists() or []
                    cached = [
                        p for p in cached
                        if (p.get("url") or "") not in deleted_urls
                    ]
                    save_cached_playlists(cached)
                except Exception:
                    logger.exception(
                        "Could not update playlist cache after bulk deletion"
                    )
                try:
                    from organization import remove_playlist_by_url
                    org = load_org()
                    total_removed = 0
                    for u in deleted_urls:
                        total_removed += remove_playlist_by_url(org, u)
                    if total_removed:
                        save_org(org)
                        logger.info(
                            "Removed %d org entry/entries across %d deleted playlists",
                            total_removed, len(deleted_urls),
                        )
                except Exception:
                    logger.exception(
                        "Could not remove playlists from organization "
                        "after bulk deletion"
                    )

            # Only delete cached xlsx whose source_url matches a successfully
            # deleted playlist (so a failed Amazon delete doesn't wipe its xlsx).
            xlsx_failures: list[tuple[str, str]] = []
            xlsx_removed = 0
            xlsx_safe: list[str] = []
            if xlsx_to_delete and deleted_urls:
                for p in xlsx_to_delete:
                    try:
                        src_url = (get_source(p) or {}).get("url") or ""
                    except Exception:
                        src_url = ""
                    if src_url and src_url in deleted_urls:
                        xlsx_safe.append(p)
            if xlsx_safe:
                try:
                    org = load_org()
                    for p in xlsx_safe:
                        self._org_tree._strip_spreadsheet_refs(
                            org.get("root", {}), _norm_path(p)
                        )
                    save_org(org)
                except Exception:
                    logger.exception(
                        "Could not strip xlsx refs from organization "
                        "after bulk deletion"
                    )
                for p in xlsx_safe:
                    try:
                        if os.path.exists(p):
                            os.remove(p)
                        xlsx_removed += 1
                    except OSError as e:
                        xlsx_failures.append((os.path.basename(p), str(e)))
                self._spreadsheet_list.refresh()

            self._playlist_list.refresh()
            self._org_tree.refresh()

            n_ok = len(deleted)
            n_fail = len(failed)
            prefix = "Cancelled — " if cancelled else ""
            base = (
                f"{prefix}Deleted {n_ok} of {total} "
                f"playlist{'s' if total != 1 else ''}"
            )
            if n_fail:
                base += f" ({n_fail} failed)"
            if xlsx_removed:
                base += (
                    f"; removed {xlsx_removed} cached "
                    f"spreadsheet{'s' if xlsx_removed != 1 else ''}"
                )
            self._status.showMessage(base, 8000)

            if failed:
                shown = failed[:8]
                more = len(failed) - len(shown)
                lines = []
                for f in shown:
                    nm = f.get("name") or f.get("url") or "(unknown)"
                    err = f.get("err") or ""
                    if len(err) > 200:
                        err = err[:197] + "…"
                    lines.append(f"  • {nm}: {err}")
                if more > 0:
                    lines.append(f"  …and {more} more")
                joined = "\n".join(lines)
                QMessageBox.warning(
                    self, "Some playlists could not be deleted",
                    f"{n_fail} of {total} playlist{'s' if total != 1 else ''} "
                    f"failed to delete:\n\n{joined}"
                )
            if xlsx_failures:
                joined = "\n".join(
                    f"  • {n}: {err}" for n, err in xlsx_failures[:8]
                )
                QMessageBox.warning(
                    self, "Some spreadsheets could not be deleted",
                    f"{len(xlsx_failures)} file(s) failed to delete:\n\n{joined}"
                )
        elif task_name == "delete_songs":
            summary = result or {}
            deleted = int(summary.get("deleted", 0) or 0)
            total = int(summary.get("total", 0) or 0)
            if deleted == total:
                msg = (f"Removed {deleted} song{'s' if deleted != 1 else ''} "
                       "from Amazon Music playlist")
            else:
                msg = (f"Removed {deleted}/{total} songs from Amazon Music playlist "
                       f"({total - deleted} not found)")
            self._status.showMessage(msg, 8000)
        elif task_name == "web_restricted_remove":
            # Result mirrors delete_songs. On any successful deletion, clear
            # the (playlist_url, requested_asin) entry from the web_restricted
            # log AND refresh the review dialog if it's open.
            summary = result or {}
            deleted = int(summary.get("deleted", 0) or 0)
            total = int(summary.get("total", 0) or 0)
            pending = getattr(self, "_pending_web_restricted_remove", None) or {}
            self._pending_web_restricted_remove = None
            if deleted >= 1 and pending.get("playlist_url"):
                try:
                    import web_restricted
                    web_restricted.remove(
                        playlist_url=pending["playlist_url"],
                        requested_asin=pending.get("requested_asin", ""),
                    )
                except Exception:
                    logger.exception("web_restricted.remove failed (non-fatal)")
            dlg = getattr(self, "_web_restricted_dlg", None)
            if dlg is not None and dlg.isVisible():
                dlg.refresh()
            if deleted == total and total > 0:
                msg = (f"Removed {deleted} web-restricted track"
                       f"{'s' if deleted != 1 else ''} from Amazon Music")
            elif deleted == 0:
                msg = ("No tracks removed (none matched the playlist on "
                       "Amazon — try Refresh on the review dialog)")
            else:
                msg = (f"Removed {deleted}/{total} web-restricted tracks "
                       f"({total - deleted} not found)")
            self._status.showMessage(msg, 8000)
        elif task_name == "delete_songs_both":
            summary = result or {}
            deleted = int(summary.get("deleted", 0) or 0)
            total = int(summary.get("total", 0) or 0)
            deleted_rows = summary.get("deleted_rows") or []
            path = summary.get("path") or ""
            # Delete from spreadsheet only the rows that Amazon confirmed removed.
            if deleted_rows and path:
                try:
                    _delete_rows_from_xlsx(path, deleted_rows)
                except Exception as e:
                    logger.exception("Failed to remove rows from spreadsheet after Amazon delete")
                    QMessageBox.warning(
                        self, "Spreadsheet update failed",
                        f"Removed {deleted} song{'s' if deleted != 1 else ''} from Amazon Music "
                        f"but could not update the spreadsheet:\n{e}"
                    )
                else:
                    if (
                        path
                        and self._spreadsheet_detail._current_path
                        and os.path.abspath(self._spreadsheet_detail._current_path)
                        == os.path.abspath(path)
                    ):
                        self._spreadsheet_detail.load(path)
            skipped = total - deleted
            if deleted == total:
                msg = (f"Removed {deleted} song{'s' if deleted != 1 else ''} "
                       "from Amazon Music playlist and spreadsheet")
            elif deleted == 0:
                msg = f"No songs removed — {total} not found in Amazon Music playlist"
            else:
                msg = (f"Removed {deleted}/{total} songs from Amazon Music and spreadsheet; "
                       f"{skipped} not found in playlist (kept in spreadsheet)")
            self._status.showMessage(msg, 8000)
        elif task_name == "reconcile_scrape_for_xlsx":
            playlist_songs = (result or {}).get("playlist_songs") or []
            path = self._reconcile_path
            xlsx_songs = self._reconcile_xlsx_songs
            playlist_name = self._reconcile_playlist_name
            diff = _compute_reconcile_diff(playlist_songs, xlsx_songs)
            dlg = ReconcilePreviewDialog(
                f"Make spreadsheet match '{playlist_name}'  (Spreadsheet ← Playlist)",
                to_add=diff["only_in_playlist"],
                to_remove=diff["only_in_xlsx"],
                parent=self,
            )
            if dlg.exec() == QDialog.DialogCode.Accepted:
                self._apply_reconcile_from_playlist(path, diff, playlist_name)
            self._reconcile_path = ""
            self._reconcile_xlsx_songs = []
        elif task_name == "reconcile_scrape_for_amazon":
            playlist_songs = (result or {}).get("playlist_songs") or []
            path = self._reconcile_path
            xlsx_songs = self._reconcile_xlsx_songs
            playlist_name = self._reconcile_playlist_name
            playlist_url = self._reconcile_playlist_url
            diff = _compute_reconcile_diff(playlist_songs, xlsx_songs)
            to_add = diff["only_in_xlsx"]      # in xlsx but not playlist → add to Amazon
            to_remove = diff["only_in_playlist"]  # in playlist but not xlsx → remove from Amazon

            dlg = ReconcilePreviewDialog(
                f"Make playlist match '{playlist_name}'  (Playlist ← Spreadsheet)",
                to_add=to_add,
                to_remove=to_remove,
                parent=self,
            )
            self._reconcile_path = ""
            self._reconcile_xlsx_songs = []
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return

            # Launch the apply task via the CreatePlaylist progress/controller
            # machinery so that ambiguous song searches get the picker dialog.
            controller = CreatePlaylistController()
            progress = CreatePlaylistProgressDialog(
                f"Reconcile: {playlist_name}", controller, self
            )
            self._create_controller = controller
            self._create_progress = progress

            # Compute occurrence_index for the songs-to-remove (playlist positions).
            occ_counts: dict[tuple, int] = {}
            songs_to_remove_with_idx = []
            for i, s in enumerate(to_remove):
                key = (s["title"].strip().lower(), s["artist"].strip().lower())
                occ = occ_counts.get(key, 0)
                songs_to_remove_with_idx.append({
                    "title": s["title"], "artist": s.get("artist", ""),
                    "occurrence_index": occ, "row": i,
                })
                occ_counts[key] = occ + 1

            threshold = 85
            coro = self._build_reconcile_to_amazon_coroutine(
                songs_to_add=to_add,
                songs_to_remove=songs_to_remove_with_idx,
                playlist_url=playlist_url,
                playlist_name=playlist_name,
                threshold=threshold,
                controller=controller,
            )
            self._task_running = True
            self._worker.submit_task(coro, "reconcile_apply_to_amazon")
            self._apply_state(self._state)
            progress.show()
        elif task_name == "reconcile_apply_to_amazon":
            summary = result or {}
            if self._create_progress is not None:
                self._create_progress.close()
                self._create_progress = None
            self._create_controller = None
            added = int(summary.get("added", 0) or 0)
            deleted = int(summary.get("deleted", 0) or 0)
            skipped = int(summary.get("skipped", 0) or 0)
            name = summary.get("name", "") or ""
            parts = []
            if deleted:
                parts.append(f"removed {deleted}")
            if added:
                parts.append(f"added {added}")
            if skipped:
                parts.append(f"skipped {skipped}")
            verb = "Cancelled" if summary.get("cancelled") else "Reconciled"
            self._status.showMessage(
                f"{verb} '{name}': {', '.join(parts or ['no changes'])}", 8000
            )
        elif task_name == "reorder_scrape_for_xlsx":
            playlist_songs = (result or {}).get("playlist_songs") or []
            path = self._reconcile_path
            playlist_name = self._reconcile_playlist_name
            self._reconcile_path = ""
            self._reconcile_xlsx_songs = []
            if not playlist_songs:
                QMessageBox.warning(self, "Reorder", "No tracks found in playlist.")
                return
            reply = QMessageBox.question(
                self,
                "Reorder spreadsheet",
                f"Reorder '{os.path.basename(path)}' rows to match "
                f"'{playlist_name}' ({len(playlist_songs)} tracks)?",
                QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            )
            if reply != QMessageBox.StandardButton.Ok:
                return
            try:
                moved = _reorder_xlsx_rows(path, playlist_songs)
            except Exception as e:
                logger.exception("Reorder xlsx failed")
                QMessageBox.critical(self, "Reorder failed", str(e))
                return
            if (
                self._spreadsheet_detail._current_path
                and os.path.abspath(self._spreadsheet_detail._current_path)
                == os.path.abspath(path)
            ):
                self._spreadsheet_detail.load(path)
            self._status.showMessage(
                f"Reordered '{os.path.basename(path)}': {moved} rows moved", 8000
            )
        elif task_name == "reorder_playlist_apply":
            summary = result or {}
            moved = int(summary.get("moved", 0) or 0)
            skipped = int(summary.get("skipped", 0) or 0)
            skipped_dup = int(summary.get("skipped_duplicates", 0) or 0)
            name = self._reconcile_playlist_name or ""
            self._reconcile_path = ""
            self._reconcile_xlsx_songs = []
            parts = [f"moved {moved}"]
            if skipped:
                parts.append(f"skipped {skipped}")
            if skipped_dup:
                parts.append(f"skipped {skipped_dup} duplicate-titled")
            self._status.showMessage(
                f"Reordered '{name}': {', '.join(parts)}", 8000
            )
        elif task_name == "add_song":
            summary = result or {}
            path = summary.get("path", "") or ""
            base = os.path.basename(path) if path else ""
            if summary.get("duplicate"):
                msg = f"Already in {base} — not added"
            elif summary.get("created"):
                msg = f"Created {base} and added the track"
            else:
                msg = f"Added to {base}"
            self._status.showMessage(msg, 6000)
            self._spreadsheet_list.refresh()
            # If the affected sheet is currently open, reload it.
            if (
                path
                and self._spreadsheet_detail._current_path
                and os.path.abspath(self._spreadsheet_detail._current_path)
                == os.path.abspath(path)
            ):
                try:
                    self._spreadsheet_detail.load(path)
                except Exception:
                    logger.exception("Reload after add_song failed: %s", path)
        elif task_name == "create_playlist":
            summary = result or {}
            name = summary.get("name", "") or ""
            added = int(summary.get("added", 0) or 0)
            total = int(summary.get("total", 0) or 0)
            skipped = int(summary.get("skipped", 0) or 0)
            cancelled = bool(summary.get("cancelled", False))
            skipped_list = summary.get("skipped_list", []) or []
            if self._create_progress is not None:
                self._create_progress.close()
                self._create_progress = None
            self._create_controller = None
            verb = "Cancelled" if cancelled else "Created"
            status_msg = f"{verb} '{name}': added {added}/{total}"
            if skipped:
                status_msg += f", skipped {skipped}"
            self._status.showMessage(status_msg, 8000)
            body = (f"<b>{verb} playlist '{name}'</b><br><br>"
                    f"Added: {added} of {total}<br>"
                    f"Skipped: {skipped}")
            if skipped_list:
                head = skipped_list[:30]
                rows = "<br>".join(
                    f"• {t} — {a}" if a else f"• {t}"
                    for t, a in head
                )
                body += "<br><br><b>Skipped songs:</b><br>" + rows
                if len(skipped_list) > 30:
                    body += f"<br>… and {len(skipped_list) - 30} more"
            box = QMessageBox(self)
            box.setWindowTitle("Create Playlist")
            box.setIcon(QMessageBox.Icon.Information)
            box.setTextFormat(Qt.TextFormat.RichText)
            box.setText(body)
            box.exec()
        elif task_name == "classical_shuffle_generate":
            summary = result or {}
            name = summary.get("name", "") or ""
            added = int(summary.get("added", 0) or 0)
            failed = int(summary.get("failed", 0) or 0)
            total_tracks = int(summary.get("total_tracks", 0) or 0)
            perfs = int(summary.get("performances", 0) or 0)
            cancelled = bool(summary.get("cancelled", False))
            filed = bool(summary.get("filed", False))
            if self._generate_shuffle_progress is not None:
                self._generate_shuffle_progress.close()
                self._generate_shuffle_progress = None
            self._generate_shuffle_controller = None
            verb = "Cancelled" if cancelled else "Generated"
            status_msg = (
                f"{verb} '{name}': {added}/{total_tracks} tracks "
                f"across {perfs} performance"
                f"{'s' if perfs != 1 else ''}"
            )
            if failed:
                status_msg += f", {failed} failed"
            self._status.showMessage(status_msg, 10000)
            # Refresh playlist list + org tree so the new playlist
            # surfaces immediately in the UI.
            try:
                self._playlist_list.refresh()
            except Exception:
                pass
            if filed:
                try:
                    self._org_tree.refresh()
                except Exception:
                    pass
            body = (
                f"<b>{verb} playlist '{name}'</b><br><br>"
                f"Performances sampled: {perfs}<br>"
                f"Tracks added: {added} of {total_tracks}<br>"
                f"Tracks failed: {failed}<br>"
                f"Filed in org tree: {'yes' if filed else 'no'}"
            )
            box = QMessageBox(self)
            box.setWindowTitle("Generate Classical Shuffle Playlist")
            box.setIcon(QMessageBox.Icon.Information)
            box.setTextFormat(Qt.TextFormat.RichText)
            box.setText(body)
            box.exec()
        self._apply_state(self._state)

    @pyqtSlot(str, str)
    def _on_task_failed(self, task_name: str, error: str) -> None:
        # add_song and login_check are never gated by _task_running.
        if task_name not in ("add_song", "login_check"):
            self._task_running = False
        if task_name == "login_check":
            logger.warning("Login check failed: %s", error)
            dlg = LoginWaitDialog(self)
            if dlg.exec() == QDialog.DialogCode.Accepted:
                self._status.showMessage("Verifying sign-in…")
                self._worker.submit_task(_login_check_coroutine, "login_check")
            else:
                self._on_disconnect_clicked()
            return
        elif task_name == "create_playlist":
            if self._create_progress is not None:
                self._create_progress.close()
                self._create_progress = None
            self._create_controller = None
        elif task_name in ("reconcile_scrape_for_xlsx", "reconcile_scrape_for_amazon"):
            self._reconcile_path = ""
            self._reconcile_xlsx_songs = []
        elif task_name == "reconcile_apply_to_amazon":
            if self._create_progress is not None:
                self._create_progress.close()
                self._create_progress = None
            self._create_controller = None
        elif task_name == "play":
            self._release_playback()
        elif task_name == "list_playlists":
            self._playlist_list.set_fetch_enabled(True)
        elif task_name == "delete_playlist":
            self._pending_delete_playlist_xlsx = []
        elif task_name == "web_restricted_remove":
            # Clear the pending entry on failure too — otherwise a retry
            # might replay the previous selection. Don't touch the log
            # (the track is still restricted; the removal just didn't go
            # through).
            self._pending_web_restricted_remove = None
            dlg = getattr(self, "_web_restricted_dlg", None)
            if dlg is not None and dlg.isVisible():
                dlg.refresh()
        elif task_name == "bulk_delete_playlists":
            self._pending_bulk_delete_xlsx = []
            if self._bulk_delete_progress is not None:
                try:
                    self._bulk_delete_progress.close()
                except Exception:
                    pass
                self._bulk_delete_progress = None
            self._bulk_delete_controller = None
        elif task_name == "classical_shuffle_generate":
            if self._generate_shuffle_progress is not None:
                try:
                    self._generate_shuffle_progress.close()
                except Exception:
                    pass
                self._generate_shuffle_progress = None
            self._generate_shuffle_controller = None
        label = {
            "export": "Export", "update": "Update", "metadata": "Metadata",
            "filter": "Filter", "merge": "Merge", "compare": "Compare",
            "duplicates": "Duplicates",
            "create_playlist": "Create Playlist",
            "play": "Play",
            "list_playlists": "Fetch Playlists",
            "delete_playlist": "Delete Playlist",
            "bulk_delete_playlists": "Delete Playlists",
            "delete_songs": "Delete from Amazon",
            "delete_songs_both": "Delete from Amazon and spreadsheet",
            "reconcile_scrape_for_xlsx": "Reconcile (fetch)",
            "reconcile_scrape_for_amazon": "Reconcile (fetch)",
            "reconcile_apply_to_amazon": "Reconcile (apply)",
            "add_song": "Add to spreadsheet",
            "classical_shuffle_generate": "Generate Classical Shuffle Playlist",
        }.get(task_name, task_name)
        self._status.showMessage(f"{label} failed: {error}", 8000)
        QMessageBox.critical(self, f"{label} failed", error)
        self._apply_state(self._state)

    @pyqtSlot(str)
    def _on_update_requested(self, path: str) -> None:
        if self._worker is None or self._task_running:
            return

        # If the spreadsheet has no stored source URL, try to find a matching
        # playlist by name before opening the dialog.
        src = {}
        try:
            src = get_source(path)
        except Exception:
            pass
        if not src.get("url"):
            # Prefer the name recorded in the sheet; fall back to the filename stem.
            lookup_name = src.get("name") or os.path.splitext(
                os.path.basename(path)
            )[0]
            matches = _find_playlists_by_name(lookup_name)
            if len(matches) == 1:
                m = matches[0]
                try:
                    from spreadsheet_tags import set_source as _set_source
                    _set_source(path, url=m["url"], name=m["name"])
                except Exception:
                    logger.exception("Could not store auto-matched source in %s", path)
                else:
                    logger.info(
                        "Auto-matched '%s' → %s and stored as source for %s",
                        m["name"], m["url"], os.path.basename(path),
                    )
            elif len(matches) > 1:
                # Multiple playlists share this name — let the user pick.
                items = [f"{m['name']}  ({m['url']})" for m in matches]
                choice, ok = QInputDialog.getItem(
                    self,
                    "Multiple playlists found",
                    f"Multiple playlists named \"{lookup_name}\" were found.\n"
                    "Select the one to use as the source for this spreadsheet:",
                    items,
                    0,
                    False,
                )
                if not ok:
                    return
                chosen = matches[items.index(choice)]
                try:
                    from spreadsheet_tags import set_source as _set_source
                    _set_source(path, url=chosen["url"], name=chosen["name"])
                except Exception:
                    logger.exception("Could not store chosen source in %s", path)

        dialog = UpdateDialog(path, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        vals = dialog.values()
        if not vals["url"]:
            QMessageBox.warning(
                self, "Playlist URL required",
                "This spreadsheet has no stored source playlist. Enter a "
                "playlist URL in the Update dialog."
            )
            return
        self._task_running = True
        self._status.showMessage("Updating playlist…")
        coro = _build_update_coroutine(path, vals["url"], vals["metadata"])
        self._worker.submit_task(coro, "update")
        self._apply_state(self._state)

    @pyqtSlot(str)
    def _on_duplicates_requested(self, path: str) -> None:
        if self._task_running:
            return
        self._task_running = True
        self._status.showMessage("Scanning for duplicates…")
        self._background.submit(
            lambda: _run_duplicates_scan_task(path, 0.85),
            "duplicates",
        )
        self._apply_state(self._state)

    @pyqtSlot(str)
    def _on_curate_requested(self, path: str) -> None:
        if self._task_running:
            return
        dlg = CurateDialog(self, path)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            self._status.showMessage("Curate: cancelled", 4000)
            return
        summary = dlg.result_summary()
        written = summary.get("written", 0)
        skipped = summary.get("skipped", 0)
        dest_path = summary.get("path", "")
        dest_is_new = summary.get("dest_is_new", False)
        base = os.path.basename(dest_path) if dest_path else ""
        verb = "Saved" if dest_is_new else "Appended"
        msg_parts = [f"{verb} {written} song{'s' if written != 1 else ''} to {base}"]
        if skipped:
            msg_parts.append(f"{skipped} duplicate(s) skipped")
        self._status.showMessage("Curate: " + " · ".join(msg_parts), 6000)
        self._spreadsheet_list.refresh()
        self._org_tree.refresh()

    @pyqtSlot(str)
    def _on_create_requested(self, path: str) -> None:
        if self._worker is None or self._task_running:
            return
        try:
            songs = _read_curate_songs(path)
        except Exception as e:
            logger.exception("Could not read source spreadsheet")
            QMessageBox.critical(
                self, "Create Playlist",
                f"Couldn't read {os.path.basename(path)}: {e}",
            )
            return
        if not songs:
            QMessageBox.information(
                self, "Create Playlist",
                f"'{os.path.basename(path)}' has no songs to add."
            )
            return

        dlg = CreatePlaylistDialog(self, path, len(songs))
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        vals = dlg.values()
        if not vals["name"]:
            QMessageBox.warning(
                self, "Create Playlist",
                "Enter a name for the new playlist."
            )
            return

        controller = CreatePlaylistController()
        progress = CreatePlaylistProgressDialog(self, vals["name"], len(songs))
        controller.progress.connect(progress.update_progress)
        controller.log.connect(progress.append_log)
        controller.ask_match.connect(self._on_match_request)
        controller.finished.connect(lambda _: self._playlist_list.refresh())
        progress.cancel_requested.connect(controller.request_cancel)

        self._create_controller = controller
        self._create_progress = progress

        self._task_running = True
        self._status.showMessage(f"Creating playlist '{vals['name']}'…")
        coro = _build_create_coroutine(
            songs, vals["name"], vals["threshold"], controller
        )
        self._worker.submit_task(coro, "create_playlist")
        self._apply_state(self._state)
        progress.show()

    @pyqtSlot(object)
    def _on_match_request(self, payload: object) -> None:
        if self._create_controller is None:
            return
        data = payload if isinstance(payload, dict) else {}
        title = data.get("title", "") or ""
        artist = data.get("artist", "") or ""
        candidates = data.get("candidates", []) or []
        dlg = MatchPickerDialog(self, title, artist, candidates)
        result = dlg.exec()
        if self._create_controller is None:
            return
        if result == QDialog.DialogCode.Accepted:
            idx = dlg.selected_index()
        else:
            idx = None
        self._create_controller.resolve_match(idx)

    @pyqtSlot(str, int)
    def _on_rows_deleted(self, path: str, count: int) -> None:
        """Detail view finished deleting rows — refresh the flat list."""
        del path
        self._status.showMessage(
            f"Deleted {count} row{'s' if count != 1 else ''}", 4000
        )
        self._spreadsheet_list.refresh()

    @pyqtSlot(str, object)
    def _on_delete_from_amazon_requested(self, path: str, songs: list) -> None:
        if self._worker is None or self._state != self.STATE_CONNECTED:
            QMessageBox.warning(
                self, "Delete from Amazon",
                "Connect Chrome first, then try again."
            )
            return
        if self._task_running:
            QMessageBox.information(
                self, "Delete from Amazon",
                "Wait for the current task to finish, then try again."
            )
            return

        # Get the playlist URL from the spreadsheet's source tag.
        src = {}
        try:
            src = get_source(path)
        except Exception:
            pass
        playlist_url = src.get("url") or ""
        playlist_name = src.get("name") or os.path.splitext(os.path.basename(path))[0]

        if not playlist_url:
            QMessageBox.warning(
                self, "Delete from Amazon",
                f"No playlist URL is stored for '{os.path.basename(path)}'.\n\n"
                "Use 'Update' on this spreadsheet first to link it to a playlist, "
                "or open the playlist URL dialog via the Update button."
            )
            return

        n = len(songs)
        titles = "\n".join(
            f"  • {s['title']}" + (f" — {s['artist']}" if s.get("artist") else "")
            for s in songs[:10]
        )
        if n > 10:
            titles += f"\n  … and {n - 10} more"
        reply = QMessageBox.warning(
            self,
            "Delete from Amazon Music",
            f"Remove {n} song{'s' if n != 1 else ''} from "
            f"'{playlist_name}' on Amazon Music?\n\n{titles}\n\n"
            "This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self._task_running = True
        self._status.showMessage(
            f"Removing {n} song{'s' if n != 1 else ''} from '{playlist_name}'…"
        )

        async def _coro(page, _url=playlist_url, _songs=songs):
            from scraper import delete_songs_from_playlist
            deleted_rows = await delete_songs_from_playlist(page, _songs, playlist_url=_url)
            return {"deleted": len(deleted_rows or []), "total": len(_songs)}

        self._worker.submit_task(_coro, "delete_songs")
        self._apply_state(self._state)

    # ------------------------------------------------------------------
    # Web-restricted tracks review
    # ------------------------------------------------------------------

    def _on_review_web_restricted(self) -> None:
        """Open (or raise) the Review web-restricted tracks dialog.

        Re-uses an existing instance so the dialog remembers its size/sort
        across re-opens during a session."""
        dlg = getattr(self, "_web_restricted_dlg", None)
        if dlg is None:
            dlg = WebRestrictedReviewDialog(self)
            dlg.remove_requested.connect(self._on_web_restricted_remove_requested)
            self._web_restricted_dlg = dlg
        else:
            dlg.refresh()
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _on_web_restricted_remove_requested(
        self, playlist_url: str, playlist_name: str, songs: list, asin: str,
    ) -> None:
        """User confirmed; dispatch the Amazon-side delete via the task worker.

        On success, clear the entry from web_restricted.json and refresh the
        dialog. asin is the requested_asin from the log entry — used as part
        of the dedup key when removing the entry."""
        if self._worker is None or self._state != self.STATE_CONNECTED:
            QMessageBox.warning(
                self, "Remove web-restricted track",
                "Connect Chrome first, then try again."
            )
            return
        if self._task_running:
            QMessageBox.information(
                self, "Remove web-restricted track",
                "Wait for the current task to finish, then try again."
            )
            return
        if not playlist_url or not songs:
            return

        # Stash the entry-key so the task-completion handler can clear it
        # from the log only if the delete succeeded.
        self._pending_web_restricted_remove = {
            "playlist_url": playlist_url,
            "requested_asin": (asin or "").upper(),
        }

        n = len(songs)
        self._task_running = True
        display_name = playlist_name or playlist_url
        self._status.showMessage(
            f"Removing {n} web-restricted track"
            f"{'s' if n != 1 else ''} from '{display_name}'…"
        )

        async def _coro(page, _url=playlist_url, _songs=songs):
            from scraper import delete_songs_from_playlist
            deleted_rows = await delete_songs_from_playlist(
                page, _songs, playlist_url=_url,
            )
            return {
                "deleted": len(deleted_rows or []),
                "total": len(_songs),
            }

        self._worker.submit_task(_coro, "web_restricted_remove")
        self._apply_state(self._state)

    def _on_delete_from_amazon_confirmed(self, path: str, songs: list) -> None:
        """Like _on_delete_from_amazon_requested but skips the confirmation dialog.

        Called from the 'delete from both' flow where the user already confirmed.
        """
        if self._worker is None or self._state != self.STATE_CONNECTED:
            QMessageBox.warning(
                self, "Delete from Amazon",
                "Connect Chrome first, then try again."
            )
            return
        if self._task_running:
            QMessageBox.information(
                self, "Delete from Amazon",
                "Wait for the current task to finish, then try again."
            )
            return

        src = {}
        try:
            src = get_source(path)
        except Exception:
            pass
        playlist_url = src.get("url") or ""
        playlist_name = src.get("name") or os.path.splitext(os.path.basename(path))[0]

        if not playlist_url:
            QMessageBox.warning(
                self, "Delete from Amazon",
                f"No playlist URL is stored for '{os.path.basename(path)}'.\n\n"
                "Use 'Update' on this spreadsheet first to link it to a playlist, "
                "or open the playlist URL dialog via the Update button."
            )
            return

        n = len(songs)
        self._task_running = True
        self._status.showMessage(
            f"Removing {n} song{'s' if n != 1 else ''} from '{playlist_name}'…"
        )

        async def _coro(page, _url=playlist_url, _songs=songs, _path=path):
            from scraper import delete_songs_from_playlist
            deleted_rows = await delete_songs_from_playlist(page, _songs, playlist_url=_url)
            return {
                "deleted": len(deleted_rows or []),
                "total": len(_songs),
                "deleted_rows": sorted(deleted_rows or []),
                "path": _path,
            }

        self._worker.submit_task(_coro, "delete_songs_both")
        self._apply_state(self._state)

    # ------------------------------------------------------------------
    # Reconcile
    # ------------------------------------------------------------------

    def _get_playlist_src(self, path: str) -> tuple[str, str] | None:
        """Return (playlist_url, playlist_name) for *path*, or None if missing."""
        src = {}
        try:
            src = get_source(path)
        except Exception:
            pass
        url = src.get("url") or ""
        name = src.get("name") or os.path.splitext(os.path.basename(path))[0]
        if not url:
            QMessageBox.warning(
                self, "Reconcile",
                f"No playlist URL is stored for '{os.path.basename(path)}'.\n\n"
                "Use 'Update' on this spreadsheet first to link it to a playlist."
            )
            return None
        return url, name

    def _on_reconcile_from_playlist_requested(self, path: str) -> None:
        """Scrape the linked playlist then make the xlsx match it."""
        if self._worker is None or self._state != self.STATE_CONNECTED:
            QMessageBox.warning(self, "Reconcile", "Connect Chrome first.")
            return
        if self._task_running:
            QMessageBox.information(self, "Reconcile",
                                    "Wait for the current task to finish.")
            return
        src = self._get_playlist_src(path)
        if src is None:
            return
        playlist_url, playlist_name = src

        self._reconcile_path = path
        self._reconcile_playlist_url = playlist_url
        self._reconcile_playlist_name = playlist_name
        self._reconcile_xlsx_songs = _read_xlsx_songs_for_reconcile(path)

        self._task_running = True
        self._status.showMessage(f"Fetching '{playlist_name}' for reconcile…")

        async def _coro(page, _url=playlist_url):
            from scraper import scrape_playlist
            pl = await scrape_playlist(page, playlist_url=_url)
            return {
                "playlist_songs": [
                    {"title": s.title, "artist": s.artist, "album": s.album}
                    for s in (pl.songs if pl else [])
                ]
            }

        self._worker.submit_task(_coro, "reconcile_scrape_for_xlsx")
        self._apply_state(self._state)

    def _on_reconcile_to_playlist_requested(self, path: str) -> None:
        """Scrape the linked playlist then make it match the xlsx."""
        if self._worker is None or self._state != self.STATE_CONNECTED:
            QMessageBox.warning(self, "Reconcile", "Connect Chrome first.")
            return
        if self._task_running:
            QMessageBox.information(self, "Reconcile",
                                    "Wait for the current task to finish.")
            return
        src = self._get_playlist_src(path)
        if src is None:
            return
        playlist_url, playlist_name = src

        self._reconcile_path = path
        self._reconcile_playlist_url = playlist_url
        self._reconcile_playlist_name = playlist_name
        self._reconcile_xlsx_songs = _read_xlsx_songs_for_reconcile(path)

        self._task_running = True
        self._status.showMessage(f"Fetching '{playlist_name}' for reconcile…")

        async def _coro(page, _url=playlist_url):
            from scraper import scrape_playlist
            pl = await scrape_playlist(page, playlist_url=_url)
            return {
                "playlist_songs": [
                    {"title": s.title, "artist": s.artist, "album": s.album}
                    for s in (pl.songs if pl else [])
                ]
            }

        self._worker.submit_task(_coro, "reconcile_scrape_for_amazon")
        self._apply_state(self._state)

    def _on_reorder_xlsx_from_playlist_requested(self, path: str) -> None:
        """Scrape the playlist order then rewrite xlsx rows to match it."""
        if self._worker is None or self._state != self.STATE_CONNECTED:
            QMessageBox.warning(self, "Reorder", "Connect Chrome first.")
            return
        if self._task_running:
            QMessageBox.information(self, "Reorder",
                                    "Wait for the current task to finish.")
            return
        src = self._get_playlist_src(path)
        if src is None:
            return
        playlist_url, playlist_name = src

        self._reconcile_path = path
        self._reconcile_playlist_url = playlist_url
        self._reconcile_playlist_name = playlist_name
        self._reconcile_xlsx_songs = []

        self._task_running = True
        self._status.showMessage(f"Fetching '{playlist_name}' order…")

        async def _coro(page, _url=playlist_url):
            from scraper import scrape_playlist
            pl = await scrape_playlist(page, playlist_url=_url)
            return {
                "playlist_songs": [
                    {"title": s.title, "artist": s.artist, "album": s.album}
                    for s in (pl.songs if pl else [])
                ]
            }

        self._worker.submit_task(_coro, "reorder_scrape_for_xlsx")
        self._apply_state(self._state)

    def _on_reorder_playlist_from_xlsx_requested(self, path: str) -> None:
        """Read the xlsx row order then drag-reorder the Amazon playlist to match."""
        if self._worker is None or self._state != self.STATE_CONNECTED:
            QMessageBox.warning(self, "Reorder", "Connect Chrome first.")
            return
        if self._task_running:
            QMessageBox.information(self, "Reorder",
                                    "Wait for the current task to finish.")
            return
        src = self._get_playlist_src(path)
        if src is None:
            return
        playlist_url, playlist_name = src

        xlsx_songs = _read_xlsx_songs_for_reconcile(path)
        if not xlsx_songs:
            QMessageBox.warning(self, "Reorder",
                                "No songs found in spreadsheet.")
            return

        reply = QMessageBox.question(
            self,
            "Reorder playlist",
            f"Reorder '{playlist_name}' ({len(xlsx_songs)} tracks) "
            f"to match the spreadsheet row order?\n\n"
            f"This will drag-and-drop each out-of-place track in Amazon Music.",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
        )
        if reply != QMessageBox.StandardButton.Ok:
            return

        self._reconcile_path = path
        self._reconcile_playlist_url = playlist_url
        self._reconcile_playlist_name = playlist_name
        self._reconcile_xlsx_songs = xlsx_songs

        self._task_running = True
        self._status.showMessage(f"Reordering '{playlist_name}'…")

        async def _coro(page, _url=playlist_url, _songs=xlsx_songs):
            from scraper import reorder_playlist
            return await reorder_playlist(page, _songs, playlist_url=_url)

        self._worker.submit_task(_coro, "reorder_playlist_apply")
        self._apply_state(self._state)

    def _apply_reconcile_from_playlist(
        self,
        path: str,
        diff: dict,
        playlist_name: str,
    ) -> None:
        """After user confirms, apply xlsx-side changes for Spreadsheet ← Playlist."""
        to_add = diff["only_in_playlist"]
        to_remove = diff["only_in_xlsx"]
        try:
            if to_remove:
                rows_to_del = [s["row"] for s in to_remove]
                _delete_rows_from_xlsx(path, rows_to_del)
            if to_add:
                _append_songs_to_xlsx(path, to_add)
        except Exception as e:
            logger.exception("Reconcile xlsx apply failed")
            QMessageBox.critical(self, "Reconcile failed", str(e))
            return
        if (
            self._spreadsheet_detail._current_path
            and os.path.abspath(self._spreadsheet_detail._current_path)
            == os.path.abspath(path)
        ):
            self._spreadsheet_detail.load(path)
        n_add, n_rem = len(to_add), len(to_remove)
        parts = []
        if n_add:
            parts.append(f"added {n_add}")
        if n_rem:
            parts.append(f"removed {n_rem}")
        self._status.showMessage(
            f"Reconciled '{os.path.basename(path)}': {', '.join(parts or ['no changes'])}",
            8000,
        )

    def _build_reconcile_to_amazon_coroutine(
        self,
        songs_to_add: list[dict],
        songs_to_remove: list[dict],
        playlist_url: str,
        playlist_name: str,
        threshold: int,
        controller,
    ):
        """Return a coroutine that deletes surplus songs then adds missing ones."""
        async def _do(page):
            from importer import _add_song_to_playlist
            from search import score_match, search_songs
            from scraper import delete_songs_from_playlist

            total_del = len(songs_to_remove)
            total_add = len(songs_to_add)
            total = total_del + total_add
            controller._loop = asyncio.get_running_loop()

            # --- Step 1: delete surplus songs ---
            if songs_to_remove:
                controller.progress.emit(0, total, f"Removing {total_del} surplus songs…")
                await delete_songs_from_playlist(
                    page, songs_to_remove, playlist_url=playlist_url
                )
                controller.log.emit(f"Removed {total_del} song(s) from playlist.")

            # --- Step 2: add missing songs ---
            added = 0
            skipped = 0
            skipped_list: list[tuple[str, str]] = []
            generic_artists = {
                "various artists", "various", "soundtrack",
                "soundtrack/cast album", "original soundtrack", "ost", "cast",
            }

            async def ask_user(t, a, cands):
                loop = asyncio.get_running_loop()
                fut = loop.create_future()
                controller._pending_future = fut
                controller.ask_match.emit({
                    "title": t, "artist": a,
                    "candidates": [
                        {"title": c.title, "artist": c.artist,
                         "album": c.album, "score": c.score}
                        for c in cands
                    ],
                })
                return await fut

            for idx, song in enumerate(songs_to_add, 1):
                if controller.cancelled:
                    controller.log.emit("Cancelled by user.")
                    break
                title, artist = song["title"], song.get("artist", "")
                label = f"{title} — {artist}" if artist else title
                controller.progress.emit(
                    total_del + idx, total, f"[{idx}/{total_add}] {label}"
                )
                query = title if (artist or "").strip().lower() in generic_artists \
                    else f"{title} {artist}".strip()
                try:
                    cands = await search_songs(page, query)
                except Exception as e:
                    controller.log.emit(f"  Search error for {label}: {e}")
                    skipped += 1
                    skipped_list.append((title, artist))
                    continue
                if not cands:
                    controller.log.emit(f"  No results for {label}.")
                    skipped += 1
                    skipped_list.append((title, artist))
                    continue
                for c in cands:
                    c.score = score_match(title, artist, c)
                cands.sort(key=lambda c: c.score, reverse=True)
                best = cands[0]
                if best.score >= threshold:
                    chosen = 0
                    controller.log.emit(
                        f"  Auto-picked: {best.title} — {best.artist} (score {best.score})"
                    )
                else:
                    pick_idx = await ask_user(title, artist, cands[:8])
                    if controller.cancelled or pick_idx is None:
                        controller.log.emit(f"  Skipped {label}.")
                        skipped += 1
                        skipped_list.append((title, artist))
                        continue
                    chosen = pick_idx
                    picked = cands[chosen]
                    controller.log.emit(
                        f"  You picked: {picked.title} — {picked.artist} (score {picked.score})"
                    )
                picked = cands[chosen]
                try:
                    ok = await _add_song_to_playlist(page, picked, playlist_name)
                except Exception as e:
                    controller.log.emit(f"  Add failed for {label}: {e}")
                    skipped += 1
                    skipped_list.append((title, artist))
                    continue
                if ok:
                    added += 1
                    controller.log.emit("  Added.")
                else:
                    skipped += 1
                    skipped_list.append((title, artist))
                    controller.log.emit(f"  Could not add {label}.")

            summary = {
                "name": playlist_name,
                "playlist_url": playlist_url,
                "deleted": total_del,
                "added": added,
                "skipped": skipped,
                "total": total,
                "skipped_list": skipped_list,
                "cancelled": controller.cancelled,
            }
            controller.finished.emit(summary)
            return summary

        return _do

    @pyqtSlot(str, str)
    def _on_spreadsheet_renamed(self, old_path: str, new_path: str) -> None:
        """Reload the detail view if it was showing the renamed file."""
        current = self._spreadsheet_detail._current_path
        if current and _norm_path(current) == _norm_path(old_path):
            self._spreadsheet_detail.load(new_path)
        self._status.showMessage(
            f"Renamed to {os.path.basename(new_path)}", 4000
        )

    @pyqtSlot(str)
    def _on_metadata_requested(self, path: str) -> None:
        if self._task_running:
            return
        dialog = MetadataDialog(path, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        vals = dialog.values()
        self._task_running = True
        self._status.showMessage("Looking up metadata…")
        self._background.submit(
            lambda: _run_metadata_task(path, vals["force_dates"]),
            "metadata",
        )
        self._apply_state(self._state)

    @pyqtSlot(str)
    def _on_filter_requested(self, path: str) -> None:
        if self._task_running:
            return
        dialog = FilterDialog(path, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        vals = dialog.values()
        if not vals["output"]:
            QMessageBox.warning(self, "Missing output path",
                                "Choose a file to save the filtered songs to.")
            return
        if not (vals["year"] or vals["genre"] or vals["artist"] or vals["keyword"]):
            QMessageBox.warning(self, "No filters set",
                                "Enter at least one filter field.")
            return
        self._task_running = True
        self._status.showMessage("Filtering songs…")
        self._background.submit(
            lambda: _run_filter_task(path, vals), "filter"
        )
        self._apply_state(self._state)

    @pyqtSlot()
    def _on_merge_clicked(self) -> None:
        if self._task_running:
            return
        default_target = self._spreadsheet_detail._current_path or ""
        if len(_scan_output_dir()) < 2:
            QMessageBox.information(
                self, "Merge",
                "Need at least 2 spreadsheets in ./output to merge."
            )
            return
        dialog = MergeDialog(self, default_target=default_target)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        vals = dialog.values()
        if not vals["target"] or not vals["source"]:
            QMessageBox.warning(self, "Merge",
                                "Select both a target and a source spreadsheet.")
            return
        if os.path.abspath(vals["target"]) == os.path.abspath(vals["source"]):
            QMessageBox.warning(self, "Merge",
                                "Target and source must be different files.")
            return
        new_path = vals.get("new_path", "")
        if new_path:
            # Validate the requested new filename isn't empty / colliding.
            if os.path.exists(new_path):
                QMessageBox.warning(
                    self, "Merge",
                    f"'{os.path.basename(new_path)}' already exists. "
                    "Pick a different name."
                )
                return
            target, source = vals["target"], vals["source"]
            self._task_running = True
            self._status.showMessage("Merging into new spreadsheet…")
            self._background.submit(
                lambda: _run_merge_to_new_task(target, source, new_path),
                "merge",
            )
        else:
            self._task_running = True
            self._status.showMessage("Merging spreadsheets…")
            self._background.submit(
                lambda: _run_merge_task(vals["target"], vals["source"]),
                "merge",
            )
        self._apply_state(self._state)

    @pyqtSlot()
    def _on_compare_clicked(self) -> None:
        if self._task_running:
            return
        default_first = self._spreadsheet_detail._current_path or ""
        if len(_scan_output_dir()) < 2:
            QMessageBox.information(
                self, "Compare",
                "Need at least 2 spreadsheets in ./output to compare."
            )
            return
        dialog = CompareDialog(self, default_first=default_first)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        vals = dialog.values()
        if not vals["a"] or not vals["b"]:
            QMessageBox.warning(self, "Compare",
                                "Select two spreadsheets.")
            return
        if os.path.abspath(vals["a"]) == os.path.abspath(vals["b"]):
            QMessageBox.warning(self, "Compare",
                                "Pick two different spreadsheets.")
            return
        self._task_running = True
        self._status.showMessage("Comparing spreadsheets…")
        self._background.submit(
            lambda: _run_compare_task(vals["a"], vals["b"]), "compare"
        )
        self._apply_state(self._state)

    @pyqtSlot(str)
    def _on_spreadsheet_activated(self, path: str) -> None:
        try:
            self._spreadsheet_detail.load(path)
        except Exception as e:
            logger.exception("Failed to open spreadsheet: %s", path)
            self._status.showMessage(f"Could not open {os.path.basename(path)}: {e}", 6000)
            return
        self._content_stack.setCurrentIndex(1)
        # A spreadsheet is now loaded — refresh action button enablement.
        self._apply_state(self._state)

    @pyqtSlot(str, str)
    def _on_playlist_add_to_folder(self, url: str, name: str) -> None:
        """User picked 'Add to folder…' on a playlist row — open the picker
        and file the playlist via the same drop handler the tree uses."""
        if not url:
            return
        dlg = _FolderPickerDialog(
            self,
            title="Add playlist to folder",
            prompt=f"File '{name or url}' under:",
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        path = dlg.chosen_path()
        if path is None:
            return
        self._org_tree._handle_playlist_drop(
            {"url": url, "name": name}, path,
        )

    @pyqtSlot(list)
    def _on_playlist_bulk_add_to_folder(self, items: list) -> None:
        """Bulk variant: file N playlists under one chosen folder. Opens the
        folder picker once and writes all entries in a single org save."""
        items = [it for it in items if (it or {}).get("url")]
        if not items:
            return
        n = len(items)
        dlg = _FolderPickerDialog(
            self,
            title="Add playlists to folder",
            prompt=f"File {n} playlists under:",
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        dest_path = dlg.chosen_path()
        if dest_path is None:
            return
        try:
            org = load_org()
            node = get_node(org, dest_path)
            if node is None:
                QMessageBox.warning(
                    self, "Cannot add",
                    f"Folder not found: /{'/'.join(dest_path)}"
                )
                return
            existing_urls = {
                e.get("url") or ""
                for e in (node.get("items") or [])
                if e.get("kind") == "playlist"
            }
            added = 0
            skipped = 0
            for it in items:
                url = it.get("url") or ""
                if not url:
                    continue
                if url in existing_urls:
                    skipped += 1
                    continue
                node.setdefault("items", []).append({
                    "kind": "playlist",
                    "url": url,
                    "name": it.get("name") or url,
                })
                existing_urls.add(url)
                added += 1
            save_org(org)
        except Exception as e:
            logger.exception("Bulk add to folder failed")
            QMessageBox.critical(self, "Add failed", str(e))
            return
        self._org_tree.refresh()
        self._playlist_list.refresh()
        msg = f"Filed {added} playlist{'s' if added != 1 else ''} under " \
              f"/{'/'.join(dest_path) or '(root)'}"
        if skipped:
            msg += f" ({skipped} already there)"
        self._status.showMessage(msg, 6000)

    @pyqtSlot(str, str, object)
    def _on_playlist_export_to_folder_requested(
        self, url: str, name: str, folder_path: list[str]
    ) -> None:
        """Right-click 'Export to spreadsheet…' on a playlist inside a folder.

        The default output path mirrors the org folder under OUTPUT_DIR (e.g.
        `./output/Classical/Piano Concertos/<name>.xlsx`). The exporter
        creates missing parent dirs. On success the new xlsx is auto-filed
        into the same org folder.
        """
        if self._worker is None or self._state != self.STATE_CONNECTED:
            QMessageBox.warning(
                self, "Export",
                "Connect Chrome first, then export."
            )
            return
        if self._task_running:
            QMessageBox.information(
                self, "Export",
                "Wait for the current task to finish, then try again."
            )
            return
        invalid = set(r'\/:*?"<>|')

        def _safe(s: str) -> str:
            return "".join(c for c in (s or "") if c not in invalid).strip()

        safe_name = _safe(name)
        safe_segments = [s for s in (_safe(seg) for seg in folder_path) if s]
        default_output = os.path.join(
            OUTPUT_DIR, *safe_segments, f"{safe_name or 'playlist'}.xlsx"
        )
        dialog = ExportDialog(
            self, prefill_url=url, prefill_output=default_output,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        vals = dialog.values()
        if not vals["url"] or not vals["output"]:
            return
        # Only stash after dialog acceptance — a cancel must not leave the
        # auto-file target hanging around for a later unrelated export.
        self._pending_export_folder = list(folder_path)
        self._task_running = True
        self._export_action.setEnabled(False)
        self._status.showMessage(f"Exporting '{name}'…")
        coro = _build_export_coroutine(
            vals["url"], vals["output"], vals["metadata"],
        )
        self._worker.submit_task(coro, "export")
        self._apply_state(self._state)

    @pyqtSlot(str, str)
    def _on_playlist_export_requested(self, url: str, name: str) -> None:
        if self._worker is None or self._state != self.STATE_CONNECTED:
            QMessageBox.warning(
                self, "Export",
                "Connect Chrome first, then export."
            )
            return
        if self._task_running:
            QMessageBox.information(
                self, "Export",
                "Wait for the current task to finish, then try again."
            )
            return
        # Strip characters invalid in filenames while preserving spaces/capitals.
        invalid = set(r'\/:*?"<>|')
        safe_name = "".join(c for c in (name or "") if c not in invalid).strip()
        default_output = os.path.join(
            OUTPUT_DIR, f"{safe_name or 'playlist'}.xlsx"
        )
        dialog = ExportDialog(
            self, prefill_url=url, prefill_output=default_output,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        vals = dialog.values()
        if not vals["url"] or not vals["output"]:
            return
        self._task_running = True
        self._export_action.setEnabled(False)
        self._status.showMessage(f"Exporting '{name}'…")
        coro = _build_export_coroutine(
            vals["url"], vals["output"], vals["metadata"],
        )
        self._worker.submit_task(coro, "export")
        self._apply_state(self._state)

    @pyqtSlot()
    def _on_add_current_to_sheet(self) -> None:
        title, artist, album = self._now_playing.current_track_meta()
        if not title:
            QMessageBox.information(
                self, "Add to spreadsheet",
                "No track is currently showing — wait for the player to "
                "report a song, then try again."
            )
            return
        # Intentionally NOT gated on _task_running — this is plain disk I/O
        # that runs on the BackgroundTasks pool, so it's safe to overlap with
        # an active playback (which is a long-lived browser-worker task).
        dialog = AddToSpreadsheetDialog(self, title, artist, album)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        vals = dialog.values()
        path = vals.get("path") or ""
        if not path:
            return
        is_new = bool(vals.get("is_new"))
        self._status.showMessage(f"Adding to {os.path.basename(path)}…")
        # Capture the values the lambda needs.
        self._background.submit(
            lambda p=path, t=title, a=artist, b=album, n=is_new:
                _append_song_to_sheet(p, t, a, b, n),
            "add_song",
        )

    @pyqtSlot()
    def _on_fetch_playlists_clicked(self) -> None:
        if self._worker is None or self._state != self.STATE_CONNECTED:
            QMessageBox.warning(
                self, "Fetch playlists",
                "Connect Chrome first, then click 'Fetch from Amazon'."
            )
            return
        if self._task_running:
            QMessageBox.information(
                self, "Fetch playlists",
                "Wait for the current task to finish, then try again."
            )
            return
        self._task_running = True
        self._playlist_list.set_fetch_enabled(False)
        self._status.showMessage("Fetching your Amazon Music playlists…")
        self._worker.submit_task(_list_playlists_coroutine, "list_playlists")
        self._apply_state(self._state)

    @pyqtSlot(str, str)
    def _on_delete_playlist_requested(self, url: str, name: str) -> None:
        if self._worker is None or self._state != self.STATE_CONNECTED:
            QMessageBox.warning(
                self, "Delete playlist",
                "Connect Chrome first, then try again."
            )
            return
        if self._task_running:
            QMessageBox.information(
                self, "Delete playlist",
                "Wait for the current task to finish, then try again."
            )
            return
        display = name or url
        xlsx_paths = _find_xlsx_for_playlist_url(url)
        dlg = DeletePlaylistDialog(self, display, xlsx_paths)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self._pending_delete_playlist_xlsx = (
            list(xlsx_paths) if dlg.delete_xlsx() else []
        )
        self._task_running = True
        self._status.showMessage(f"Deleting '{display}'…")

        async def _coro(page, _url=url, _name=name):
            from scraper import delete_playlist
            await delete_playlist(page, _url)
            return {"name": _name, "url": _url}

        self._worker.submit_task(_coro, "delete_playlist")
        self._apply_state(self._state)

    @pyqtSlot(list)
    def _on_bulk_delete_playlists_requested(self, items: list) -> None:
        """Confirm + run bulk Amazon-side delete across N playlists."""
        if self._worker is None or self._state != self.STATE_CONNECTED:
            QMessageBox.warning(
                self, "Delete playlists",
                "Connect Chrome first, then try again."
            )
            return
        if self._task_running:
            QMessageBox.information(
                self, "Delete playlists",
                "Wait for the current task to finish, then try again."
            )
            return
        items = [it for it in items if (it or {}).get("url")]
        if not items:
            return

        # Collect all cached xlsx across the selected playlists, deduped.
        xlsx_seen: set[str] = set()
        xlsx_paths: list[str] = []
        for it in items:
            for p in _find_xlsx_for_playlist_url(it["url"]):
                key = _norm_path(p)
                if key in xlsx_seen:
                    continue
                xlsx_seen.add(key)
                xlsx_paths.append(p)

        dlg = BulkDeletePlaylistDialog(self, items, xlsx_paths)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        self._pending_bulk_delete_xlsx = (
            list(xlsx_paths) if dlg.delete_xlsx() else []
        )

        controller = BulkDeleteController()
        progress = BulkDeleteProgressDialog(self, len(items))
        controller.progress.connect(progress.update_progress)
        controller.log.connect(progress.append_log)
        progress.cancel_requested.connect(controller.request_cancel)

        self._bulk_delete_controller = controller
        self._bulk_delete_progress = progress

        self._task_running = True
        self._status.showMessage(
            f"Deleting {len(items)} playlist{'s' if len(items) != 1 else ''}…"
        )
        coro = _build_bulk_delete_coroutine(items, controller)
        self._worker.submit_task(coro, "bulk_delete_playlists")
        self._apply_state(self._state)
        progress.show()

    @pyqtSlot(str, str)
    def _on_playlist_activated(self, url: str, name: str) -> None:
        if not url:
            QMessageBox.warning(
                self, "Play",
                "This playlist has no stored URL. Refresh the playlist list."
            )
            return
        display_name = name or "Playlist"
        if not self._can_start_playback("Play"):
            return
        dlg = PlayModeDialog(self, display_name)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self._start_play_queue(
            [{"name": display_name, "url": url}],
            shuffle=dlg.shuffle(),
            header_name=display_name,
        )

    @pyqtSlot(object, bool)
    def _on_play_folder(self, folder_path, recursive: bool) -> None:
        """User picked 'Play all in folder' / 'Play all (recursive)' from the
        org tree's context menu — collect playlist URLs (deduped) and start a
        queue, prompting for shuffle mode."""
        try:
            org = load_org()
        except Exception:
            QMessageBox.warning(
                self, "Play folder",
                "Could not load organization.json."
            )
            return
        path = list(folder_path or [])
        if recursive:
            playlists = list(iter_playlists_recursive(org, path))
        else:
            node = get_node(org, path) or {}
            playlists = [
                it for it in node.get("items", [])
                if it.get("kind") == "playlist"
            ]

        # Dedupe by URL, preserving folder order so the user gets predictable
        # playback for tightly-curated folders.
        items: list[dict] = []
        seen: set[str] = set()
        for it in playlists:
            url = (it.get("url") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            items.append({"name": it.get("name") or "Playlist", "url": url})

        if not items:
            QMessageBox.information(
                self, "Play folder",
                "No playlists in this folder"
                + (" or its subfolders." if recursive else ".")
            )
            return

        if not self._can_start_playback("Play folder"):
            return

        folder_label = path[-1] if path else "Root"
        scope = "all subfolders" if recursive else "folder"
        header_name = (
            f"{folder_label} · {len(items)} playlist"
            f"{'s' if len(items) != 1 else ''}"
            + (" (recursive)" if recursive else "")
        )

        dlg = PlayModeDialog(
            self, f"{folder_label} ({len(items)} playlists, {scope})",
            multi_playlist=True,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self._start_play_queue(
            items, shuffle=dlg.shuffle(), header_name=header_name,
            shuffle_queue=dlg.shuffle_queue(),
        )

    def _can_start_playback(self, op_label: str) -> bool:
        """Common preconditions for any Play action. Shows a message box on
        failure and returns False; True when it's safe to proceed."""
        if self._worker is None or self._state != self.STATE_CONNECTED:
            QMessageBox.warning(
                self, op_label,
                "Connect Chrome first, then try again."
            )
            return False
        if self._task_running:
            QMessageBox.information(
                self, op_label,
                "Stop the current playback (or wait for the running task) "
                "before starting a new one."
            )
            return False
        return True

    def _start_play_queue(
        self,
        items: list[dict],
        shuffle: bool,
        header_name: str,
        shuffle_queue: bool = False,
    ) -> None:
        """Wire signals, prime the dock, and submit a queue playback task.

        *items* is a list of ``{"name": str, "url": str}``; the queue plays
        them back-to-back with auto-advance. *header_name* is the label shown
        in the dock until the first ``playlist_advanced`` arrives — typically
        the first item's name (single play) or a folder summary
        ("Folder · 5 playlists").
        """
        if not items:
            return
        controller = PlaybackController()
        controller.track_changed.connect(self._now_playing.update_track)
        controller.track_meta.connect(self._now_playing.update_track_meta)
        controller.progress.connect(self._now_playing.update_progress)
        controller.status.connect(self._now_playing.update_status)
        controller.log.connect(self._append_log)
        controller.playlist_advanced.connect(
            self._now_playing.set_queue_position
        )
        # Transport actions: each dock signal maps to a request_action(...) call.
        self._now_playing.stop_requested.connect(controller.request_stop)
        self._now_playing.play_pause_requested.connect(
            lambda: controller.request_action("play_pause")
        )
        self._now_playing.next_requested.connect(
            lambda: controller.request_action("next")
        )
        self._now_playing.previous_requested.connect(
            lambda: controller.request_action("previous")
        )
        self._now_playing.prev_playlist_requested.connect(
            lambda: controller.request_action("prev_playlist")
        )
        self._now_playing.skip_playlist_requested.connect(
            lambda: controller.request_action("skip_playlist")
        )
        self._now_playing.seek_requested.connect(
            lambda secs: controller.request_action(("seek", secs))
        )
        self._now_playing.add_to_sheet_requested.connect(
            self._on_add_current_to_sheet
        )

        self._playback_controller = controller
        self._now_playing.begin(
            header_name, shuffle,
            queue_total=len(items),
            shuffle_queue=shuffle_queue,
        )

        self._task_running = True
        if len(items) == 1:
            status_msg = f"Playing '{header_name}'"
        else:
            status_msg = (
                f"Playing queue: {len(items)} playlist"
                f"{'s' if len(items) != 1 else ''} starting "
                f"with '{items[0].get('name', '')}'"
            )
        if shuffle:
            status_msg += " (shuffle)"
        if shuffle_queue:
            status_msg += " (playlists shuffled)"
        self._status.showMessage(status_msg)
        coro = _build_queue_coroutine(
            items, shuffle, controller, shuffle_queue=shuffle_queue
        )
        self._worker.submit_task(coro, "play")
        self._apply_state(self._state)

    def _start_classical_shuffle(self, folder_path: list[str]) -> None:
        """Build the performance pool from *folder_path*'s subtree and start
        Classical Shuffle playback. Must already pass `_can_start_playback`.
        """
        try:
            org = load_org()
        except Exception:
            QMessageBox.warning(
                self, "Classical Shuffle",
                "Could not load organization.json."
            )
            return

        performances, skipped, restricted_skip_count = (
            _build_classical_performance_pool(org, folder_path)
        )
        if not performances:
            msg = "No exportable performances found in this folder."
            if skipped:
                msg += "\n\nSkipped:\n  • " + "\n  • ".join(skipped[:8])
                if len(skipped) > 8:
                    msg += f"\n  • … and {len(skipped) - 8} more"
            if restricted_skip_count:
                msg += (
                    f"\n\n({restricted_skip_count} performance"
                    f"{'s' if restricted_skip_count != 1 else ''} "
                    "filtered out as web-restricted — see Tools → "
                    "Review web-restricted tracks…)"
                )
            QMessageBox.information(self, "Classical Shuffle", msg)
            return

        # Composer + piece-type filter. Skip the dialog when both axes are
        # pinned by scope (nothing for the user to pick); mirrors the
        # generator's filter logic (Step 6 of the playlist plan) so a perf
        # passes only when its playlist's composer tags intersect the
        # selection AND its piece-type tags do.
        scope_composer = _composer_from_org_path(folder_path)
        scope_piece_type = _piece_type_from_org_path(folder_path)
        composer_filter: set[str] | None = None
        piece_type_filter: set[str] | None = None
        if scope_composer is None or scope_piece_type is None:
            dlg = ClassicalShuffleFilterDialog(
                self, scope_path=folder_path, org=org,
            )
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            sc = dlg.selected_composers()
            sp = dlg.selected_piece_types()
            if isinstance(sc, list):
                composer_filter = {c for c in sc if c}
            if isinstance(sp, list):
                piece_type_filter = {p for p in sp if p}

        if composer_filter is not None:
            tag_map = _build_playlist_composer_map(org)
            before = len(performances)
            performances = [
                p for p in performances
                if tag_map.get(p.playlist_url, set()) & composer_filter
            ]
            dropped = before - len(performances)
            if dropped:
                self._append_log(
                    f"Classical Shuffle: composer filter dropped {dropped} "
                    f"performance{'s' if dropped != 1 else ''}."
                )
        if piece_type_filter is not None:
            type_map = _build_playlist_piece_type_map(org)
            before = len(performances)
            performances = [
                p for p in performances
                if type_map.get(p.playlist_url, set()) & piece_type_filter
            ]
            dropped = before - len(performances)
            if dropped:
                self._append_log(
                    f"Classical Shuffle: piece-type filter dropped {dropped} "
                    f"performance{'s' if dropped != 1 else ''}."
                )
        if not performances:
            QMessageBox.information(
                self, "Classical Shuffle",
                "No performances match the selected composer / piece-type "
                "filters."
            )
            return

        if not self._can_start_playback("Classical Shuffle"):
            return

        folder_label = folder_path[-1] if folder_path else "Classical"
        # Report the two skip categories separately — they mean very
        # different things (cf. 2026-05-25 confusion where the user
        # couldn't find a playlist matching the "1 playlist(s) without
        # cached xlsx" message because the only entry was actually the
        # web-restricted perf-filter summary).
        if skipped:
            self._append_log(
                f"Classical Shuffle: skipped {len(skipped)} playlist(s) "
                "(no cached xlsx, empty xlsx, or read error — export to "
                "include):"
            )
            for s in skipped[:8]:
                self._append_log(f"  • {s}")
            if len(skipped) > 8:
                self._append_log(f"  • … and {len(skipped) - 8} more")
        if restricted_skip_count:
            self._append_log(
                f"Classical Shuffle: filtered out {restricted_skip_count} "
                f"web-restricted performance"
                f"{'s' if restricted_skip_count != 1 else ''} "
                "(see Tools → Review web-restricted tracks…)."
            )

        controller = PlaybackController()
        controller.track_changed.connect(self._now_playing.update_track)
        controller.track_meta.connect(self._now_playing.update_track_meta)
        controller.progress.connect(self._now_playing.update_progress)
        controller.status.connect(self._now_playing.update_status)
        controller.log.connect(self._append_log)
        controller.playlist_advanced.connect(
            self._now_playing.set_queue_position
        )
        # Transport actions — only the meaningful ones; prev/skip-playlist
        # are routed to the controller too but the coroutine ignores them.
        self._now_playing.stop_requested.connect(controller.request_stop)
        self._now_playing.play_pause_requested.connect(
            lambda: controller.request_action("play_pause")
        )
        self._now_playing.next_requested.connect(
            lambda: controller.request_action("next")
        )
        self._now_playing.previous_requested.connect(
            lambda: controller.request_action("previous")
        )
        self._now_playing.prev_playlist_requested.connect(
            lambda: controller.request_action("prev_playlist")
        )
        self._now_playing.skip_playlist_requested.connect(
            lambda: controller.request_action("skip_playlist")
        )
        self._now_playing.seek_requested.connect(
            lambda secs: controller.request_action(("seek", secs))
        )
        self._now_playing.add_to_sheet_requested.connect(
            self._on_add_current_to_sheet
        )

        self._playback_controller = controller
        header = (
            f"Classical Shuffle · {folder_label} "
            f"({len(performances)} performances)"
        )
        # perf_shuffle=True keeps ⏪/⏩ visible and re-tooltipped as
        # "previous performance" / "skip to next performance" — the loop
        # below handles those actions to navigate the random pick history.
        self._now_playing.begin(
            header, shuffle=True, queue_total=1, perf_shuffle=True,
        )

        self._task_running = True
        self._status.showMessage(
            f"Classical Shuffle: {len(performances)} performances queued"
            f" from '{folder_label}'"
        )
        coro = _build_classical_shuffle_coroutine(
            performances, folder_label, controller
        )
        self._worker.submit_task(coro, "play")
        self._apply_state(self._state)

    def _open_classical_shuffle_generate_dialog(
        self, folder_path: list[str]
    ) -> None:
        """Open the generator dialog and, on accept, launch the Step 6
        build coroutine through the task worker.

        Mirrors the gating that the live Classical Shuffle uses: requires
        an active browser connection and no other task running, then
        creates a controller + progress dialog, submits the coroutine via
        ``submit_task("classical_shuffle_generate")``, and lets
        ``_on_task_completed`` / ``_on_task_failed`` clean up the dialog.
        """
        if self._worker is None or self._state != self.STATE_CONNECTED:
            QMessageBox.warning(
                self, "Generate Classical Shuffle Playlist",
                "Connect Chrome first, then try again."
            )
            return
        if self._task_running:
            QMessageBox.information(
                self, "Generate Classical Shuffle Playlist",
                "Wait for the current task to finish, then try again."
            )
            return
        try:
            org = load_org()
        except Exception:
            QMessageBox.warning(
                self, "Generate Classical Shuffle Playlist",
                "Could not load organization.json."
            )
            return
        dlg = ClassicalShufflePlaylistDialog(
            self, scope_path=list(folder_path), org=org
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        v = dlg.values()
        if not v["name"]:
            QMessageBox.warning(
                self, "Generate Classical Shuffle Playlist",
                "Playlist name is empty."
            )
            return
        # Empty composer selection in a multi-composer scope means "filter
        # out everything" — refuse rather than launch a guaranteed-empty
        # run. Single-composer scopes get composers=None and skip this
        # check entirely (the scope itself is the filter).
        composers = v["composers"]
        if isinstance(composers, list) and not composers:
            QMessageBox.warning(
                self, "Generate Classical Shuffle Playlist",
                "No composers selected — pick at least one or "
                "use 'Select all'."
            )
            return

        controller = GenerateShufflePlaylistController()
        # Progress dialog starts with total=1 as a placeholder; the
        # coroutine re-ranges the bar to the real track count once it
        # has sampled and read ASIN slices.
        progress = GenerateShufflePlaylistProgressDialog(
            self, v["name"], total=1
        )
        controller.progress.connect(progress.update_progress)
        controller.log.connect(progress.append_log)
        progress.cancel_requested.connect(controller.request_cancel)

        self._generate_shuffle_controller = controller
        self._generate_shuffle_progress = progress

        self._task_running = True
        self._status.showMessage(
            f"Generating Classical Shuffle playlist '{v['name']}'…"
        )
        coro = _build_classical_shuffle_generate_coroutine(v, controller)
        self._worker.submit_task(coro, "classical_shuffle_generate")
        self._apply_state(self._state)
        progress.show()

    def _release_playback(self) -> None:
        """Disconnect signals and clear playback state. Idempotent."""
        ctrl = self._playback_controller
        if ctrl is not None:
            for sig, slot in (
                (ctrl.track_changed, self._now_playing.update_track),
                (ctrl.track_meta, self._now_playing.update_track_meta),
                (ctrl.progress, self._now_playing.update_progress),
                (ctrl.status, self._now_playing.update_status),
                (ctrl.log, self._append_log),
                (ctrl.playlist_advanced,
                 self._now_playing.set_queue_position),
            ):
                try:
                    sig.disconnect(slot)
                except (RuntimeError, TypeError):
                    pass
            self._playback_controller = None
        # Drop *all* dock-side connections so the lambda-bound action handlers
        # from this run don't accumulate on the next playback.
        for sig in (
            self._now_playing.stop_requested,
            self._now_playing.play_pause_requested,
            self._now_playing.next_requested,
            self._now_playing.previous_requested,
            self._now_playing.prev_playlist_requested,
            self._now_playing.skip_playlist_requested,
            self._now_playing.seek_requested,
            self._now_playing.add_to_sheet_requested,
        ):
            try:
                sig.disconnect()
            except (RuntimeError, TypeError):
                pass
        self._now_playing.end()

    @pyqtSlot()
    def _on_detail_back(self) -> None:
        self._content_stack.setCurrentIndex(0)

    @pyqtSlot(object, str)
    def _on_folder_selected(self, paths, label: str) -> None:
        # Switch back to the list view so the filter is visible, and apply.
        self._content_stack.setCurrentIndex(0)
        self._spreadsheet_list.set_path_filter(paths, label)

    @pyqtSlot()
    def _on_show_playlists(self) -> None:
        """Sidebar picked "All Playlists" — switch content to the playlist view."""
        self._playlist_list.refresh()
        self._content_stack.setCurrentIndex(2)

    @pyqtSlot()
    def _on_show_search(self) -> None:
        """Sidebar picked "Search" — switch content to the search view."""
        self._content_stack.setCurrentIndex(3)

    @pyqtSlot(str, str)
    def _on_list_clipboard_requested(self, path: str, op: str) -> None:
        """Main list emitted Cut/Copy → put path on the tree's clipboard."""
        self._org_tree.set_clipboard_from_list(path, op)

    @pyqtSlot(list, str)
    def _on_list_bulk_clipboard_requested(
        self, paths: list, op: str
    ) -> None:
        """Main list emitted bulk Cut/Copy → put N paths on the tree's clipboard."""
        self._org_tree.set_clipboard_from_list_multi(list(paths), op)

    @pyqtSlot(list)
    def _on_list_bulk_delete_requested(self, paths: list) -> None:
        """Main list emitted bulk Delete → confirm once, strip refs, rm files."""
        paths = [str(p) for p in paths if p]
        if not paths:
            return
        n = len(paths)
        preview = "\n".join(
            f"  • {os.path.relpath(p, OUTPUT_DIR)}" for p in paths[:8]
        )
        more = f"\n  …and {n - 8} more" if n > 8 else ""
        reply = QMessageBox.question(
            self,
            "Delete files",
            f"Delete {n} file{'s' if n != 1 else ''} from disk?\n\n"
            f"{preview}{more}\n\n"
            "This cannot be undone. All folder references will be removed too.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            org = load_org()
            for p in paths:
                self._org_tree._strip_spreadsheet_refs(
                    org.get("root", {}), _norm_path(p)
                )
            save_org(org)
        except Exception as e:
            logger.exception("Bulk delete: refs strip failed")
            QMessageBox.critical(self, "Delete failed", str(e))
            return
        failures: list[tuple[str, str]] = []
        removed = 0
        for p in paths:
            try:
                if os.path.exists(p):
                    os.remove(p)
                removed += 1
            except OSError as e:
                failures.append((os.path.basename(p), str(e)))
        # If anything was on the cut clipboard for these paths, clear it.
        clip = self._org_tree._clipboard
        if clip is not None and clip.get("source") in ("list_file", "list_files"):
            self._org_tree._clear_clipboard()
        self._spreadsheet_list.refresh()
        self._org_tree.refresh()
        self._status.showMessage(
            f"Deleted {removed} file{'s' if removed != 1 else ''}"
            + (f" ({len(failures)} failed)" if failures else ""),
            6000,
        )
        if failures:
            joined = "\n".join(f"  • {n}: {err}" for n, err in failures[:8])
            QMessageBox.warning(
                self, "Some files could not be deleted",
                f"{len(failures)} file(s) failed to delete:\n\n{joined}"
            )

    @pyqtSlot()
    def _on_tree_clipboard_changed(self) -> None:
        """Tree clipboard changed → reflect Cut styling in the main list."""
        clip = self._org_tree._clipboard
        if clip is not None and clip.get("op") == "cut":
            if clip.get("source") == "list_file":
                self._spreadsheet_list.mark_cut_paths(clip.get("path"))
                return
            if clip.get("source") == "list_files":
                self._spreadsheet_list.mark_cut_paths(
                    set(clip.get("paths") or [])
                )
                return
        self._spreadsheet_list.mark_cut_paths(None)

    # --- System tray -------------------------------------------------------
    def _setup_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            logger.info("tray: system tray not available")
            return
        png_path = _resource_path("AmazonMusicManager_Logo2-Square.png")
        ico_path = _ensure_tray_ico(png_path)
        icon: QIcon
        if ico_path:
            icon = QIcon(ico_path)
            logger.info("tray: using ico %s (null=%s)", ico_path, icon.isNull())
        elif os.path.exists(png_path):
            src = QPixmap(png_path)
            icon = QIcon()
            for size in (16, 24, 32, 48, 64, 128, 256):
                icon.addPixmap(src.scaled(
                    size, size,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                ))
            logger.info("tray: ico build failed, using scaled PNG fallback")
        else:
            icon = self.windowIcon()
            logger.warning("tray: logo png missing at %s", png_path)
        self._tray = QSystemTrayIcon(icon, self)
        self._tray.setToolTip("Amazon Music Manager")

        menu = QMenu(self)
        show_action = QAction("Show", self)
        show_action.triggered.connect(self._tray_show_window)
        hide_action = QAction("Hide", self)
        hide_action.triggered.connect(self.hide)
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(QApplication.quit)
        menu.addAction(show_action)
        menu.addAction(hide_action)
        menu.addSeparator()
        menu.addAction(quit_action)
        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._on_tray_activated)
        self._tray.show()

    def _tray_show_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _on_tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            if self.isVisible() and not self.isMinimized():
                self.hide()
            else:
                self._tray_show_window()

    # --- Shutdown ----------------------------------------------------------
    def closeEvent(self, event) -> None:
        settings = QSettings("VYSERIX", "AmazonMusicManager")
        settings.setValue("mainwindow/geometry", self.saveGeometry())
        settings.setValue("mainwindow/splitter", self._splitter.saveState())
        if self._worker is not None:
            self._worker.request_stop()
            if self._thread is not None:
                self._thread.quit()
                self._thread.wait(10_000)
        super().closeEvent(event)


def main() -> int:
    setup_logging(verbose=False)
    # Detach process identity from python.exe so Windows uses our icon for
    # the tray / taskbar / Alt-Tab instead of the host interpreter's.
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "VYSERIX.AmazonMusicManager.GUI.1"
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("AppUserModelID set failed: %s", e)
    app = QApplication(sys.argv)
    app.setApplicationName("Amazon Music Manager")
    app.setOrganizationName("VYSERIX")
    icon_path = _resource_path("AmazonMusicManager_TaskbarIcon2.png")
    if os.path.exists(icon_path):
        ico_path = _ensure_tray_ico(icon_path)
        if ico_path:
            app.setWindowIcon(QIcon(ico_path))
        else:
            src = QPixmap(icon_path)
            multi = QIcon()
            for size in (16, 24, 32, 48, 64, 128, 256):
                multi.addPixmap(src.scaled(
                    size, size,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                ))
            app.setWindowIcon(multi)
    font = app.font()
    font.setPointSizeF(font.pointSizeF() * 1.21)
    app.setFont(font)

    splash: QSplashScreen | None = None
    splash_path = _resource_path("AmazonMusicManager_Logo2-Square.png")
    if os.path.exists(splash_path):
        pixmap = QPixmap(splash_path)
        if not pixmap.isNull():
            pixmap = pixmap.scaled(
                480, 480,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            splash = QSplashScreen(pixmap, Qt.WindowType.WindowStaysOnTopHint)
            splash.show()
            app.processEvents()

    window = MainWindow()

    def _finish_splash() -> None:
        window.show()
        if splash is not None:
            splash.finish(window)

    if splash is not None:
        QTimer.singleShot(3000, _finish_splash)
    else:
        window.show()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
