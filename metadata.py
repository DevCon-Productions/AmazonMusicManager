"""Fetch release date and genre/tags from the MusicBrainz API."""

import re
import time
import urllib.parse
import json
from http.client import HTTPSConnection

from models import Song
from utils import logger

# MusicBrainz requires a descriptive User-Agent
_UA = "AmazonMusicExporter/1.0 (playlist-export-tool)"
_HOST = "musicbrainz.org"

# Rate limit: MusicBrainz allows ~1 request per second for unauthenticated users
_REQUEST_DELAY = 1.1  # seconds between requests

# Patterns to strip from titles before searching
_TITLE_NOISE = re.compile(
    r'\s*[\(\[]'
    r'(?:Remaster(?:ed)?|Deluxe|Explicit|Clean|Bonus Track|'
    r'Anniversary|Edition|Version|Original|Extended|Radio Edit|'
    r'Single|Album|Live|Acoustic|Demo|Mono|Stereo|'
    r'Juke\s*Box|7"|12"|LP|EP|'
    r'\d+\s*Remaster(?:ed)?)'
    r'[^\)\]]*[\)\]]',
    re.IGNORECASE,
)
_FEAT_PATTERN = re.compile(
    r'\s*[\(\[](feat\.?|ft\.?|featuring|with)\s+[^\)\]]+[\)\]]',
    re.IGNORECASE,
)
_TRAILING_NOISE = re.compile(
    r'\s*[-–—]\s*(Remaster(?:ed)?|Deluxe|Explicit|Bonus Track|'
    r'\d+\s*(?:Remaster(?:ed)?|Mix)).*$',
    re.IGNORECASE,
)


def _clean_title(title: str) -> str:
    """Strip noise like (Remastered), [Explicit], (feat. X) from a title."""
    cleaned = _FEAT_PATTERN.sub('', title)
    cleaned = _TITLE_NOISE.sub('', cleaned)
    cleaned = _TRAILING_NOISE.sub('', cleaned)
    return cleaned.strip()


def _clean_artist(artist: str) -> str:
    """Simplify artist name — take the primary artist before feat/&/,."""
    # "Artist1 feat. Artist2" -> "Artist1"
    for sep in [' feat.', ' feat ', ' ft.', ' ft ', ' featuring ', ' Feat.', ' Feat ']:
        if sep in artist:
            artist = artist[:artist.index(sep)]
            break
    # "Artist1 & Artist2" or "Artist1, Artist2" -> "Artist1"
    for sep in [' & ', ', ', ' and ', ' And ']:
        if sep in artist:
            artist = artist[:artist.index(sep)]
            break
    return artist.strip()


def _mb_get(path: str) -> dict | None:
    """Synchronous HTTPS GET to MusicBrainz API. Returns parsed JSON or None."""
    try:
        conn = HTTPSConnection(_HOST, timeout=10)
        conn.request("GET", path, headers={
            "User-Agent": _UA,
            "Accept": "application/json",
        })
        resp = conn.getresponse()
        if resp.status == 200:
            return json.loads(resp.read())
        if resp.status == 503:
            logger.debug("MusicBrainz rate limited, backing off")
            time.sleep(3)
            return None
        logger.debug("MusicBrainz returned %d for %s", resp.status, path)
        return None
    except Exception as e:
        logger.debug("MusicBrainz request failed: %s", e)
        return None
    finally:
        conn.close()


def _search_recording(title: str, artist: str, album: str = "") -> dict | None:
    """Search MusicBrainz for a recording matching title + artist.

    Tries multiple strategies:
    1. Exact title + artist
    2. Cleaned title + artist (strip noise like Remastered, feat., etc.)
    3. Cleaned title + cleaned artist (primary artist only)
    4. Cleaned title + album hint
    5. Title-only as last resort
    """
    strategies = []

    clean_t = _clean_title(title)
    clean_a = _clean_artist(artist)

    # Strategy 1: exact
    strategies.append(f'recording:"{title}" AND artist:"{artist}"')

    # Strategy 2: cleaned title + original artist (only if title changed)
    if clean_t != title:
        strategies.append(f'recording:"{clean_t}" AND artist:"{artist}"')

    # Strategy 3: cleaned title + cleaned artist (only if artist changed)
    if clean_a != artist:
        strategies.append(f'recording:"{clean_t}" AND artist:"{clean_a}"')

    # Strategy 4: cleaned title + album (if we have album info)
    if album:
        clean_album = _TITLE_NOISE.sub('', album).strip()
        clean_album = _TRAILING_NOISE.sub('', clean_album).strip()
        if clean_album:
            strategies.append(f'recording:"{clean_t}" AND release:"{clean_album}"')

    # Strategy 5: just the title + loose artist (unquoted)
    strategies.append(f'recording:"{clean_t}" AND artist:{clean_a}')

    # Strategy 6: strip ALL parenthetical/bracket content as last resort
    bare_t = re.sub(r'\s*[\(\[][^\)\]]*[\)\]]', '', title).strip()
    bare_t = _TRAILING_NOISE.sub('', bare_t).strip()
    if bare_t and bare_t != clean_t:
        strategies.append(f'recording:"{bare_t}" AND artist:"{artist}"')
        strategies.append(f'recording:"{bare_t}" AND artist:{clean_a}')

    # Deduplicate while preserving order
    seen = set()
    unique_strategies = []
    for s in strategies:
        if s not in seen:
            seen.add(s)
            unique_strategies.append(s)

    for query in unique_strategies:
        path = f"/ws/2/recording/?query={urllib.parse.quote(query)}&limit=5&fmt=json"
        data = _mb_get(path)

        if data and data.get("recordings"):
            # Score results by how well they match
            best = _pick_best_recording(data["recordings"], clean_t, clean_a)
            if best:
                return best

        time.sleep(_REQUEST_DELAY)

    return None


def _pick_best_recording(recordings: list[dict], title: str, artist: str) -> dict | None:
    """Pick the best matching recording from search results.

    Prefers recordings that have both a release date and genre tags,
    since the whole point is to extract metadata.
    """
    title_lower = title.lower().strip()
    # Also prepare a cleaned version for comparison
    clean_title_lower = _clean_title(title_lower).lower().strip()
    artist_lower = artist.lower().strip()

    scored = []
    for rec in recordings:
        score = 0
        rec_title = rec.get("title", "").lower().strip()
        rec_artists = " ".join(
            a.get("name", "") for a in rec.get("artist-credit", [])
        ).lower()

        # Title match — check both exact and cleaned versions
        if rec_title == title_lower or rec_title == clean_title_lower:
            score += 40
        elif clean_title_lower in rec_title or rec_title in clean_title_lower:
            score += 25
        elif title_lower in rec_title or rec_title in title_lower:
            score += 25

        # Artist match
        if artist_lower in rec_artists:
            score += 30
        elif any(part in rec_artists for part in artist_lower.split()):
            score += 15

        # Strongly prefer recordings with actual release dates
        has_date = any(
            r.get("date") for r in rec.get("releases", [])
            if r.get("date") and r["date"] != "?" and re.match(r'\d', r["date"])
        )
        if has_date:
            score += 30

        # Strongly prefer recordings with tags
        if rec.get("tags"):
            score += 25

        # Higher MB score is a tiebreaker
        mb_score = rec.get("score", 0)
        score += mb_score / 20

        scored.append((score, rec))

    scored.sort(key=lambda x: x[0], reverse=True)

    # Only return if we have a reasonable match
    if scored and scored[0][0] >= 40:
        return scored[0][1]
    return None


def _extract_release_date(recording: dict) -> str:
    """Get the earliest original release year from a recording's releases.

    Returns just the 4-digit year. Prefers dates from the search results,
    but use _get_original_year() for a more thorough lookup.
    """
    dates = []
    for release in recording.get("releases", []):
        date = release.get("date", "")
        if date and date != "?" and re.match(r'\d{4}', date):
            dates.append(date)
        rg = release.get("release-group", {})
        rg_date = rg.get("first-release-date", "")
        if rg_date and rg_date != "?" and re.match(r'\d{4}', rg_date):
            dates.append(rg_date)
    if dates:
        dates.sort()
        return dates[0][:4]
    return ""


def _get_original_year(recording_id: str) -> str:
    """Look up a recording by ID and find the earliest release year across all releases.

    This is more thorough than _extract_release_date because the search API
    only returns a few releases, while this fetches all of them.
    """
    data = _mb_get(
        f"/ws/2/recording/{recording_id}?inc=releases+release-groups&fmt=json"
    )
    if not data:
        return ""

    dates = []
    for release in data.get("releases", []):
        date = release.get("date", "")
        if date and date != "?" and re.match(r'\d{4}', date):
            dates.append(date)
        rg = release.get("release-group", {})
        rg_date = rg.get("first-release-date", "")
        if rg_date and rg_date != "?" and re.match(r'\d{4}', rg_date):
            dates.append(rg_date)

    if dates:
        dates.sort()
        return dates[0][:4]
    return ""


def _get_album_original_year(album: str, artist: str) -> str:
    """Search for an album as a release-group and return its first-release-date.

    This is the most reliable way to get the original release year, since
    release-groups represent the abstract album across all editions/remasters.
    """
    clean_album = _TITLE_NOISE.sub('', album).strip()
    clean_album = _TRAILING_NOISE.sub('', clean_album).strip()
    if not clean_album:
        return ""

    clean_a = _clean_artist(artist)
    query = f'release:"{clean_album}" AND artist:"{clean_a}"'
    path = f"/ws/2/release-group/?query={urllib.parse.quote(query)}&limit=5&fmt=json"
    data = _mb_get(path)

    if not data or not data.get("release-groups"):
        return ""

    # Pick the best match: prefer Album type with earliest first-release-date
    album_lower = clean_album.lower()
    best_date = ""
    for rg in data["release-groups"]:
        rg_title = rg.get("title", "").lower()
        rg_type = rg.get("primary-type", "")
        frd = rg.get("first-release-date", "")

        # Must be a reasonable title match
        if album_lower not in rg_title and rg_title not in album_lower:
            continue

        if not frd or frd == "?" or not re.match(r'\d{4}', frd):
            continue

        year = frd[:4]

        # Prefer Album type over Single/Compilation
        if rg_type == "Album":
            if not best_date or year < best_date:
                best_date = year
        elif not best_date:
            best_date = year

    return best_date


def _extract_genre(recording: dict) -> str:
    """Get genre tags from the recording's release-groups first, then recording."""
    # Prefer release-group (album) genre for consistency across tracks
    for release in recording.get("releases", []):
        rg = release.get("release-group", {})
        rg_tags = rg.get("tags", [])
        if rg_tags:
            rg_tags.sort(key=lambda t: t.get("count", 0), reverse=True)
            return rg_tags[0].get("name", "")

    # Fall back to recording-level tags
    tags = recording.get("tags", [])
    if tags:
        tags.sort(key=lambda t: t.get("count", 0), reverse=True)
        return tags[0].get("name", "")

    return ""


# Cache album genre lookups so all songs from the same album share a genre
_album_genre_cache: dict[str, str] = {}


def _get_album_genre(album: str, artist: str) -> str:
    """Look up the genre for an album via its release-group tags.

    Results are cached so all songs from the same album get the same genre.
    """
    cache_key = f"{album.lower().strip()}|{artist.lower().strip()}"
    if cache_key in _album_genre_cache:
        return _album_genre_cache[cache_key]

    clean_album = _TITLE_NOISE.sub('', album).strip()
    clean_album = _TRAILING_NOISE.sub('', clean_album).strip()
    if not clean_album:
        _album_genre_cache[cache_key] = ""
        return ""

    clean_a = _clean_artist(artist)
    query = f'release:"{clean_album}" AND artist:"{clean_a}"'
    path = f"/ws/2/release-group/?query={urllib.parse.quote(query)}&limit=5&fmt=json"
    data = _mb_get(path)

    genre = ""
    if data and data.get("release-groups"):
        album_lower = clean_album.lower()
        for rg in data["release-groups"]:
            rg_title = rg.get("title", "").lower()
            if album_lower not in rg_title and rg_title not in album_lower:
                continue
            rg_tags = rg.get("tags", [])
            if rg_tags:
                rg_tags.sort(key=lambda t: t.get("count", 0), reverse=True)
                genre = rg_tags[0].get("name", "")
                break

    _album_genre_cache[cache_key] = genre
    return genre


def _lookup_recording_tags(recording_id: str) -> list[dict]:
    """Fetch tags for a specific recording by ID (separate lookup for full tag data)."""
    path = f"/ws/2/recording/{recording_id}?inc=tags+releases+release-groups&fmt=json"
    data = _mb_get(path)
    if not data:
        return []
    return data.get("tags", [])


def enrich_song(song: Song) -> Song:
    """Look up release date and genre for a single song via MusicBrainz."""
    recording = _search_recording(song.title, song.artist, song.album)
    if not recording:
        return song

    if not song.release_date:
        # First try: look up the album's release-group (most reliable for
        # original release year — survives remasters/compilations)
        album_year = ""
        if song.album:
            time.sleep(_REQUEST_DELAY)
            album_year = _get_album_original_year(song.album, song.artist)
            song.release_date = album_year

        # Second try: extract from the recording's search results
        if not song.release_date:
            song.release_date = _extract_release_date(recording)
            # If we got a date but also have an album year, prefer the earlier
            if song.release_date and album_year and album_year < song.release_date:
                song.release_date = album_year

        # Third try: full lookup of all releases for this recording
        if not song.release_date and recording.get("id"):
            time.sleep(_REQUEST_DELAY)
            original_year = _get_original_year(recording["id"])
            if original_year:
                song.release_date = original_year

        # Fourth try: search with cleaned title for the original version
        if not song.release_date:
            clean_t = _clean_title(song.title)
            if clean_t != song.title:
                time.sleep(_REQUEST_DELAY)
                orig = _search_recording(clean_t, song.artist)
                if orig:
                    song.release_date = _extract_release_date(orig)
                    if not song.genre:
                        song.genre = _extract_genre(orig)

    if not song.genre:
        # First try: album-level genre (most consistent — all tracks on an
        # album share the same genre). Results are cached per album.
        if song.album:
            # Check cache first (no API call needed if already looked up)
            cache_key = f"{song.album.lower().strip()}|{song.artist.lower().strip()}"
            if cache_key in _album_genre_cache:
                song.genre = _album_genre_cache[cache_key]
            else:
                time.sleep(_REQUEST_DELAY)
                song.genre = _get_album_genre(song.album, song.artist)

        # Second try: release-group tags from the recording search results
        if not song.genre:
            song.genre = _extract_genre(recording)

        # Third try: direct lookup for recording tags
        if not song.genre and recording.get("id"):
            time.sleep(_REQUEST_DELAY)
            tags = _lookup_recording_tags(recording["id"])
            if tags:
                tags.sort(key=lambda t: t.get("count", 0), reverse=True)
                song.genre = tags[0].get("name", "")

        # Fourth try: artist-level tags as last resort
        if not song.genre:
            artist_credits = recording.get("artist-credit", [])
            for ac in artist_credits:
                artist_obj = ac.get("artist", {})
                artist_id = artist_obj.get("id")
                if artist_id:
                    time.sleep(_REQUEST_DELAY)
                    artist_data = _mb_get(
                        f"/ws/2/artist/{artist_id}?inc=tags&fmt=json"
                    )
                    if artist_data and artist_data.get("tags"):
                        atags = artist_data["tags"]
                        atags.sort(key=lambda t: t.get("count", 0), reverse=True)
                        song.genre = atags[0].get("name", "")
                        break

    return song


def enrich_songs(songs: list[Song], progress_callback=None) -> list[Song]:
    """Enrich a list of songs with release date and genre from MusicBrainz.

    This is rate-limited to ~1 request/second per MusicBrainz guidelines.
    """
    total = len(songs)
    enriched = 0
    skipped = 0

    for i, song in enumerate(songs):
        # Normalize existing dates to year-only
        if song.release_date and len(song.release_date) > 4:
            song.release_date = song.release_date[:4]

        # Skip if already has both fields
        if song.release_date and song.genre:
            skipped += 1
            if progress_callback:
                progress_callback(i + 1, total, enriched)
            continue

        enrich_song(song)

        if song.release_date or song.genre:
            enriched += 1

        if progress_callback:
            progress_callback(i + 1, total, enriched)
        elif (i + 1) % 25 == 0 or i + 1 == total:
            logger.info("Metadata lookup: %d/%d done (%d enriched, %d skipped)",
                        i + 1, total, enriched, skipped)

        # Rate limit (only if we actually made a request)
        time.sleep(_REQUEST_DELAY)

    logger.info("Metadata enrichment complete: %d/%d songs enriched", enriched, total)
    return songs
