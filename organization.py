"""Virtual folder tree for organizing playlists and spreadsheets.

The tree is stored in JSON at `./output/organization.json`. Each folder has
sub-folders and items; items are either a playlist reference or a spreadsheet
reference:

    {"kind": "playlist",    "url": str, "name": str}
    {"kind": "spreadsheet", "path": str, "name": str}

The same item may appear in multiple folders — nothing dedupes across the tree.
"""

from __future__ import annotations

import json
import os
from typing import Iterable

from config import OUTPUT_DIR as _OUTPUT_DIR
ORG_FILE = os.path.join(_OUTPUT_DIR, "organization.json")
_VERSION = 1


def to_stored_path(path: str) -> str:
    """Convert a filesystem path to a portable relative path for storage in org.

    Paths inside OUTPUT_DIR are stored as relative (e.g. "MyList.xlsx").
    Paths on a different drive or outside OUTPUT_DIR are kept absolute.
    """
    if not path:
        return path
    try:
        rel = os.path.relpath(path, _OUTPUT_DIR)
        if not rel.startswith(".."):
            return rel
    except ValueError:
        pass
    return os.path.abspath(path)


def from_stored_path(path: str) -> str:
    """Resolve a stored spreadsheet path to a filesystem path.

    Handles three formats:
      - Absolute path (legacy on same machine): returned unchanged.
      - Legacy relative: "./output/filename.xlsx" — strips the prefix first.
      - New relative: "filename.xlsx" — joined directly with OUTPUT_DIR.
    """
    if not path or os.path.isabs(path):
        return path
    norm = path.replace("\\", "/")
    for prefix in ("./output/", "output/"):
        if norm.startswith(prefix):
            path = norm[len(prefix):]
            break
    return os.path.normpath(os.path.join(_OUTPUT_DIR, path))


def _default_org() -> dict:
    return {"version": _VERSION, "root": {"folders": {}, "items": []}}


def load_org() -> dict:
    if not os.path.exists(ORG_FILE):
        return _default_org()
    try:
        with open(ORG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return _default_org()
    if not isinstance(data, dict) or "root" not in data:
        return _default_org()
    return data


def save_org(org: dict) -> None:
    os.makedirs(os.path.dirname(ORG_FILE) or ".", exist_ok=True)
    with open(ORG_FILE, "w", encoding="utf-8") as f:
        json.dump(org, f, indent=2)


def path_to_str(path: list[str]) -> str:
    return "/" if not path else "/" + "/".join(path)


def get_node(org: dict, path: list[str]) -> dict | None:
    node = org["root"]
    for name in path:
        folders = node.get("folders", {})
        if name not in folders:
            return None
        node = folders[name]
    return node


def list_folders(org: dict, path: list[str]) -> list[str]:
    node = get_node(org, path)
    if not node:
        return []
    return sorted(node.get("folders", {}).keys(), key=str.lower)


def list_items(org: dict, path: list[str]) -> list[dict]:
    node = get_node(org, path)
    return list(node.get("items", [])) if node else []


def create_folder(org: dict, path: list[str], name: str) -> None:
    name = name.strip()
    if not name:
        raise ValueError("Folder name cannot be empty")
    if "/" in name:
        raise ValueError("Folder names cannot contain '/'")
    parent = get_node(org, path)
    if parent is None:
        raise ValueError(f"Folder not found: {path_to_str(path)}")
    parent.setdefault("folders", {})
    if name in parent["folders"]:
        raise ValueError(f"Folder '{name}' already exists here")
    parent["folders"][name] = {"folders": {}, "items": []}


def delete_folder(org: dict, path: list[str]) -> None:
    if not path:
        raise ValueError("Cannot delete the root folder")
    parent = get_node(org, path[:-1])
    if parent is None or path[-1] not in parent.get("folders", {}):
        raise ValueError(f"Folder not found: {path_to_str(path)}")
    del parent["folders"][path[-1]]


def rename_folder(org: dict, path: list[str], new_name: str) -> None:
    if not path:
        raise ValueError("Cannot rename the root folder")
    new_name = new_name.strip()
    if not new_name:
        raise ValueError("Name cannot be empty")
    if "/" in new_name:
        raise ValueError("Folder names cannot contain '/'")
    parent = get_node(org, path[:-1])
    old = path[-1]
    if new_name == old:
        return
    if new_name in parent["folders"]:
        raise ValueError(f"Folder '{new_name}' already exists here")
    parent["folders"][new_name] = parent["folders"].pop(old)


def add_item(org: dict, path: list[str], item: dict) -> None:
    node = get_node(org, path)
    if node is None:
        raise ValueError(f"Folder not found: {path_to_str(path)}")
    node.setdefault("items", []).append(item)


def remove_item(org: dict, path: list[str], index: int) -> dict:
    node = get_node(org, path)
    items = node.get("items", []) if node else []
    if not (0 <= index < len(items)):
        raise ValueError("Item not found")
    return node["items"].pop(index)


def prune_playlists_not_in(org: dict, url_set: set) -> int:
    """Remove every playlist entry whose URL is NOT in *url_set*.

    Use after a fresh Amazon fetch to drop stale entries (e.g. deleted playlists).
    Returns the number of entries removed.
    """
    removed = 0

    def _prune(node: dict) -> None:
        nonlocal removed
        before = len(node.get("items", []))
        node["items"] = [
            it for it in node.get("items", [])
            if not (it.get("kind") == "playlist" and it.get("url") not in url_set)
        ]
        removed += before - len(node["items"])
        for child in node.get("folders", {}).values():
            _prune(child)

    _prune(org["root"])
    return removed


def remove_playlist_by_url(org: dict, url: str) -> int:
    """Remove every playlist entry whose URL matches *url* from the entire tree.

    Returns the number of entries removed.
    """
    removed = 0

    def _prune(node: dict) -> None:
        nonlocal removed
        before = len(node.get("items", []))
        node["items"] = [
            it for it in node.get("items", [])
            if not (it.get("kind") == "playlist" and it.get("url") == url)
        ]
        removed += before - len(node["items"])
        for child in node.get("folders", {}).values():
            _prune(child)

    _prune(org["root"])
    return removed


def iter_playlists_recursive(org: dict, path: list[str]) -> Iterable[dict]:
    """Yield playlist items from this folder and every descendant folder."""
    node = get_node(org, path)
    if not node:
        return
    for item in node.get("items", []):
        if item.get("kind") == "playlist":
            yield item
    for name in node.get("folders", {}):
        yield from iter_playlists_recursive(org, path + [name])


def _walk_all_nodes(node: dict):
    yield node
    for sub in node.get("folders", {}).values():
        yield from _walk_all_nodes(sub)


def sync_playlist_names(org: dict, playlists: list[dict]) -> bool:
    """Update playlist names in org from a fresh Amazon fetch result.

    Returns True if any name was changed (caller should save_org).
    """
    by_url = {pl["url"]: pl["name"] for pl in playlists if pl.get("url") and pl.get("name")}
    changed = False
    for node in _walk_all_nodes(org.get("root", {})):
        for item in node.get("items", []):
            if item.get("kind") == "playlist":
                url = item.get("url") or ""
                new_name = by_url.get(url)
                if new_name and item.get("name") != new_name:
                    item["name"] = new_name
                    changed = True
    return changed
