import os
import subprocess
import socket
import time
from playwright.async_api import async_playwright, Browser, BrowserContext, Page

from config import USER_DATA_DIR, SLOW_MO
from utils import logger


def _find_free_port() -> int:
    """Find a free port for Chrome's debug protocol."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        return s.getsockname()[1]


def _find_chrome_path() -> str:
    """Find the installed Chrome executable path."""
    common_paths = [
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
    ]
    for p in common_paths:
        if os.path.exists(p):
            return p
    return "chrome"  # hope it's on PATH


def minimize_process_windows(pid: int, retries: int = 20, delay: float = 0.1) -> int:
    """Minimize all visible top-level windows owned by *pid* (Windows only).

    Chrome's window takes a short moment after launch before EnumWindows can
    see it — we retry a few times until at least one window is found. The
    alternative (CDP `Browser.getWindowForTarget`) intermittently fails with
    "Browser window not found" even with an explicit targetId, so we go
    straight to Win32 instead.
    """
    if os.name != "nt":
        return 0
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    SW_MINIMIZE = 6
    enum_proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    for _ in range(retries):
        result = [0]

        def _enum(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            window_pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
            if window_pid.value == pid:
                user32.ShowWindow(hwnd, SW_MINIMIZE)
                result[0] += 1
            return True

        user32.EnumWindows(enum_proc_type(_enum), 0)
        if result[0] > 0:
            return result[0]
        time.sleep(delay)
    return 0


def _kill_chrome_owning_user_data_dir(user_data_dir: str) -> int:
    """Kill chrome.exe processes whose command line contains *user_data_dir*.

    Returns the number of processes terminated (0 if none found or on error).
    The user-data-dir path is passed via an env var so we don't have to
    escape it inside the PowerShell command.
    """
    if os.name != "nt":
        return 0
    script = (
        "$needle = [Environment]::GetEnvironmentVariable('AMZ_USER_DATA_DIR');"
        "if (-not $needle) { Write-Output 0; exit 0 };"
        "$killed = 0;"
        "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
        "ForEach-Object { "
        "  if ($_.CommandLine -and $_.CommandLine.ToLower().Contains($needle.ToLower())) { "
        "    try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop; $killed++ } catch {} "
        "  } "
        "};"
        "Write-Output $killed"
    )
    env = os.environ.copy()
    env["AMZ_USER_DATA_DIR"] = user_data_dir
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-Command", script],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        out = (result.stdout or "").strip().splitlines()
        killed = int(out[-1]) if out and out[-1].strip().isdigit() else 0
        return killed
    except (subprocess.SubprocessError, ValueError, OSError) as e:
        logger.warning("Could not scan Chrome processes: %s", e)
        return 0


class BrowserManager:
    """Manages a Chrome browser instance for automation.

    Launches the real Chrome with remote debugging enabled, then connects
    Playwright to it. This avoids Playwright's bundled Chromium which
    Amazon Music detects as outdated/automated.
    """

    def __init__(self, headless: bool = False, slow_mo: int = SLOW_MO):
        self.headless = headless
        self.slow_mo = slow_mo
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._chrome_process = None
        self.page: Page | None = None

    async def __aenter__(self):
        os.makedirs(USER_DATA_DIR, exist_ok=True)
        os.makedirs("screenshots", exist_ok=True)

        chrome_path = _find_chrome_path()
        debug_port = await self._launch_chrome_with_retry(chrome_path)

        # Connect Playwright to the running Chrome
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.connect_over_cdp(
            f"http://127.0.0.1:{debug_port}",
            slow_mo=self.slow_mo,
        )

        # Get or create a context and page
        contexts = self._browser.contexts
        if contexts:
            self._context = contexts[0]
            self.page = self._context.pages[0] if self._context.pages else await self._context.new_page()
        else:
            self._context = await self._browser.new_context(
                viewport={"width": 1400, "height": 900},
            )
            self.page = await self._context.new_page()

        logger.info("Browser launched (headless=%s, real Chrome via CDP)", self.headless)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        # Terminate Chrome process
        if self._chrome_process:
            try:
                self._chrome_process.terminate()
                self._chrome_process.wait(timeout=5)
            except Exception:
                try:
                    self._chrome_process.kill()
                except Exception:
                    pass
        logger.info("Browser closed")

    async def new_page(self) -> Page:
        return await self._context.new_page()

    async def _launch_chrome_with_retry(self, chrome_path: str, attempts: int = 3) -> int:
        """Launch Chrome and wait for its CDP port. Retry if the process
        delegates to an existing instance (new chrome.exe exits almost
        immediately when another Chrome already owns --user-data-dir, which
        leaves no debug port open).
        """
        user_data_dir = os.path.abspath(USER_DATA_DIR)
        delegation_detected = False

        for attempt in range(1, attempts + 1):
            # Stale SingletonLock from a previous crash prevents Chrome from
            # owning the profile — the new exe quietly delegates and exits.
            lock = os.path.join(user_data_dir, "SingletonLock")
            if os.path.exists(lock):
                try:
                    os.remove(lock)
                    logger.info("Removed stale SingletonLock from user data dir")
                except OSError as e:
                    logger.warning("Could not remove SingletonLock: %s", e)

            # If we've already seen delegation on a prior attempt, actively
            # kill any Chrome process holding this profile before relaunching.
            if delegation_detected:
                killed = _kill_chrome_owning_user_data_dir(user_data_dir)
                if killed:
                    logger.info("Terminated %d existing chrome.exe process(es) "
                                "owning this user-data-dir", killed)
                    time.sleep(2)  # let the OS release the profile lock
                else:
                    logger.warning("No chrome.exe process found matching the "
                                   "user-data-dir — delegation may be from a "
                                   "different mechanism")

            debug_port = _find_free_port()
            chrome_args = [
                chrome_path,
                f"--remote-debugging-port={debug_port}",
                f"--user-data-dir={user_data_dir}",
                "--no-first-run",
                "--no-default-browser-check",
                "--window-size=1400,900",
            ]
            if self.headless:
                chrome_args.append("--headless=new")

            logger.info("Launching Chrome (attempt %d/%d) on debug port %d",
                        attempt, attempts, debug_port)
            self._chrome_process = subprocess.Popen(
                chrome_args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            port_ready = False
            this_attempt_delegated = False
            for _ in range(30):  # up to ~15s
                # If the subprocess exited quickly without opening the port,
                # it almost certainly delegated to an existing Chrome instance.
                if self._chrome_process.poll() is not None:
                    logger.warning("Chrome process exited early (code %s) — "
                                   "likely delegated to an existing Chrome "
                                   "owning this user-data-dir",
                                   self._chrome_process.returncode)
                    this_attempt_delegated = True
                    break
                try:
                    with socket.create_connection(("127.0.0.1", debug_port), timeout=1):
                        port_ready = True
                        break
                except (ConnectionRefusedError, OSError):
                    time.sleep(0.5)

            if port_ready:
                return debug_port

            if this_attempt_delegated:
                delegation_detected = True

            # Clean up before retrying
            try:
                if self._chrome_process and self._chrome_process.poll() is None:
                    self._chrome_process.terminate()
                    self._chrome_process.wait(timeout=3)
            except Exception:
                pass
            self._chrome_process = None

            if attempt < attempts:
                logger.info("Retrying Chrome launch in 2s...")
                time.sleep(2)

        raise RuntimeError(
            "Chrome failed to start with remote debugging after "
            f"{attempts} attempts. If another Chrome window is still open "
            "using this profile, close it manually and try again."
        )
