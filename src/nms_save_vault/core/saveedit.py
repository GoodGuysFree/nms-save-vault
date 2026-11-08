"""A save's data and meta rebuilt after a content edit (CL5).

The edited JSON (:mod:`.savepatch`) is compressed again (:func:`lz4_block.compress`) and the
meta gets the new sizes, set the way the game sets them. Nothing else in the meta changes:
its name, play time, playthrough id (0x15C) and save time (0x164) stay as they were, so the
live operation that places the result decides the save time exactly as it does for any
version it places.

The sizes, as real saves show them (Steam, Worlds Part II, 432-byte meta, decrypted):

- 0x38 is the decompressed size, the JSON with its NUL; the game checks it against the data.
- 0x3C is the size of the container on disk.
- 0x54 repeats the decompressed size in every meta the game wrote. Saves another tool edited
  keep an old value there (and some an old 0x3C) and still load; a rebuilt meta has all
  three right.

Xbox (360-byte meta, as stored): 0x10 is the decompressed size.

Only those two meta layouts are rebuilt; a save of a newer game than this core knows, a meta
whose sizes disagree with its own data, or an old container (one bare LZ4 block, HGSAVEV2)
is refused with :class:`EditRefused`. Pure functions: nothing is read or written here.
"""
from __future__ import annotations

import struct
from collections.abc import Iterable
from dataclasses import dataclass

from . import formats, gameversion, lz4_block, meta, msstore, savepatch

#: Steam: the second copy of the decompressed size (see the module text).
OFF_SIZE_DECOMPRESSED_AGAIN = 0x54

STEAM_META_LEN = formats.META_LEN_WORLDS_II
XBOX_META_LEN = msstore.MS_META_LEN_WORLDS_II


class EditRefused(ValueError):
    """A save this core will not rebuild, or a rebuild that did not check out."""


@dataclass(frozen=True)
class Rebuilt:
    data: bytes                 # the new container
    meta: bytes                 # Steam: the decrypted meta; Xbox: the meta as stored
    size_decompressed: int      # the JSON with its NUL
    size_disk: int              # len(data)


def body_of(data: bytes) -> bytes:
    """The JSON text of a current save container, without its NUL."""
    if data.startswith(lz4_block.HGSAVEV2_HEADER) or not lz4_block.is_chunked(data):
        raise EditRefused("an old save container (load and save it once in the game first)")
    try:
        raw = lz4_block.decompress(data)
    except ValueError as exc:
        raise EditRefused(f"the save data cannot be read ({exc})") from None
    body, sep, tail = raw.partition(b"\x00")
    if not sep or tail:
        raise EditRefused("the save data does not end with exactly one NUL after its JSON")
    return body


def rebuild(platform: str, data: bytes, meta_plain: bytes,
            edits: Iterable[savepatch.Edit]) -> Rebuilt:
    """``data`` with ``edits`` made (each proven, :func:`savepatch.apply`), compressed again,
    and ``meta_plain`` with its sizes set. Refused with :class:`EditRefused`, or
    :class:`savepatch.PatchRefused` for an edit that cannot be made exactly."""
    if platform == "steam":
        recorded, base_version, meta_format = _steam_fields(meta_plain)
    elif platform == "xbox":
        recorded, base_version, meta_format = _xbox_fields(meta_plain)
    else:
        raise EditRefused(f"not a platform: {platform!r}")
    if gameversion.newer(platform, meta_format, base_version):
        raise EditRefused(gameversion.GAME_NEWER_TEXT)
    body = body_of(data)
    chunks, _ = lz4_block.walk_chunks(data)
    if sum(c.decompressed_size for c in chunks) != recorded:
        raise EditRefused("the meta's size does not match its own data; not touching it")
    new_body = savepatch.apply(body, edits)
    raw = new_body + b"\x00"
    try:
        new_data = lz4_block.compress(raw)
    except lz4_block.LZ4FormatError as exc:
        raise EditRefused(f"the edited save could not be compressed ({exc})") from None
    if body_of(new_data) != new_body:                  # as the game will read it back
        raise EditRefused("the rebuilt save does not read back as the edited JSON")
    if platform == "steam":
        new_meta = _steam_sizes(meta_plain, len(raw), len(new_data))
    else:
        new_meta = _xbox_size(meta_plain, len(raw))
    return Rebuilt(new_data, new_meta, len(raw), len(new_data))


# --- Steam ---------------------------------------------------------------------------------


def _steam_fields(plain: bytes) -> tuple[int, int, int]:
    if len(plain) != STEAM_META_LEN:
        raise EditRefused(f"a Steam meta of {len(plain)} bytes (this core rebuilds "
                          f"{STEAM_META_LEN}-byte ones)")
    info = meta.parse(plain, 0)
    if info.header != formats.META_HEADER:
        raise EditRefused("the Steam meta is not decrypted")
    return info.size_decompressed, info.base_version, info.meta_format


def _steam_sizes(plain: bytes, size_decompressed: int, size_disk: int) -> bytes:
    out = bytearray(plain)
    struct.pack_into("<I", out, formats.OFF_SIZE_DECOMPRESSED, size_decompressed)
    struct.pack_into("<I", out, formats.OFF_SIZE_DISK, size_disk)
    struct.pack_into("<I", out, OFF_SIZE_DECOMPRESSED_AGAIN, size_decompressed)
    new = bytes(out)
    sizes = {formats.OFF_SIZE_DECOMPRESSED, formats.OFF_SIZE_DISK, OFF_SIZE_DECOMPRESSED_AGAIN}
    changed = {i - i % 4 for i in range(len(plain)) if plain[i] != new[i]}
    before, after = meta.parse(plain, 0), meta.parse(new, 0)
    if not changed <= sizes or (after.size_decompressed, after.size_disk) != (
            size_decompressed, size_disk) or (
            after.slot_identifier, after.timestamp, after.save_name, after.total_play_time) != (
            before.slot_identifier, before.timestamp, before.save_name, before.total_play_time):
        raise EditRefused("the rebuilt Steam meta does not read back as expected")
    return new


# --- Xbox ----------------------------------------------------------------------------------


def _xbox_fields(stored: bytes) -> tuple[int, int, int]:
    if len(stored) != XBOX_META_LEN:
        raise EditRefused(f"an Xbox meta of {len(stored)} bytes (this core rebuilds "
                          f"{XBOX_META_LEN}-byte ones)")
    fields = msstore.parse_ms_meta(stored)
    return fields["size_field"], fields["base_version"], fields["meta_format"]


def _xbox_size(stored: bytes, size_decompressed: int) -> bytes:
    out = bytearray(stored)
    struct.pack_into("<I", out, msstore.MS_OFF_SIZE, size_decompressed)
    new = bytes(out)
    before, after = msstore.parse_ms_meta(stored), msstore.parse_ms_meta(new)
    if after["size_field"] != size_decompressed or {
            k: v for k, v in after.items() if k != "size_field"} != {
            k: v for k, v in before.items() if k != "size_field"}:
        raise EditRefused("the rebuilt Xbox meta does not read back as expected")
    return new
