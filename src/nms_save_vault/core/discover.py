"""Find NMS save folders and classify them into *live* sources vs *backups*.

The rules are deliberately conservative and name-based, so a misfire can never turn
a backup into a live target:

* **Steam live** -- a folder named *exactly* ``st_<17-digit-steamid64>`` that sits
  directly under ``%APPDATA%\\HelloGames\\NMS`` and looks like a save dir. Each
  distinct steamid64 is its own account (so a machine with two Steam accounts gets
  two live sources).
* **Xbox live** -- each Game Pass account folder under the ``wgs`` root (read-only).
* **In-place backup** -- anything else under the NMS root that still contains save
  files: a hand-pasted ``st_... - Copy``, a renamed/dated folder, etc. These are
  *not* live targets; they are handed to the catalog as in-place backups.

Discovery is strictly read-only: it stats and name-matches folders, it never writes
into or modifies any save directory.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import catalog, locations, savedir, state
from .state import (
    ORIGIN_AUTO,
    ORIGIN_MANUAL,
    PLATFORM_STEAM,
    PLATFORM_XBOX,
    ROLE_LIVE,
    AppState,
    Source,
)

# A Steam friend-code / steamid64 is a 17-digit number. The canonical live folder
# is named exactly "st_<steamid64>"; copies get a suffix (" - Copy", " (1)", ...).
_CANONICAL_STEAM = re.compile(r"^st_(\d{17})$")
_STEAMID = re.compile(r"(\d{17})")


def steam_account_id(path: str | Path) -> str | None:
    """The steamid64 embedded in a folder name, if any (works for copies too)."""
    m = _STEAMID.search(Path(path).name)
    return m.group(1) if m else None


def is_canonical_steam_live(path: str | Path, nms_root: str | Path) -> bool:
    """True for an exactly-named ``st_<id>`` folder directly under the NMS root."""
    p = Path(path)
    if _CANONICAL_STEAM.match(p.name) is None:
        return False
    try:
        same_parent = p.parent.resolve() == Path(nms_root).resolve()
    except OSError:
        return False
    return same_parent and savedir.looks_like_save_dir(p)


def _steam_label(account: str, index: int) -> str:
    suffix = "" if index == 0 else f" #{index + 1}"
    return f"Steam ({account}){suffix}" if account else f"Steam save{suffix}"


def _xbox_label(account: str, index: int) -> str:
    suffix = "" if index == 0 else f" #{index + 1}"
    return f"Xbox / Game Pass{suffix}"


def discover_live_sources(
    nms_root: str | Path | None = None,
    ms_dirs: list[Path] | None = None,
) -> list[Source]:
    """Build the list of live sources currently on disk (Steam + Xbox)."""
    root = Path(nms_root) if nms_root else locations.nms_root()
    sources: list[Source] = []

    steam_dirs: list[Path] = []
    if root and root.is_dir():
        for child in sorted(root.glob("st_*")):
            if child.is_dir() and is_canonical_steam_live(child, root):
                steam_dirs.append(child)
    for i, d in enumerate(steam_dirs):
        account = steam_account_id(d) or ""
        sources.append(
            Source(
                id=f"{PLATFORM_STEAM}-{account or d.name}",
                platform=PLATFORM_STEAM,
                account=account,
                path=str(d),
                label=_steam_label(account, i),
                role=ROLE_LIVE,
                writable=True,
                origin=ORIGIN_AUTO,
            )
        )

    xbox_dirs = ms_dirs if ms_dirs is not None else locations.find_microsoft_save_dirs()
    for i, d in enumerate(xbox_dirs):
        account = Path(d).name
        sources.append(
            Source(
                id=f"{PLATFORM_XBOX}-{account}",
                platform=PLATFORM_XBOX,
                account=account,
                path=str(d),
                label=_xbox_label(account, i),
                role=ROLE_LIVE,
                writable=False,  # Xbox saves are read-only in this tool
                origin=ORIGIN_AUTO,
            )
        )
    return sources


def discover_inplace_backups(
    nms_root: str | Path | None = None,
    *,
    exclude: list[str | Path] = (),
) -> list[Path]:
    """Save folders under the NMS root that are *not* live targets -- i.e. backups."""
    root = Path(nms_root) if nms_root else locations.nms_root()
    if not root or not root.is_dir():
        return []
    excluded = list(exclude)
    found = catalog.discover_save_dirs(root, exclude=excluded)
    live = {Path(p).resolve() for p in excluded}
    out: list[Path] = []
    for d in found:
        rd = d.resolve()
        if rd in live or is_canonical_steam_live(d, root):
            continue
        out.append(d)
    return out


def bootstrap_state(
    nms_root: str | Path | None = None,
    ms_dirs: list[Path] | None = None,
) -> AppState:
    """Fresh state for first run: all live sources, default vault, no backups yet."""
    sources = discover_live_sources(nms_root, ms_dirs=ms_dirs)
    return AppState(sources=sources, vault=str(locations.default_vault_dir()))


def merge_live_sources(
    existing: AppState,
    nms_root: str | Path | None = None,
    ms_dirs: list[Path] | None = None,
) -> int:
    """Re-scan and fold newly found live sources into ``existing`` in place.

    Manual entries are never touched. An auto entry that still exists has its path
    refreshed; brand-new sources are added as ``origin=auto``. Returns how many
    sources were added.
    """
    added = 0
    for found in discover_live_sources(nms_root, ms_dirs=ms_dirs):
        current = existing.get(found.id)
        if current is None:
            existing.upsert(found)
            added += 1
        elif current.origin == ORIGIN_AUTO:
            current.path = found.path  # refresh in case the folder moved
            current.writable = found.writable
    return added
