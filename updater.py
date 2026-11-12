"""Self-update via GitHub Releases.

Flow: ``check_for_update()`` asks the GitHub API for the latest release,
compares its tag to APP_VERSION and returns an ``UpdateInfo`` when newer.
``download_installer()`` fetches the Setup .exe to a temp folder (verifying
its SHA-256 when GitHub publishes a digest), and ``launch_installer()``
starts it silently. The caller then quits the app; the Inno Setup installer
replaces the files and relaunches the app when done.

Pure stdlib (urllib) so it adds no dependencies. All functions are blocking
and meant to run off the GUI thread.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass
from typing import Callable

from version import APP_VERSION

REPO = "DevCon-Productions/AmazonMusicManager"
LATEST_RELEASE_API = f"https://api.github.com/repos/{REPO}/releases/latest"
USER_AGENT = f"AmazonMusicManager/{APP_VERSION}"
_TIMEOUT = 15  # seconds


@dataclass
class UpdateInfo:
    version: str          # e.g. "1.2"
    notes: str            # release body (markdown)
    page_url: str         # release page on GitHub
    asset_name: str
    asset_url: str        # direct download URL of the Setup .exe
    asset_size: int
    sha256: str | None    # hex digest if GitHub provided one


def parse_version(v: str) -> tuple[int, ...]:
    """'v1.10.2' -> (1, 10, 2). Non-numeric suffixes are ignored."""
    nums = re.findall(r"\d+", v.split("-")[0])
    return tuple(int(n) for n in nums) or (0,)


def is_newer(remote: str, local: str = APP_VERSION) -> bool:
    r, l = parse_version(remote), parse_version(local)
    width = max(len(r), len(l))
    return r + (0,) * (width - len(r)) > l + (0,) * (width - len(l))


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github+json",
    })
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return json.load(resp)


def check_for_update() -> UpdateInfo | None:
    """Return UpdateInfo if a newer release with a Setup .exe exists."""
    rel = _get_json(LATEST_RELEASE_API)
    if rel.get("draft") or rel.get("prerelease"):
        return None
    tag = rel.get("tag_name") or ""
    if not is_newer(tag):
        return None
    for a in rel.get("assets", []):
        name = a.get("name", "")
        if name.lower().endswith(".exe") and "setup" in name.lower():
            digest = a.get("digest") or ""
            sha = digest.split(":", 1)[1] if digest.startswith("sha256:") else None
            return UpdateInfo(
                version=tag.lstrip("vV"),
                notes=rel.get("body") or "",
                page_url=rel.get("html_url") or "",
                asset_name=name,
                asset_url=a["browser_download_url"],
                asset_size=int(a.get("size") or 0),
                sha256=sha,
            )
    return None


def download_installer(
    info: UpdateInfo,
    progress: Callable[[int, int], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> str:
    """Download the installer; return its local path. Raises on failure."""
    folder = os.path.join(tempfile.gettempdir(), "AmazonMusicManager-update")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, os.path.basename(info.asset_name))
    req = urllib.request.Request(info.asset_url, headers={"User-Agent": USER_AGENT})
    h = hashlib.sha256()
    done = 0
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp, open(path + ".part", "wb") as f:
        total = int(resp.headers.get("Content-Length") or info.asset_size or 0)
        while True:
            if cancelled and cancelled():
                raise RuntimeError("Download cancelled")
            chunk = resp.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
            h.update(chunk)
            done += len(chunk)
            if progress:
                progress(done, total)
    if total and done != total:
        raise RuntimeError(f"Incomplete download ({done} of {total} bytes)")
    if info.sha256 and h.hexdigest().lower() != info.sha256.lower():
        os.remove(path + ".part")
        raise RuntimeError("Downloaded installer failed its checksum check")
    os.replace(path + ".part", path)
    return path


def launch_installer(path: str) -> None:
    """Start the installer silently and detached from this process.

    /SILENT shows only a progress window; /CLOSEAPPLICATIONS lets Inno close
    us if we're still running; the installer's [Run] section relaunches the
    app after a silent install.
    """
    args = "/SILENT /SP- /NORESTART /CLOSEAPPLICATIONS"
    if sys.platform == "win32":
        # ShellExecute (not CreateProcess) so Windows shows the UAC prompt
        # the admin-level installer needs instead of failing with error 740.
        os.startfile(path, "open", args)
    else:  # pragma: no cover - Windows-only app
        subprocess.Popen([path, *args.split()], close_fds=True)


def can_self_update() -> bool:
    """Only the installed (frozen) build can update itself."""
    return bool(getattr(sys, "frozen", False))
