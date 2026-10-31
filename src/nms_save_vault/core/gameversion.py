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


#: Raise these (and only these) after the maintenance steps pass for a new game version.
KNOWN = {
    "steam": KnownFormat(formats.META_FORMAT_4, 4231, ("saveN.hg chunked LZ4 (HGSAVEV1)",)),
    "xbox": KnownFormat(formats.META_FORMAT_4, 4228, ("containers.index blob (GameSaveBlob)",)),
}
#: A container kind not listed here stays refused (never decoded): lz4_block.UnsupportedContainer.
REFUSED_CONTAINERS = ("HGSAVEV2",)


def newer(platform: str, meta_format: int, base_version: int) -> bool:
    """Is a save with these meta fields newer than the app's tested format?"""
    known = KNOWN.get(platform)
    if known is None:
        return False
    return meta_format > known.meta_format or base_version > known.base_version


def newer_plain(platform: str, plain: bytes) -> bool:
    """The same for a meta plaintext (Steam: the decrypted meta; Xbox: the blob's meta)."""
    try:
        if platform == "steam":
            info = meta.parse(plain, 0)
            return newer(platform, info.meta_format, info.base_version)
        if len(plain) < msstore.MS_META_LEN_WAYPOINT:
            return False                      # not a meta blob: nothing to judge
        fields = msstore.parse_ms_meta(plain)
        return newer(platform, fields.get("meta_format", 0), fields.get("base_version", 0))
    except (ValueError, KeyError, OSError):
        return False
