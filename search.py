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
    element: object = None  # unused since the Nov 2026 redesign (adds go by ASIN)
    score: int = 0
    asin: str = ""        # track ASIN
    album_asin: str = ""


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

    Since the Nov 2026 redesign each result's React props carry a
    ``trackRef`` (track ASIN, title, album {id, title}, artists), which we
    read directly; songs are later added by ASIN.
    """
    import urllib.parse
    search_url = f"{BASE_URL}/search/{urllib.parse.quote(query)}"
    logger.debug("Navigating to search: %s", search_url)

    data: list[dict] = []
    for attempt in range(3):
        await page.goto(search_url, wait_until="domcontentloaded", timeout=LONG_TIMEOUT)
        try:
            await page.wait_for_selector(
                '[data-testid="SearchResultsWidget_Track"], [data-testid*="SearchResultsWidget_TopResults"]',
                state="attached", timeout=DEFAULT_TIMEOUT,
            )
        except Exception:
            logger.debug("No search results yet for '%s' (attempt %d)", query, attempt + 1)
            continue
        await page.wait_for_timeout(1500)
        data = await page.evaluate(_SEARCH_TRACKS_JS)
        if data:
            break

    candidates = [
        SearchCandidate(
            title=d.get("title", ""),
            artist=_join_names(d.get("artists") or []),
            album=d.get("album", ""),
            asin=d.get("asin", ""),
            album_asin=d.get("album_asin", ""),
        )
        for d in data[:15] if d.get("title") and d.get("asin")
    ]
    logger.info("Search: %d song results for '%s'", len(candidates), query)
    return candidates


def _join_names(names: list[str]) -> str:
    if len(names) <= 1:
        return names[0] if names else ""
    return ", ".join(names[:-1]) + " & " + names[-1]


# Collect Track objects from React props, Songs section first (that's what
# the old shoveler-based search used), then any other track results.
_SEARCH_TRACKS_JS = r"""() => {
    const out = [], seen = new Set();
    const collect = (root) => {
        for (const e of root.querySelectorAll('[data-testid]')) {
            const fk = Object.keys(e).find(k => k.startsWith('__reactFiber'));
            let f = fk ? e[fk] : null;
            for (let i = 0; i < 8 && f; i++, f = f.return) {
                const p = f.memoizedProps;
                const t = p && (p.trackRef || p.track);
                if (t && t.__typename === 'Track' && t.id) {
                    if (!seen.has(t.id)) {
                        seen.add(t.id);
                        out.push({
                            asin: t.id,
                            title: t.title || t.shortTitle || '',
                            album: (t.album && (t.album.title || t.album.shortTitle)) || '',
                            album_asin: (t.album && t.album.id) || '',
                            artists: ((t.contributingArtists || {}).edges || [])
                                .map(a => a && a.node && a.node.name).filter(Boolean),
                        });
                    }
                    break;
                }
            }
        }
    };
    const songs = document.querySelector('[data-testid="SearchResultsWidget_Track"]');
    if (songs) collect(songs);
    collect(document);
    return out;
}"""


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
