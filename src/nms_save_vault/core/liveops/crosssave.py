"""Whether Hello Games cross-save is on for an account (D5 2.3.3 ``CROSS_SAVE_ON``).

The account's ``accountdata.hg`` carries a ``CrossSaves`` flag. It is read as the game writes
it (:mod:`core.accountdata`: plain JSON text) with the computed save key (core.hashname);
nothing is indexed or cached.
``None`` means unknown: no file, an Xbox account (its account data lives in a container
this reader does not open), unreadable data, or no such flag.
"""
from __future__ import annotations

from pathlib import Path

from .. import accountdata, hashname

FLAG = "CrossSaves"


def cross_save_on(folder: Path, platform: str) -> bool | None:
    root = accountdata.read(folder, platform)
    if root is None:
        return None
    found = _find(root, hashname.key(FLAG))
    if isinstance(found, bool):
        return found
    if isinstance(found, int) and found in (0, 1):
        return bool(found)
    return None


def _find(node: object, key: str, depth: int = 0) -> object:
    """The value of the first ``key`` in the tree (breadth is small: it is account data)."""
    if depth > 6 or not isinstance(node, dict):
        return None
    if key in node:
        return node[key]
    for child in node.values():
        got = _find(child, key, depth + 1)
        if got is not None:
            return got
    return None
