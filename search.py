import re
from difflib import SequenceMatcher
from dataclasses import dataclass

from playwright.async_api import Page

from config import (
    BASE_URL,
    DEFAULT_TIMEOUT,
    LONG_TIMEOUT,
    AUTO_SELECT_THRESHOLD,
    PROMPT_THRESHOLD,
)
from utils import logger, normalize


@dataclass
class SearchCandidate:
    title: str
    artist: str
    album: str
    element: object  # Playwright ElementHandle — used to click/add the song
    score: int = 0


@dataclass
class SelectionResult:
    status: str  # "added", "skipped", "manual"
    song_title: str
    song_artist: str
    selected: SearchCandidate | None = None


def score_match(desired_title: str, desired_artist: str, candidate: SearchCandidate) -> int:
    """Score a candidate against the desired song (0-100).

    Title is weighted 70 %, artist 30 %.  Extra penalties are applied for
    live/remix/acoustic variants unless the desired title also contains
    those keywords.
    """
    title_ratio = SequenceMatcher(
        None, normalize(desired_title), normalize(candidate.title)
    ).ratio()
    artist_ratio = SequenceMatcher(
        None, normalize(desired_artist), normalize(candidate.artist)
    ).ratio()

    raw = int(title_ratio * 70 + artist_ratio * 30)

    # Penalize unwanted variants
    variant_keywords = ["live", "remix", "acoustic", "demo", "karaoke", "instrumental"]
    desired_lower = desired_title.lower()
    candidate_lower = candidate.title.lower()

    for kw in variant_keywords:
        if kw in candidate_lower and kw not in desired_lower:
            raw -= 15

    return max(0, min(100, raw))


async def search_songs(page: Page, query: str) -> list[SearchCandidate]:
    """Search Amazon Music and return song candidates from the Songs section.

    Uses the search URL directly. Song results are `music-horizontal-item`
    elements inside the `music-shoveler[primary-text="Songs"]` section.
    """
    import urllib.parse
    encoded = urllib.parse.quote(query)
    search_url = f"{BASE_URL}/search/{encoded}"

    logger.debug("Navigating to search: %s", search_url)
    await page.goto(search_url, wait_until="domcontentloaded", timeout=LONG_TIMEOUT)

    # Wait for the Songs shoveler to appear — may take longer after dialog interactions
    shoveler_found = False
    for wait_attempt in range(3):
        try:
            await page.wait_for_selector(
                'music-shoveler[primary-text="Songs"]',
                state="attached",
                timeout=DEFAULT_TIMEOUT,
            )
            shoveler_found = True
            break
        except Exception:
            if wait_attempt < 2:
                logger.debug("No Songs shoveler yet for '%s', retrying (attempt %d)...", query, wait_attempt + 1)
                # Reload the search page in case it didn't load properly
                await page.goto(search_url, wait_until="domcontentloaded", timeout=LONG_TIMEOUT)
            else:
                logger.debug("No Songs shoveler found for '%s' after retries", query)

    # Extra wait for horizontal items to populate inside the shoveler
    await page.wait_for_timeout(3000)

    # Extract song results from the Songs shoveler
    # Song results are music-horizontal-item elements with primary-text and secondary-text
    candidates_data = await page.evaluate("""() => {
        const results = [];

        // Find the "Songs" shoveler by primary-text attribute
        const shovelers = document.querySelectorAll('music-shoveler');
        let songsShoveler = null;

        for (const shov of shovelers) {
            const pt = (shov.getAttribute('primary-text') || '').toLowerCase();
            if (pt === 'songs') {
                songsShoveler = shov;
                break;
            }
        }

        if (!songsShoveler) {
            return { results: [], foundSongsShoveler: false };
        }

        // Get music-horizontal-item elements within the Songs shoveler
        const items = songsShoveler.querySelectorAll('music-horizontal-item');
        for (const item of items) {
            const title = item.getAttribute('primary-text') || '';
            const artist = item.getAttribute('secondary-text') || '';
            if (title) {
                results.push({ title, artist, album: '' });
            }
        }

        return {
            results: results.slice(0, 15),
            foundSongsShoveler: true,
        };
    }""")

    found_shoveler = candidates_data.get("foundSongsShoveler", False)
    results_list = candidates_data.get("results", [])
    logger.info("Search: %d song results (Songs shoveler: %s)", len(results_list), found_shoveler)

    # Get element handles for each result so we can interact with them
    candidates: list[SearchCandidate] = []

    if results_list:
        # Query the actual elements in the Songs shoveler
        item_handles = await page.evaluate_handle("""() => {
            const shovelers = document.querySelectorAll('music-shoveler');
            for (const shov of shovelers) {
                if ((shov.getAttribute('primary-text') || '').toLowerCase() === 'songs') {
                    return Array.from(shov.querySelectorAll('music-horizontal-item[primary-text]'));
                }
            }
            return [];
        }""")

        # Get individual element handles
        count = await item_handles.evaluate("arr => arr.length")
        for i in range(min(count, len(results_list))):
            handle = await item_handles.evaluate_handle("(arr, i) => arr[i]", i)
            data = results_list[i]
            if data.get("title"):
                candidates.append(SearchCandidate(
                    title=data["title"],
                    artist=data["artist"],
                    album=data["album"],
                    element=handle,
                ))

    logger.debug("Search for '%s' returned %d candidates", query, len(candidates))
    return candidates


async def hybrid_select(
    page: Page,
    desired_title: str,
    desired_artist: str,
    auto_threshold: int = AUTO_SELECT_THRESHOLD,
) -> SelectionResult:
    """Search for a song and return a SelectionResult.

    - High confidence  (>= auto_threshold): auto-select best match
    - Medium confidence (>= PROMPT_THRESHOLD): prompt the user
    - Low confidence    (< PROMPT_THRESHOLD) or no results: skip with warning
    """
    # Strip generic/unhelpful artist names from the search query
    generic_artists = {"various artists", "various", "soundtrack", "soundtrack/cast album",
                       "original soundtrack", "ost", "cast"}
    artist_clean = desired_artist.strip()
    if artist_clean.lower() in generic_artists:
        query = desired_title
    else:
        query = f"{desired_title} {desired_artist}"

    candidates = await search_songs(page, query)

    if not candidates:
        logger.warning("No results for '%s'", query)
        print(f"  No results found for \"{desired_title}\" by {desired_artist} — skipping")
        return SelectionResult("skipped", desired_title, desired_artist)

    # Score every candidate
    for c in candidates:
        c.score = score_match(desired_title, desired_artist, c)

    # Sort descending by score
    candidates.sort(key=lambda c: c.score, reverse=True)
    best = candidates[0]

    # --- High confidence ---
    if best.score >= auto_threshold:
        logger.info("Auto-selected '%s' by %s (score %d)", best.title, best.artist, best.score)
        print(f"  Auto-selected: {best.title} -- {best.artist} ({best.album}) [score {best.score}]")
        return SelectionResult("added", desired_title, desired_artist, selected=best)

    # --- Medium confidence — prompt user ---
    if best.score >= PROMPT_THRESHOLD:
        return await _prompt_user(candidates, desired_title, desired_artist)

    # --- Low confidence ---
    logger.warning("Best match score too low (%d) for '%s'", best.score, query)
    print(f"  No good match for \"{desired_title}\" by {desired_artist} (best score: {best.score})")
    return await _prompt_user(candidates, desired_title, desired_artist)


async def _prompt_user(
    candidates: list[SearchCandidate],
    desired_title: str,
    desired_artist: str,
) -> SelectionResult:
    """Display numbered choices and ask the user to pick."""
    top = candidates[:8]

    print(f"\n  Found multiple matches for \"{desired_title}\" by {desired_artist}:\n")
    for i, c in enumerate(top, 1):
        print(f"    [{i}] {c.title} -- {c.artist} ({c.album}) [score {c.score}]")
    print(f"\n    [s] Skip this song")
    print()

    while True:
        choice = input("  Select [1-{}/s]: ".format(len(top))).strip().lower()

        if choice == "s":
            return SelectionResult("skipped", desired_title, desired_artist)

        if choice.isdigit() and 1 <= int(choice) <= len(top):
            picked = top[int(choice) - 1]
            logger.info("User selected '%s' by %s", picked.title, picked.artist)
            return SelectionResult("added", desired_title, desired_artist, selected=picked)

        print("  Invalid choice — try again.")
