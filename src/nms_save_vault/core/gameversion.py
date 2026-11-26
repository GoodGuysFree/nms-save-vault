"""The newest save format this app is tested with, and what to do with a newer one (D5 guard).

A save written by a newer game version stays readable and indexable on a best-effort basis, but
no live write may read from it or replace it (Place, Swap, Promote, Undo, restore): the app does
not know what changed in the format, so it could silently damage a save. The block carries the
code ``GAME_NEWER`` and the plain text below; the Organizer shows a notice on such saves.

Maintenance after a game update: run ``nmsvault-cli selftest`` and ``tools/check_upstream.py``,
regenerate the pins (``tools/gen_lookups.py``), then raise the numbers below (DESIGN.md,
"Maintenance after a game update").
"""
from __future__ import annotations

from dataclasses import dataclass

from . import formats, meta, msstore

GAME_NEWER = "GAME_NEWER"
GAME_NEWER_TEXT = ("This save was written by a newer game version than this app knows. "
                   "Update the app before moving it.")


@dataclass(frozen=True)
class KnownFormat:
    meta_format: int         # the newest meta format (the u32 at 0x04) the app is tested with
    base_version: int        # the newest BaseVersion the app is tested with
    containers: tuple[str, ...]


#: The guard of GOG's folder (formats.GOG_ACCOUNT_DIR). GOG writes the Steam format, so its saves
#: are platform "steam" everywhere else; only the version its game knows can lag Steam's.
GOG = "gog"

#: Raise these (and only these) after the maintenance steps pass for a new game version.
KNOWN = {
    # 4235: Steam build 26918301 (NMS.exe 186686, 2026-11-25); Xbox stays until its package
    # updates (INSTALLED.json same_release)
    "steam": KnownFormat(formats.META_FORMAT_4, 4235, ("saveN.hg chunked LZ4 (HGSAVEV1)",)),
    "xbox": KnownFormat(formats.META_FORMAT_4, 4234, ("containers.index blob (GameSaveBlob)",)),
    # 4235: GOG NMS.exe 186923, the build of Steam 7.15.1 (INSTALLED.json gog_same_release)
    GOG: KnownFormat(formats.META_FORMAT_4, 4235, ("saveN.hg chunked LZ4 (HGSAVEV1)",)),
}
#: A container kind not listed here stays refused (never decoded): lz4_block.UnsupportedContainer.
REFUSED_CONTAINERS = ("HGSAVEV2",)


def guard_for(platform: str, account_key: str = "") -> str:
    """The :data:`KNOWN` key a live folder is judged by: :data:`GOG` for GOG's own folder,
    else its platform."""
    return GOG if platform == "steam" and account_key == formats.GOG_ACCOUNT_DIR else platform


def newer(platform: str, meta_format: int, base_version: int) -> bool:
    """Is a save with these meta fields newer than the app's tested format? ``platform`` is a
    :data:`KNOWN` key (:func:`guard_for` gives a live folder's)."""
    known = KNOWN.get(platform)
    if known is None:
        return False
    return meta_format > known.meta_format or base_version > known.base_version


def newer_plain(platform: str, plain: bytes, *, guard: str | None = None) -> bool:
    """The same for a meta plaintext (Steam: the decrypted meta; Xbox: the blob's meta), read
    as ``platform``'s and judged by ``guard`` (a :data:`KNOWN` key; default ``platform``)."""
    guard = guard or platform
    try:
        if platform in ("steam", GOG):
            info = meta.parse(plain, 0)
            return newer(guard, info.meta_format, info.base_version)
        if len(plain) < msstore.MS_META_LEN_WAYPOINT:
            return False                      # not a meta blob: nothing to judge
        fields = msstore.parse_ms_meta(plain)
        return newer(guard, fields.get("meta_format", 0), fields.get("base_version", 0))
    except (ValueError, KeyError, OSError):
        return False
