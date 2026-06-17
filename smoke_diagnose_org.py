"""One-off diagnostic for the smoke script — print the Classical org subtree
top-level folders, then check what's actually under Grieg."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from organization import load_org, get_node, iter_playlists_recursive
from gui import _build_classical_performance_pool


def _walk(node: dict, indent: int = 0, max_depth: int = 4) -> None:
    if indent >= max_depth:
        return
    if isinstance(node, dict):
        folders = node.get("folders") or {}
        items = node.get("items") or []
        for name, sub in folders.items():
            print(f"{'  ' * indent}[{name}]")
            _walk(sub, indent + 1, max_depth)
        for it in items:
            url = (it.get("url") or "").strip()
            label = it.get("name") or url or "<unnamed>"
            tag = "PL" if url else "sheet"
            print(f"{'  ' * indent}- {tag} {label}")


def main() -> int:
    org = load_org()
    print("Classical top-level:")
    classical = get_node(org, ["Classical"])
    if not classical:
        print("  [no Classical node in org]")
        return 1
    for name in (classical.get("folders") or {}).keys():
        print(f"  - {name}")

    print("\nGrieg subtree (depth 4):")
    grieg = get_node(org, ["Classical", "Grieg"])
    if not grieg:
        print("  [no Classical/Grieg]")
        return 1
    _walk(grieg, 1)

    print("\nPlaylists under Classical/Grieg (recursive):")
    n = 0
    for pl in iter_playlists_recursive(org, ["Classical", "Grieg"]):
        n += 1
        print(f"  - {pl.get('name') or '<unnamed>'}  url={pl.get('url')!r}")
    print(f"  ({n} playlists)")

    # Try building pool on each candidate root we found.
    for path in [
        ["Classical", "Grieg"],
        ["Classical", "Grieg", "Piano Concerto in A Minor"],
        ["Classical", "Grieg", "Piano Concerto"],
        ["Classical", "Grieg", "Concertos"],
    ]:
        if not get_node(org, path):
            print(f"\nNo node at {path}")
            continue
        pool, skipped, restricted = _build_classical_performance_pool(org, path)
        print(f"\nPool at {path}: {len(pool)} perfs, "
              f"skipped {len(skipped)}, restricted {restricted}")
        for p in pool[:8]:
            first = p.titles[0] if p.titles else "<none>"
            print(f"   - {p.track_count}-track  {p.artist!r}  first={first!r}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
