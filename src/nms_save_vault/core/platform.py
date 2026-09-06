"""OS-specific locations: where the game keeps its saves, and where our own files go.

Every resolver here takes a :class:`Host` -- the system name, the home directory and the
environment -- instead of reading ``os.environ`` or ``Path.home()`` itself. That is what
lets the macOS and Linux path logic be exercised from a Windows test run against a
synthetic directory tree (DESIGN.md, D7).

Windows and macOS each have exactly one place the game writes saves. Linux has no native
build at all: No Man's Sky runs under Proton, so its saves sit inside a Wine prefix
belonging to whichever Steam library the game was installed into -- and there can be
several of those (a second drive, a Steam Deck's SD card, a Flatpak install alongside a
native one). That asymmetry is why :func:`nms_roots` returns a list (D1).
"""
from __future__ import annotations

import os
import re
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

WINDOWS = "windows"
MACOS = "macos"
LINUX = "linux"

#: The folder the game creates under whichever application-data root the OS gives it.
NMS_SUBPATH = ("HelloGames", "NMS")

#: Our own config/vault folder name, kept identical across platforms.
APP_DIRNAME = "NMSSaveVault"

#: No Man's Sky on Steam. Names the Proton prefix under ``steamapps/compatdata``.
STEAM_APP_ID = "275850"

#: Steam installs we know how to find, relative to home. Several of these are symlinks to
#: one another on a typical machine, so the results are always deduplicated by real path.
_LINUX_STEAM_ROOTS = (
    (".steam", "steam"),
    (".steam", "root"),
    (".local", "share", "Steam"),
    (".var", "app", "com.valvesoftware.Steam", "data", "Steam"),   # Flatpak
    ("snap", "steam", "common", ".local", "share", "Steam"),       # Snap
)

#: Inside a Wine prefix, the roaming application-data folder. Current Proton builds create
#: ``AppData/Roaming``; older prefixes (and some Wine versions) use the Windows 2000-era
#: ``Application Data`` name instead, so both are probed.
_WINE_APPDATA = (
    ("AppData", "Roaming"),
    ("Application Data",),
)

_WINE_USER = ("pfx", "drive_c", "users", "steamuser")

#: ``libraryfolders.vdf`` entries. Modern Steam writes ``"path" "<dir>"`` inside a numbered
#: block; Steam before 2021 wrote a bare ``"<n>" "<dir>"`` at the top level. One expression
#: covers both, and values that are not paths (the ``apps`` block maps app id -> byte count,
#: which is digits on both sides) are rejected by :func:`_looks_like_path`.
_VDF_ENTRY = re.compile(r'"(?:path|\d+)"\s*"([^"]*)"', re.IGNORECASE)


@dataclass(frozen=True)
class Host:
    """The outside world as the resolvers below are allowed to see it (D7)."""

    system: str
    home: Path
    env: Mapping[str, str]

    @classmethod
    def current(cls) -> "Host":
        return cls(current_system(), Path.home(), os.environ)


def current_system(platform_name: str | None = None) -> str:
    """Map a ``sys.platform`` string onto :data:`WINDOWS` / :data:`MACOS` / :data:`LINUX`.

    Anything unrecognised (BSD, and whatever else CPython runs on) is treated as Linux:
    the XDG layout and ``pgrep`` are the right guesses there.
    """
    name = sys.platform if platform_name is None else platform_name
    if name.startswith("win"):
        return WINDOWS
    if name == "darwin":
        return MACOS
    return LINUX


# --- our own directories ------------------------------------------------------


def user_config_dir(host: Host | None = None) -> Path:
    """Where ``state.json`` belongs when the app is not running from a portable install."""
    host = host or Host.current()
    if host.system == LINUX:
        return _xdg(host, "XDG_CONFIG_HOME", (".config",)) / APP_DIRNAME
    return _app_support(host) / APP_DIRNAME


def user_data_dir(host: Host | None = None) -> Path:
    """Where bulk data (the default vault) belongs off Windows."""
    host = host or Host.current()
    if host.system == LINUX:
        return _xdg(host, "XDG_DATA_HOME", (".local", "share")) / APP_DIRNAME
    return _app_support(host) / APP_DIRNAME


def _app_support(host: Host) -> Path:
    """The per-user application-data root on Windows and macOS."""
    if host.system == MACOS:
        return host.home / "Library" / "Application Support"
    local = host.env.get("LOCALAPPDATA")
    return Path(local) if local else host.home


def _xdg(host: Host, var: str, fallback: tuple[str, ...]) -> Path:
    """An XDG base directory: the variable when it holds an absolute path, else the default.

    The spec says a relative value must be ignored, which matters here because a stray
    relative ``XDG_DATA_HOME`` would otherwise put the vault somewhere unpredictable.
    """
    value = host.env.get(var)
    if value:
        candidate = Path(value)
        if candidate.is_absolute():
            return candidate
    return host.home.joinpath(*fallback)


# --- the game's save roots ----------------------------------------------------


def primary_nms_root(host: Host | None = None) -> Path | None:
    """The one canonical save root for this OS, whether or not it exists yet.

    Windows and macOS have a fixed location, so callers that need a path before the game
    has ever run (the default vault location) can rely on it. Linux has no such place --
    the saves live in whichever Steam library holds the install -- so this is ``None``
    there and callers must fall back.
    """
    host = host or Host.current()
    if host.system == WINDOWS:
        appdata = host.env.get("APPDATA")
        return Path(appdata).joinpath(*NMS_SUBPATH) if appdata else None
    if host.system == MACOS:
        return host.home / "Library" / "Application Support" / Path(*NMS_SUBPATH)
    return None


def nms_roots(host: Host | None = None) -> list[Path]:
    """Every NMS save root that actually exists on this machine (D1).

    Zero or one on Windows; on macOS also any sandboxed App Store container; zero or more
    on Linux, one per Steam library that has a Proton prefix for the game.
    """
    host = host or Host.current()
    if host.system == LINUX:
        candidates = proton_nms_roots(host)
    else:
        candidates = [p for p in (primary_nms_root(host),) if p is not None]
        if host.system == MACOS:
            candidates += _macos_container_roots(host)
    return _existing_dirs(candidates)


def _macos_container_roots(host: Host) -> list[Path]:
    """Save roots inside a sandboxed (App Store) app's container.

    The bundle id is not known -- no App Store install has been inspected -- so this globs
    rather than guessing one, and finds nothing on a Steam-only machine.
    """
    containers = host.home / "Library" / "Containers"
    try:
        matches = containers.glob(
            str(Path("*", "Data", "Library", "Application Support", *NMS_SUBPATH))
        )
        return sorted(matches)
    except OSError:
        return []


# --- Linux: Steam libraries and Proton prefixes -------------------------------


def steam_roots(host: Host | None = None) -> list[Path]:
    """Steam installations found under the user's home, deduplicated by real path."""
    host = host or Host.current()
    return _existing_dirs(host.home.joinpath(*parts) for parts in _LINUX_STEAM_ROOTS)


def steam_libraries(host: Host | None = None) -> list[Path]:
    """Every Steam library folder: the installs themselves plus what they point at.

    A library on a second drive or an SD card is not reachable from the home directory at
    all -- the only record of it is ``libraryfolders.vdf`` inside an install we did find.
    """
    host = host or Host.current()
    libraries = steam_roots(host)
    for root in list(libraries):
        vdf = root / "steamapps" / "libraryfolders.vdf"
        try:
            text = vdf.read_text("utf-8", errors="replace")
        except OSError:
            continue
        libraries.extend(parse_libraryfolders(text))
    return _existing_dirs(libraries)


def parse_libraryfolders(text: str) -> list[Path]:
    """Pull the library paths out of a ``libraryfolders.vdf``.

    Deliberately not a general VDF parser: this file is the only VDF the app reads, and a
    regex over the two shapes Steam has used keeps the runtime dependency-free (D6).
    """
    out: list[Path] = []
    for raw in _VDF_ENTRY.findall(text):
        # VDF escapes backslashes, so a Windows library reads as "D:\\SteamLibrary".
        value = raw.replace("\\\\", "\\")
        if _looks_like_path(value):
            out.append(Path(value))
    return out


def _looks_like_path(value: str) -> bool:
    """Reject the non-path values the same expression inevitably also matches.

    The ``apps`` block maps app id to byte count -- ``"275850" "18000000000"`` -- which is
    digits on both sides and would otherwise be read as a library folder.
    """
    return bool(value) and ("/" in value or "\\" in value)


def proton_nms_roots(host: Host | None = None) -> list[Path]:
    """The game's save root inside every Proton prefix we can find."""
    host = host or Host.current()
    out: list[Path] = []
    for library in steam_libraries(host):
        prefix = library / "steamapps" / "compatdata" / STEAM_APP_ID
        for appdata in _WINE_APPDATA:
            out.append(prefix.joinpath(*_WINE_USER, *appdata, *NMS_SUBPATH))
    return out


# --- helpers ------------------------------------------------------------------


def _existing_dirs(paths: Iterable[Path]) -> list[Path]:
    """Keep the directories that exist, in order, one entry per real location.

    Deduplication is by resolved path rather than by name because the Steam roots above are
    largely symlinks to each other: ``~/.steam/steam``, ``~/.steam/root`` and
    ``~/.local/share/Steam`` are routinely the same folder, and without this the same saves
    would be offered three times as three separate accounts.
    """
    out: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        try:
            if not path.is_dir():
                continue
            key = path.resolve()
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out
