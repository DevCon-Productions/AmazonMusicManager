import asyncio
import logging
import os
import re

from config import LOG_FILE, SCREENSHOT_DIR


def setup_logging(verbose: bool = False) -> logging.Logger:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(),
            # Write under the per-user data dir, not the (possibly read-only)
            # working/install directory.
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
        ],
    )
    return logging.getLogger("amazonmusic")


logger = logging.getLogger("amazonmusic")


def normalize(text: str) -> str:
    """Normalize a string for fuzzy comparison.

    Lowercases, strips parenthetical suffixes like '(Remastered)',
    removes punctuation, and collapses whitespace.
    """
    s = text.lower().strip()
    s = re.sub(r"\(.*?\)", "", s)
    s = re.sub(r"\[.*?\]", "", s)
    s = re.sub(r"[^\w\s]", "", s)
    return " ".join(s.split())


async def retry(coro_fn, retries: int = 3, delay: float = 2.0):
    """Retry an async callable up to *retries* times with a fixed delay."""
    last_exc = None
    for attempt in range(retries):
        try:
            return await coro_fn()
        except Exception as exc:
            last_exc = exc
            if attempt < retries - 1:
                logger.warning("Attempt %d failed (%s), retrying in %.1fs...", attempt + 1, exc, delay)
                await asyncio.sleep(delay)
    raise last_exc


async def screenshot_on_error(page, name: str = "error"):
    """Save a debug screenshot."""
    try:
        path = os.path.join(SCREENSHOT_DIR, f"{name}.png")
        await page.screenshot(path=path, full_page=True)
        logger.info("Screenshot saved to %s", path)
    except Exception:
        pass
