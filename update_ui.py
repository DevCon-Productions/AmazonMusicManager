"""Qt glue for the self-updater: background check, prompt, download, install.

Usage from the main window:
    self._updates = UpdateController(self)
    self._updates.check(manual=False)   # quiet startup check
    self._updates.check(manual=True)    # Help -> Check for Updates…
"""
from __future__ import annotations

import logging
import threading

from PyQt6.QtCore import QObject, QRunnable, Qt, QThreadPool, pyqtSignal
from PyQt6.QtWidgets import QApplication, QMessageBox, QProgressDialog, QWidget

import updater
from version import APP_VERSION

logger = logging.getLogger("amazonmusic")


class _Run(QRunnable):
    def __init__(self, fn) -> None:
        super().__init__()
        self._fn = fn

    def run(self) -> None:
        self._fn()


class UpdateController(QObject):
    _checked = pyqtSignal(object, bool, str)   # info|None, manual, error
    _progress = pyqtSignal(int, int)
    _downloaded = pyqtSignal(str)
    _download_failed = pyqtSignal(str)

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._parent = parent
        self._busy = False
        self._cancel = threading.Event()
        self._dlg: QProgressDialog | None = None
        self._checked.connect(self._on_checked)
        self._progress.connect(self._on_progress)
        self._downloaded.connect(self._on_downloaded)
        self._download_failed.connect(self._on_download_failed)

    # --- check -------------------------------------------------------------
    def check(self, manual: bool) -> None:
        if self._busy:
            return
        self._busy = True

        def work() -> None:
            try:
                info = updater.check_for_update()
                self._checked.emit(info, manual, "")
            except Exception as e:  # network down, rate-limited, etc.
                self._checked.emit(None, manual, str(e))

        QThreadPool.globalInstance().start(_Run(work))

    def _on_checked(self, info, manual: bool, error: str) -> None:
        self._busy = False
        if error:
            logger.info("Update check failed: %s", error)
            if manual:
                QMessageBox.warning(
                    self._parent, "Check for Updates",
                    f"Couldn't reach the update server.\n\n{error}",
                )
            return
        if info is None:
            logger.info("Update check: up to date (v%s)", APP_VERSION)
            if manual:
                QMessageBox.information(
                    self._parent, "Check for Updates",
                    f"You're running the latest version (v{APP_VERSION}).",
                )
            return
        logger.info("Update available: v%s (running v%s)", info.version, APP_VERSION)
        self._prompt(info)

    # --- prompt ------------------------------------------------------------
    def _prompt(self, info: updater.UpdateInfo) -> None:
        notes = info.notes.strip()
        if len(notes) > 1200:
            notes = notes[:1200].rstrip() + "…"
        box = QMessageBox(self._parent)
        box.setWindowTitle("Update Available")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText(
            f"<b>Amazon Music Manager v{info.version}</b> is available "
            f"(you have v{APP_VERSION})."
        )
        if updater.can_self_update():
            box.setInformativeText(
                "Download and install it now? The app will close, update, "
                "and reopen. Windows will ask for permission to install."
            )
            install = box.addButton("Download && Install", QMessageBox.ButtonRole.AcceptRole)
        else:
            box.setInformativeText(
                "You're running from source, so automatic install is disabled. "
                "Pull the latest code or download the installer from GitHub."
            )
            install = None
        box.addButton("Later", QMessageBox.ButtonRole.RejectRole)
        if notes:
            box.setDetailedText(notes)
        box.exec()
        if install is not None and box.clickedButton() is install:
            self._start_download(info)

    # --- download ----------------------------------------------------------
    def _start_download(self, info: updater.UpdateInfo) -> None:
        self._cancel.clear()
        dlg = QProgressDialog(
            f"Downloading v{info.version}…", "Cancel", 0, 100, self._parent
        )
        dlg.setWindowTitle("Updating Amazon Music Manager")
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        dlg.setMinimumDuration(0)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        dlg.canceled.connect(self._cancel.set)
        dlg.show()
        self._dlg = dlg

        def work() -> None:
            try:
                path = updater.download_installer(
                    info,
                    progress=lambda d, t: self._progress.emit(d, t),
                    cancelled=self._cancel.is_set,
                )
                self._downloaded.emit(path)
            except Exception as e:
                self._download_failed.emit(str(e))

        QThreadPool.globalInstance().start(_Run(work))

    def _on_progress(self, done: int, total: int) -> None:
        if self._dlg is None:
            return
        if total > 0:
            self._dlg.setMaximum(100)
            self._dlg.setValue(int(done * 100 / total))
            self._dlg.setLabelText(
                f"Downloading… {done / 1e6:.1f} of {total / 1e6:.1f} MB"
            )
        else:
            self._dlg.setMaximum(0)  # indeterminate

    def _close_dlg(self) -> None:
        if self._dlg is not None:
            self._dlg.close()
            self._dlg = None

    def _on_download_failed(self, error: str) -> None:
        cancelled = self._cancel.is_set()
        self._close_dlg()
        logger.warning("Update download failed: %s", error)
        if not cancelled:
            QMessageBox.warning(
                self._parent, "Update Failed",
                f"The update couldn't be downloaded.\n\n{error}",
            )

    def _on_downloaded(self, path: str) -> None:
        self._close_dlg()
        logger.info("Update downloaded to %s; launching installer", path)
        try:
            updater.launch_installer(path)
        except OSError as e:
            # Most commonly: user declined the UAC prompt.
            logger.warning("Installer launch failed: %s", e)
            QMessageBox.warning(
                self._parent, "Update Failed",
                f"The installer couldn't be started.\n\n{e}",
            )
            return
        # Close cleanly (saves settings, shuts down Chrome) so the installer
        # can replace our files, then quit the event loop.
        self._parent.close()
        QApplication.quit()
