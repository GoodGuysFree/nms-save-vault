"""Total play time across every save the app can see, counted once per playthrough.

A playthrough is neither a file nor a slot. The game keeps it as two saves (an auto-save
and a restore point); the vault keeps it again in every dated backup; and because NMS
cloud saves carry a save between linked platforms, the same run also exists under both a
Steam and an Xbox account. Counting files would multiply one playthrough by all of that,
so every copy is folded onto one identity and the highest play time wins -- play time only
ever increases, so the newest copy is simply the largest.

Identity is the game's own: the u64 the Steam meta carries at ``OFF_SLOT_IDENTIFIER``,
which is the same number the save data holds as ``<h0``/``WmU``. It survives renames, slot
moves and the trip through the cloud to another platform. Xbox metas leave that field 0,
so an Xbox save is identified from its data blob instead -- that costs a decompress, which
is why the answer is cached per file. Saves older than the field (pre-Waypoint) have no
identity at all and fall back to platform+slot+name, which cannot tell two same-named runs
apart; :attr:`PlaytimeReport.unidentified` says how many of those there were.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import lz4_block, savedir
from .catalog import Vault

# The playthrough id inside the (obfuscated) save JSON. Both forms are hunted in the raw
# bytes rather than by parsing: the file is up to ~18 MB of JSON and the field sits in the
# first chunk, so a search costs nothing next to json.loads().
ID_IN_DATA = re.compile(rb'"(?:WmU|ActiveSaveSlotId)"\s*:\s*"(0[xX][0-9A-Fa-f]{1,16})"')

CACHE_NAME = "playthrough-ids.json"


@dataclass
class Playthrough:
    """One run of the game, however many copies of it exist."""

    key: str
    identified: bool
    name: str
    difficulty: str
    play_time: int = 0
    newest: int = 0
    copies: int = 0
    platforms: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)

    @property
    def cross_platform(self) -> bool:
        return len(self.platforms) > 1


@dataclass
class PlaytimeReport:
    playthroughs: list[Playthrough]
    folders: int = 0
    saves: int = 0

    @property
    def total_play_time(self) -> int:
        return sum(p.play_time for p in self.playthroughs)

    @property
    def unidentified(self) -> int:
        """Playthroughs counted by name because the save predates the id field."""
        return sum(1 for p in self.playthroughs if not p.identified)


def _platform(folder: Path) -> str:
    return savedir.platform_of(folder) or "unknown"


def _id_from_bytes(raw: bytes, expected_size: int) -> int | None:
    """The playthrough id inside a save data container, or None if it has none.

    Only the first chunk is decoded for a framed container: the field lives ~30-70 KB into
    the JSON, well inside the first 512 KB. An old unframed save has no chunks to stop at,
    so it is decoded whole -- they are small by today's standards and rare.
    """
    try:
        chunks, _consumed = lz4_block.walk_chunks(raw)
        if chunks:
            c = chunks[0]
            head = lz4_block.decompress_block(raw[c.payload_offset : c.payload_offset + c.compressed_size], c.decompressed_size)
        else:
            head = lz4_block.decompress(raw, expected_size)
    except (ValueError, IndexError):
        return None
    m = ID_IN_DATA.search(head)
    return int(m.group(1), 16) if m else None


def _id_from_data(data_path: Path, expected_size: int) -> int | None:
    try:
        return _id_from_bytes(data_path.read_bytes(), expected_size)
    except OSError:
        return None


class _IdCache:
    """Remembers a file's playthrough id across runs, keyed by identity-on-disk.

    Decoding a blob is the only expensive part of the whole report, and a save file that
    has not changed cannot have changed its id, so path+size+mtime is a sound key. A
    missing or unreadable cache is not an error -- it just costs a decode.
    """

    def __init__(self, path: Path):
        self.path = path
        self._map: dict[str, int] = {}
        self._dirty = False
        try:
            if path.is_file():
                self._map = {k: int(v) for k, v in json.loads(path.read_text("utf-8")).items()}
        except (OSError, ValueError):
            self._map = {}

    @staticmethod
    def _key(p: Path, size: int, mtime: float) -> str:
        return f"{p}|{size}|{int(mtime)}"

    def get_or_read(self, data_path: Path, size: int, mtime: float, expected_size: int) -> int | None:
        k = self._key(data_path, size, mtime)
        if k in self._map:
            v = self._map[k]
            return v or None
        found = _id_from_data(data_path, expected_size)
        self._map[k] = found or 0
        self._dirty = True
        return found

    def flush(self) -> None:
        if not self._dirty:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(self._map), "utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass  # a cache that cannot be written only costs time next run


def _identity(member, platform: str, cache: _IdCache | None) -> tuple[str, bool]:
    """(key, identified) for one save: the game's id where there is one, else a fallback.

    The Steam meta hands the id over for free. An Xbox meta does not (it leaves the field
    0), so the blob is decoded -- through the cache when there is one, directly when the
    caller turned caching off. Disabling the cache must cost time, not identity.
    """
    info = member.info
    ident = getattr(info, "slot_identifier", 0) or 0
    if not ident and member.data_path and member.data_size:
        data_path = Path(member.data_path)
        expected = getattr(info, "size_decompressed", 0) or 0
        if cache is not None:
            ident = cache.get_or_read(data_path, member.data_size, member.data_mtime, expected) or 0
        else:
            ident = _id_from_data(data_path, expected) or 0
    if ident:
        return f"{ident:016X}", True
    name = (info.save_name if info else "") or "<unnamed>"
    return f"{platform}:{member.slot}:{name}", False


def collect(vault: Vault, live_dirs, use_cache: bool = True) -> PlaytimeReport:
    """Fold every save in ``live_dirs`` and in the vault onto one row per playthrough."""
    cache = _IdCache(vault.root / CACHE_NAME) if use_cache else None
    folders: list[tuple[str, Path]] = [("live", Path(d)) for d in live_dirs]
    folders += [(e.id, Path(e.path)) for e in vault.entries]

    found: dict[str, Playthrough] = {}
    scanned = saves = 0
    for source, folder in folders:
        if not folder.is_dir():
            continue
        try:
            view = savedir.scan_any(folder)
        except Exception:  # noqa: BLE001 -- one unreadable folder must not sink the report
            continue
        scanned += 1
        platform = _platform(folder)
        for slot in view.occupied_slots:
            for m in slot.present_members:
                if m.info is None:
                    continue
                saves += 1
                key, identified = _identity(m, platform, cache)
                p = found.get(key)
                if p is None:
                    p = found[key] = Playthrough(
                        key=key, identified=identified,
                        name=m.info.save_name or "<unnamed>",
                        difficulty=m.info.difficulty_label,
                    )
                p.copies += 1
                if platform not in p.platforms:
                    p.platforms.append(platform)
                if source not in p.sources:
                    p.sources.append(source)
                if m.info.total_play_time >= p.play_time:
                    p.play_time = m.info.total_play_time
                if m.effective_timestamp > p.newest:
                    p.newest = m.effective_timestamp
                    # The newest copy owns the display name: a save that was renamed is
                    # still one playthrough, and the current name is the useful one.
                    p.name = m.info.save_name or p.name
                    p.difficulty = m.info.difficulty_label or p.difficulty

    if cache is not None:
        cache.flush()
    ordered = sorted(found.values(), key=lambda p: (-p.play_time, p.name.lower()))
    return PlaytimeReport(playthroughs=ordered, folders=scanned, saves=saves)
