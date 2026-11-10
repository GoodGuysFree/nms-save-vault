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
from .liveops.faults import FaultInjector, SimulatedCrash

__all__ = ["SimulatedCrash", "crash_during", "game_closed", "obfuscated", "write_account",
           "write_steam_save"]


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


class _CrashAt(FaultInjector):
    def __init__(self, point: str, nth: int, fired: list) -> None:
        super().__init__()
        self.point, self.nth, self.fired = point, nth, fired

    def at(self, point: str) -> None:
        self.seen.append(point)
        if point == self.point and self.seen.count(point) == self.nth:
            self.fired.append(point)
            raise SimulatedCrash(point)


@contextmanager
def crash_during(session, point: str, nth: int = 1) -> Iterator[None]:
    """Inside it, a write of ``session`` (an ``api.LiveSession``) stops dead the ``nth`` time
    it reaches ``point``, as if the app or the machine went down there: it raises
    :class:`SimulatedCrash` (a ``BaseException``, so no ``except Exception`` stops it) and
    leaves what a crash leaves, for the next session to find. Points, in the order a write
    passes them: ``"after_preparing"``; ``"in_preserve"`` and ``"after_snapshot"`` (what
    the slot holds is kept in the vault); ``"after_writing"`` (the write is journaled,
    nothing is on disk yet); ``"staged:<n>"`` then ``"swapped:<n>"`` (file ``n`` of a slot,
    again for each slot of a write over several); ``"after_validated"`` (written and
    checked, not recorded); ``"after_oplog"``; ``"after_record"``. Raises AssertionError when
    the block ends and no write reached ``point`` (a misspelt one would let the write go
    through for real). Only :meth:`~api.LiveSession.write` is stopped, not ``resolve``.
    For tests only."""
    make = session._make
    fired: list = []

    def crashing():
        ctx = make()
        ctx.faults = _CrashAt(point, nth, fired)
        return ctx
    session._make = crashing
    try:
        yield
    finally:
        session._make = make
        session._ctx = None             # the next call reads everything again, without faults
    if not fired:
        raise AssertionError(f"crash_during: the write never reached {point!r} (time {nth})")


def obfuscated(node: object) -> object:
    """``node`` with every readable field name replaced by the game's key, as a save holds it
    (``{"PlayerStateData": {"Units": 5}}`` -> ``{"vLc": {"wGS": 5}}``)."""
    if isinstance(node, dict):
        return {key(k): obfuscated(v) for k, v in node.items()}
    if isinstance(node, list):
        return [obfuscated(v) for v in node]
    return node


def write_account(folder: str | Path, root: dict, *, saved_at: int = 1_790_000_000) -> Path:
    """Write a Steam account's account data into ``folder`` as the game writes it: ``root``,
    given with readable field names, stored as plain JSON text ending in one NUL, and its
    432-byte meta (storage ordinal 1) with that size and ``saved_at``. Returns the data
    file's path."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(obfuscated(root), ensure_ascii=True, separators=(",", ":")).encode(
        "ascii") + b"\x00"
    plain = bytearray(formats.META_LEN_WORLDS_II)
    for offset, value in ((formats.OFF_HEADER, formats.META_HEADER),
                          (formats.OFF_META_FORMAT, formats.META_FORMAT_3),
                          (formats.OFF_SIZE_DECOMPRESSED, len(raw)),
                          (formats.OFF_SIZE_DISK, len(raw)), (0x54, len(raw)),
                          (formats.OFF_TIMESTAMP, saved_at)):
        struct.pack_into("<I", plain, offset, value)
    data_path = folder / formats.ACCOUNT_DATA_NAME
    data_path.write_bytes(raw)
    (folder / formats.ACCOUNT_META_NAME).write_bytes(
        meta.encrypt(bytes(plain), formats.ACCOUNT_STORAGE_ORDINAL))
    return data_path


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
