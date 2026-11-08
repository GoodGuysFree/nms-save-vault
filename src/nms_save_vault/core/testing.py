"""Saves made up for tests, for an app that bundles the core (NMS Savecraft): its tests build
saves from readable field names without knowing the save format, as the core's own do. For
temporary test folders only, never a real save folder.

Pinned with the public API (``tests/test_core_testing.py``): an app's tests may import this
module and :mod:`.api`, nothing else of the core.
"""
from __future__ import annotations

import json
import struct
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from . import formats, gameversion, lz4_block, meta, slotmap
from .hashname import key

__all__ = ["game_closed", "obfuscated", "write_steam_save"]


@contextmanager
def game_closed() -> Iterator[None]:
    """Inside it, the core takes No Man's Sky for closed (``api.game_running()`` is False and
    a write is not refused for it), so an app's write tests never depend on whether the game
    runs on the machine that tests."""
    from . import safety

    saved = safety.is_game_running
    safety.is_game_running = lambda *args, **kwargs: False
    try:
        yield
    finally:
        safety.is_game_running = saved


def obfuscated(node: object) -> object:
    """``node`` with every readable field name replaced by the game's key, as a save holds it
    (``{"PlayerStateData": {"Units": 5}}`` -> ``{"vLc": {"wGS": 5}}``)."""
    if isinstance(node, dict):
        return {key(k): obfuscated(v) for k, v in node.items()}
    if isinstance(node, list):
        return [obfuscated(v) for v in node]
    return node


def write_steam_save(folder: str | Path, slot: int, root: dict, *, member: int = 0,
                     name: str = "", play_time: int = 0, playthrough: int = 0x1234,
                     saved_at: int = 1_790_000_000,
                     base_version: int = gameversion.KNOWN["steam"].base_version) -> Path:
    """Write save ``member`` (0: A, 1: B) of Steam slot ``slot`` into ``folder``: ``root``,
    given with readable field names, stored as the game stores it (compressed, ending in one
    NUL), with a 432-byte encrypted meta whose sizes, name, play time, playthrough id, save
    time and game version are set. Returns the data file's path."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    # a hashed technology id is held as lone surrogates: its bytes go back as they were
    raw = json.dumps(obfuscated(root), ensure_ascii=False,
                     separators=(",", ":")).encode("utf-8", "surrogateescape") + b"\x00"
    data = lz4_block.compress(raw)
    file_no = slotmap.slot_file_numbers(slot)[member]
    plain = bytearray(formats.META_LEN_WORLDS_II)
    for offset, fmt, value in (
            (formats.OFF_HEADER, "<I", formats.META_HEADER),
            (formats.OFF_META_FORMAT, "<I", formats.META_FORMAT_4),
            (formats.OFF_META_FORMAT_TAIL, "<I", formats.META_FORMAT_4),
            (formats.OFF_BASE_VERSION, "<I", base_version),
            (formats.OFF_SIZE_DECOMPRESSED, "<I", len(raw)),
            (formats.OFF_SIZE_DISK, "<I", len(data)),
            (0x54, "<I", len(raw)),                     # the game's second copy of the size
            (formats.OFF_GAME_MODE, "<H", 1),
            (formats.OFF_TOTAL_PLAY_TIME, "<Q", play_time),
            (formats.OFF_DIFFICULTY, "<I", 2),
            (formats.OFF_SLOT_IDENTIFIER, "<Q", playthrough),
            (formats.OFF_TIMESTAMP, "<I", saved_at)):
        struct.pack_into(fmt, plain, offset, value)
    encoded = name.encode("utf-8")
    if len(encoded) >= formats.NAME_FIELD_LENGTH:
        raise ValueError("a save name must be under 128 bytes")
    plain[formats.OFF_SAVE_NAME:formats.OFF_SAVE_NAME + len(encoded)] = encoded
    data_path = folder / slotmap.data_filename(file_no)
    data_path.write_bytes(data)
    (folder / slotmap.meta_filename(file_no)).write_bytes(
        meta.encrypt(bytes(plain), slotmap.storage_ordinal(file_no)))
    return data_path
