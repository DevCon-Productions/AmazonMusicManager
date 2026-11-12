import asyncio
from playwright.async_api import Page

from config import BASE_URL, LONG_TIMEOUT
from utils import logger


async def ensure_logged_in(page: Page) -> bool:
    """Navigate to Amazon Music and ensure the user is logged in.

    Checks for signs of a logged-in session. If detection is inconclusive,
    asks the user to confirm via the terminal.
    """
    logger.info("Navigating to %s", BASE_URL)
    await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=LONG_TIMEOUT)
    await page.wait_for_timeout(3000)

    # Check whether we landed on a page that looks logged-in
    if await _detect_logged_in(page):
        logger.info("Already logged in (auto-detected)")
        await _suppress_dialogs(page)
        return True

    # Could not auto-detect — ask the user
    print("\n" + "=" * 60)
    print("  Please log in to Amazon Music in the browser window.")
    print("  After logging in, come back here and press Enter.")
    print("=" * 60 + "\n")

    await asyncio.to_thread(input, "  Press Enter once you are logged in... ")
    await page.wait_for_timeout(3000)
    await _suppress_dialogs(page)

    logger.info("User confirmed login — continuing")
    return True


async def _detect_logged_in(page: Page, timeout_ms: int = 10_000) -> bool:
    """Use JavaScript to check for signs of a logged-in Amazon Music session.

    Polls for up to ``timeout_ms`` because the SPA renders its content well
    after domcontentloaded; returns as soon as a logged-in marker appears.
    """
    waited = 0
    while True:
        logged_in = await _detect_logged_in_once(page)
        if logged_in or waited >= timeout_ms:
            break
        await page.wait_for_timeout(500)
        waited += 500
    if not logged_in:
        logger.info("Login not detected (url=%s)", page.url)
    return logged_in


async def _detect_logged_in_once(page: Page) -> bool:
    logged_in = await page.evaluate("""() => {
        // Check if the URL suggests we're past the login page
        const url = window.location.href;
        if (url.includes('/ap/signin') || url.includes('/ap/register')) return false;

        // Look for elements that only show for logged-in users.
        // Nov 2026 redesign dropped all music-* web components; the home
        // page now links the user's own playlists as /user-playlists/<uuid>.
        const indicators = [
            'a[href*="/user-playlists/"]',
            'a[href*="/my/"]',
            'music-detail-header',
            '[class*="profile" i]',
            'music-button[aria-label*="Account"]',
            'music-button[aria-label*="Settings"]',
        ];
        for (const sel of indicators) {
            if (document.querySelector(sel)) return true;
        }

        // Check if there are any music-image-row elements (content = logged in)
        if (document.querySelectorAll('music-image-row').length > 0) return true;

        // Personalised shelf headings (redesigned site)
        for (const h of document.querySelectorAll('h1, h2, h3')) {
            if (/^(your frequent plays|recently played|your library)/i.test(
                    (h.textContent || '').trim())) return true;
        }

        return false;
    }""")
    if logged_in:
        logger.debug("Login detected via JavaScript checks")
    return logged_in


async def _suppress_dialogs(page: Page):
    """Permanently suppress the 'Update Chrome' dialog and any similar modals.

    Waits for the dialog to appear, then removes it and sets up a
    MutationObserver to kill it if it comes back.
    """
    # Wait for the dialog to actually appear
    try:
        await page.wait_for_selector('#dialog[role="dialog"]', timeout=8000)
    except Exception:
        logger.debug("No update dialog appeared")
        return

    # Use page.evaluate to nuke it and set up persistent suppression
    await page.evaluate("""() => {
        function killDialogs() {
            // Find and remove all modal dialogs
            document.querySelectorAll('#dialog[role="dialog"], [aria-modal="true"]').forEach(d => {
                // Walk up to the highest fixed/absolute container
                let target = d;
                while (target.parentElement && target.parentElement !== document.body) {
                    const pos = window.getComputedStyle(target.parentElement).position;
                    if (pos === 'fixed' || pos === 'absolute') {
                        target = target.parentElement;
                    } else {
                        break;
                    }
                }
                target.remove();
            });
            // Remove any overlay/backdrop
            document.querySelectorAll('div').forEach(el => {
                const s = window.getComputedStyle(el);
                if (s.position === 'fixed' && s.zIndex >= 100 &&
                    (s.backgroundColor.includes('0,') || s.opacity < 1)) {
                    el.remove();
                }
            });
            document.body.style.overflow = '';
            document.documentElement.style.overflow = '';
        }

        killDialogs();

        // Watch for it coming back
        new MutationObserver(() => {
            if (document.querySelector('#dialog[role="dialog"]')) {
                setTimeout(killDialogs, 100);
            }
        }).observe(document.body, { childList: true, subtree: true });
    }""")

    await page.wait_for_timeout(500)

    # Verify it's gone
    still_there = await page.evaluate("() => !!document.querySelector('#dialog[role=\"dialog\"]')")
    if still_there:
        logger.warning("Dialog still present after removal attempt — forcing via CSS")
        await page.add_style_tag(content="""
            #dialog[role="dialog"] { display: none !important; }
            [aria-modal="true"] { display: none !important; }
        """)
    else:
        logger.debug("Update dialog dismissed successfully")
