# Amazon Music Manager

A Windows desktop app for power-managing your Amazon Music library. It exports
playlists to spreadsheets, organizes them into virtual folders, creates and
edits playlists on Amazon, plays music, and includes a **Classical Shuffle**
engine that plays (or pre-builds) playlists of complete multi-movement works.

It works by driving a **real Chrome browser** over the DevTools Protocol
(Playwright connects to your installed Chrome — not a bundled/automated
browser, which Amazon flags).

> Built by **DevCon Productions**, Cleveland, Ohio. Version 1.0.

---

## Features

- **Export / update** Amazon playlists to styled `.xlsx`, syncing only new songs on re-update.
- **Spreadsheet tools** — live filter, merge, compare, duplicate detection, curate, MusicBrainz metadata.
- **Create playlists** on Amazon from a spreadsheet or CSV (fuzzy match + prompt).
- **Reconcile & reorder** — keep a spreadsheet and its Amazon playlist in sync, in either direction.
- **Organize** playlists/spreadsheets into virtual folders (multi-file, drag-and-drop, bulk actions).
- **Playback** — play a playlist (straight/shuffle) or queue a whole folder with auto-advance.
- **Classical Shuffle** — plays random complete *performances* (whole works by one recording), with composer/piece-type filters and ⏪/⏩ retrace.
- **Classical Shuffle playlist generator** — pre-builds a real Amazon playlist to a duration target, so you can listen on your phone.
- **Web-restricted track** detection and review.
- **Database backup** — export/import your whole library as a `.zip`.

A full in-app guide lives under **Help → User Guide** (F1).

---

## Requirements

- **Windows 10/11** (uses Win32 + Windows audio APIs).
- **Python 3.11+** (to run from source) — not needed if you use the installer.
- **Google Chrome** installed and signed in to Amazon Music.

---

## Run from source

```powershell
pip install -r requirements.txt
python gui.py
```

Then click **Connect Chrome** (Ctrl+Shift+C) and sign in to Amazon Music when
the browser opens.

---

## Build the Windows installer

Produces a `Setup.exe` (Start-menu shortcut, uninstaller) via PyInstaller +
[Inno Setup 6](https://jrsoftware.org/isdl.php).

```powershell
pip install -r requirements.txt
pip install pyinstaller
# Install Inno Setup 6 from https://jrsoftware.org/isdl.php
powershell -ExecutionPolicy Bypass -File build_installer.ps1
```

The installer is written to `installer_output\AmazonMusicManager-Setup-1.0.exe`.

Build pieces, if you want to run them by hand:
- `AmazonMusicManager.spec` — PyInstaller spec (bundles assets, collects Playwright/pycaw).
- `installer.iss` — Inno Setup script.
- `build_installer.ps1` — runs both steps.

---

## Notes & tips

- **Keep Chrome connected during long runs.** System sleep drops the browser
  connection; the app blocks idle-sleep while connected (toggle under
  **File → Block idle sleep while connected**). The screen may still turn off.
- **MusicBrainz metadata needs your VPN off** — it blocks many VPN exit nodes.
- **Re-export after editing on Amazon.** The app reads cached spreadsheets, so
  re-export (or Update) a playlist after changing it on Amazon.
- **Your data stays local.** Your library (spreadsheets + folder organization)
  lives under `output/` — `./output` when running from source, or
  `%LOCALAPPDATA%\AmazonMusicManager\output` in an installed build. The Chrome
  login session lives in `browser_data/`. Move your library between machines
  with **File → Export/Import Database** (a single `.zip` that preserves the
  folder tree). Nothing leaves your machine.

---

## Project layout

| File | Purpose |
|------|---------|
| `gui.py` | The desktop app (PyQt6) — main entry point |
| `scraper.py` / `importer.py` | Amazon Music automation (read / write) |
| `organization.py` | Virtual-folder tree (`output/organization.json`) |
| `metadata.py` | MusicBrainz enrichment |
| `web_restricted.py` | Web-restricted track log |
| `menu.py` | Legacy interactive CLI |
| `test_*.py` | Unit tests (grouper, sampler, matchers, …) |

---

© 2026 DevCon Productions. Vibe coded with Claude Code.
