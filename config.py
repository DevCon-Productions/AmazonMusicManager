import os
import sys

BASE_URL = "https://music.amazon.com"
USER_DATA_DIR = os.path.join(os.path.dirname(__file__), "browser_data")

# Where the app keeps its data — the ``output/`` tree (spreadsheets,
# organization.json, caches, the web-restricted log). The INSTALLED (frozen)
# build stores it in a per-user, always-writable location so the app works
# even when installed under Program Files; running from source keeps the
# original ``./output`` in the project directory so the dev workflow is
# unchanged. All modules import OUTPUT_DIR from here so they never disagree.
if getattr(sys, "frozen", False):
    _data_base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    OUTPUT_DIR = os.path.join(_data_base, "AmazonMusicManager", "output")
else:
    OUTPUT_DIR = "./output"
try:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
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
