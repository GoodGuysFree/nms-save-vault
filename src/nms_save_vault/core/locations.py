"""Resolve well-known locations: the live save folder(s) and the default vault.

A thin, no-argument layer over :mod:`core.platform`, which does the per-OS work and takes
its environment as a parameter so it can be tested anywhere (D7).
"""
from __future__ import annotations

from pathlib import Path

from . import savedir
from . import platform as host_platform
from .platform import Host


def nms_root() -> Path | None:
    """The canonical NMS folder for this OS, or ``None`` where there is no single one.

    Windows ``%APPDATA%\\HelloGames\\NMS`` / macOS
    ``~/Library/Application Support/HelloGames/NMS``. ``None`` on Linux, where the saves
    live inside whichever Steam library holds the Proton prefix -- use :func:`nms_roots`
    for anything that has to find saves.
    """
    return host_platform.primary_nms_root()


def nms_roots() -> list[Path]:
    """Every NMS save folder that exists on this machine; several are possible on Linux."""
    return host_platform.nms_roots()


def find_live_save_dirs() -> list[Path]:
    """All st_<id> folders, under every NMS root, that contain saves."""
    out: list[Path] = []
    seen: set[Path] = set()
    for root in nms_roots():
        for child in sorted(root.glob("st_*")):
            if not (child.is_dir() and savedir.looks_like_save_dir(child)):
                continue
            try:
                key = child.resolve()
            except OSError:
                continue
            if key not in seen:
                seen.add(key)
                out.append(child)
    return out


def default_live_save_dir() -> Path | None:
    dirs = find_live_save_dirs()
    return dirs[0] if dirs else None


def default_vault_dir() -> Path:
    """Where a new vault goes by default.

    On Windows it sits beside the ``st_`` folders but outside them, so the game and Steam
    never touch it. Off Windows it goes under the user's data directory instead (D2):
    a Linux vault must not live inside the Proton prefix, because Steam deletes and
    recreates a prefix on its own account and would take the backups with it.
    """
    host = Host.current()
    if host.system == host_platform.WINDOWS:
        root = nms_root()
        return (root if root else host.home) / "_SaveVault"
    return host_platform.user_data_dir(host) / "Vault"


def microsoft_root() -> Path | None:
    """The Microsoft Store / Xbox Game Pass 'wgs' save root, if present."""
    from . import msstore

    return msstore.microsoft_root()


def find_microsoft_save_dirs() -> list[Path]:
    """Microsoft / Xbox Game Pass save folders (account folders under wgs)."""
    from . import msstore

    return msstore.find_microsoft_save_dirs()
