"""Resolve well-known locations: the live save folder(s) and the default vault."""
from __future__ import annotations

import os
from pathlib import Path

from . import savedir


def nms_root() -> Path | None:
    """%APPDATA%\\HelloGames\\NMS (the folder that holds the st_<id> save dirs)."""
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    return Path(appdata) / "HelloGames" / "NMS"


def find_live_save_dirs() -> list[Path]:
    """All st_<id> folders under the NMS root that contain saves."""
    root = nms_root()
    if not root or not root.is_dir():
        return []
    return [p for p in sorted(root.glob("st_*")) if p.is_dir() and savedir.looks_like_save_dir(p)]


def default_live_save_dir() -> Path | None:
    dirs = find_live_save_dirs()
    return dirs[0] if dirs else None


def default_vault_dir() -> Path:
    """Vault lives outside any st_ folder (so the game/Steam never touch it)."""
    root = nms_root()
    base = root if root else Path.home()
    return base / "_SaveVault"


def microsoft_root() -> Path | None:
    """The Microsoft Store / Xbox Game Pass 'wgs' save root, if present."""
    from . import msstore

    return msstore.microsoft_root()


def find_microsoft_save_dirs() -> list[Path]:
    """Microsoft / Xbox Game Pass save folders (account folders under wgs)."""
    from . import msstore

    return msstore.find_microsoft_save_dirs()
