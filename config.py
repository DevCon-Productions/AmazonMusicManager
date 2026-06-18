import os
import sys

BASE_URL = "https://music.amazon.com"

# Data root — EVERY path the app writes (output tree, Chrome profile, log,
# screenshots) derives from here, so nothing lands in a read-only install dir.
# The INSTALLED (frozen) build uses a per-user, always-writable location so the
# app works even when installed under Program Files; running from source uses
# the project directory, so the dev workflow is unchanged. All modules import
# these from here so they never disagree.
if getattr(sys, "frozen", False):
    _data_base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    DATA_DIR = os.path.join(_data_base, "AmazonMusicManager")
else:
    DATA_DIR = "."

OUTPUT_DIR = os.path.join(DATA_DIR, "output")          # spreadsheets, org tree, caches
USER_DATA_DIR = os.path.join(DATA_DIR, "browser_data")  # Chrome profile / login session
LOG_FILE = os.path.join(DATA_DIR, "amazonmusic.log")
SCREENSHOT_DIR = os.path.join(DATA_DIR, "screenshots")
for _d in (OUTPUT_DIR, USER_DATA_DIR, SCREENSHOT_DIR):
    try:
        os.makedirs(_d, exist_ok=True)
    except OSError:
        pass

DEFAULT_TIMEOUT = 15_000   # 15s
LONG_TIMEOUT = 30_000      # 30s
LOGIN_TIMEOUT = 300_000    # 5 minutes for manual login
SLOW_MO = 100              # ms between Playwright actions
SCROLL_PAUSE = 1500        # ms to wait after scrolling for virtual list to update
ROW_HEIGHT = 80            # px height of each music-image-row in the virtual list

# Confidence thresholds for hybrid song selection
AUTO_SELECT_THRESHOLD = 90   # >= this: pick automatically
PROMPT_THRESHOLD = 60        # >= this but < auto: ask user to choose
# < PROMPT_THRESHOLD: skip / warn

# CSS selectors for Amazon Music web player.
# Amazon Music uses custom web components (music-*) with shadow DOMs.
# Song data lives as attributes directly on music-image-row elements.
SELECTORS = {
    # Page load indicator
    "app_loaded": "music-app",

    # Playlist detail header — headline attribute has the playlist name
    "playlist_header": "music-detail-header",

    # Song rows in a playlist — all data is in element attributes
    "track_row": "music-image-row",

    # Virtual scroll container (the scrollable wrapper around track rows)
    "virtual_scroll_container": '[style*="will-change: transform"][style*="overflow"]',

    # Search
    "search_input": 'input[aria-label*="Search"], input[type="search"], input[placeholder*="Search"]',
    "search_result_row": "music-image-row",

    # Playlist creation
    "create_playlist_btn": '[aria-label*="Create playlist"], button[aria-label*="Create"]',
    "playlist_name_input": 'input[placeholder*="playlist"], input[placeholder*="Playlist"], input[aria-label*="playlist name"]',

    # Add to playlist (from search results / context menu)
    "more_options_btn": 'music-button[icon-name="more"]',
    "add_to_playlist_option": '[aria-label*="Add to playlist"]',
}
