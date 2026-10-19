"""Total play time across every save the app can see, counted once per playthrough.

A playthrough is neither a file nor a slot. The game keeps it as two saves (an auto-save
and a restore point); the vault keeps it again in every dated backup; and because NMS
cloud saves carry a save between linked platforms, the same run also exists under both a
Steam and an Xbox account. Counting files would multiply one playthrough by all of that,
so every copy is folded onto one identity and the highest play time wins -- play time only
ever increases, so the newest copy is simply the largest.

Identity is the game's own: the u64 the meta carries (Steam ``OFF_SLOT_IDENTIFIER``, Xbox
``MS_OFF_SLOT_ID``), which is the same number the save data holds as ``<h0``/``WmU``. It
survives renames, slot moves and the trip through the cloud to another platform. Only when
the meta has no id (older Xbox metas are too short to hold one) is the data blob decoded
for it -- that costs a decompress, which is why the answer is cached per file. Saves older
than the field (pre-Waypoint) have no identity at all and fall back to platform+slot+name,
which cannot tell two same-named runs apart; :attr:`PlaytimeReport.unidentified` says how
many of those there were.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import formats, lz4_block, savedir
from .catalog import Vault

# The playthrough id inside the (obfuscated) save JSON. Both forms are hunted in the raw
# bytes rather than by parsing: the file is up to ~18 MB of JSON and the field sits in the
# first chunk, so a search costs nothing next to json.loads().
ID_IN_DATA = re.compile(rb'"(?:WmU|ActiveSaveSlotId)"\s*:\s*"(0[xX][0-9A-Fa-f]{1,16})"')

log = logging.getLogger(__name__)

CACHE_NAME = "playthrough-ids.json"
# Version 1 was a bare {key: id} mapping, written by a decoder that took an HGSAVEV2 blob
# for "no id" (0): its entries are dropped rather than trusted.
CACHE_VERSION = 2
UNSUPPORTED = -1      # cached for a blob in a container this version cannot read


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
    # Every (folder, slot) holding a copy, so a backup list can be narrowed to one run.
    locations: set[tuple[str, int]] = field(default_factory=set)
    # Every name the run has carried, mapped to when it last carried it.
    name_seen: dict[str, int] = field(default_factory=dict)

    @property
    def cross_platform(self) -> bool:
        return len(self.platforms) > 1

    @property
    def backups(self) -> int:
        return sum(1 for s in self.sources if s != "live")

    @property
    def former_names(self) -> list[str]:
        """Names other than the current one, most recently used first."""
        ordered = sorted(self.name_seen, key=lambda n: -self.name_seen[n])
        return [n for n in ordered if n != self.name]


@dataclass
class PlaytimeReport:
    playthroughs: list[Playthrough]
    folders: int = 0
    saves: int = 0
    # Saves whose data container this version cannot read (Xbox HGSAVEV2): their id could
    # not be looked for, so they were counted by name.
    unsupported: int = 0

    @property
    def total_play_time(self) -> int:
        return sum(p.play_time for p in self.playthroughs)

    @property
    def unidentified(self) -> int:
        """Playthroughs counted by name because the save predates the id field."""
        return sum(1 for p in self.playthroughs if not p.identified)

    def find(self, folder, slot: int) -> Playthrough | None:
        """The playthrough in ``slot`` of ``folder``, or None if that slot was not seen."""
        where = (str(Path(folder)), slot)
        return next((p for p in self.playthroughs if where in p.locations), None)


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
            head = lz4_block.decode_chunk(raw, chunks[0])
        else:
            head = lz4_block.decompress(raw, expected_size)
    except lz4_block.UnsupportedContainer:
        raise                     # not "no id": the caller must say it could not look
    except (ValueError, IndexError):
        return None
    m = ID_IN_DATA.search(head)
    return int(m.group(1), 16) if m else None


def _id_from_data(data_path: Path, expected_size: int) -> int | None:
    try:
        return _id_from_bytes(data_path.read_bytes(), expected_size)
    except OSError:
        return None


class IdCache:
    """Remembers a file's playthrough id across runs, keyed by identity-on-disk.

    Decoding a blob is the only expensive part of the whole report, and a save file that
    has not changed cannot have changed its id, so path+size+mtime is a sound key. A
    missing, unreadable or older-format cache is not an error -- it just costs a decode. A
    blob in a container this version cannot read is remembered as such, and raises
    :class:`lz4_block.UnsupportedContainer` from the cache as it did when decoded.
    """

    def __init__(self, path: Path):
        self.path = path
        self._map: dict[str, int] = {}
        self._dirty = False
        try:
            if path.is_file():
                doc = json.loads(path.read_text("utf-8"))
                if doc.get("version") == CACHE_VERSION:
                    self._map = {k: int(v) for k, v in doc["ids"].items()}
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            self._map = {}

    @staticmethod
    def _key(p: Path, size: int, mtime: float) -> str:
        return f"{p}|{size}|{int(mtime)}"

    def get_or_read(self, data_path: Path, size: int, mtime: float, expected_size: int) -> int | None:
        k = self._key(data_path, size, mtime)
        if k in self._map:
            v = self._map[k]
            if v == UNSUPPORTED:
                raise lz4_block.UnsupportedContainer(lz4_block.HGSAVEV2_UNSUPPORTED)
            return v or None
        try:
            found = _id_from_data(data_path, expected_size)
        except lz4_block.UnsupportedContainer:
            self._map[k] = UNSUPPORTED
            self._dirty = True
            raise
        self._map[k] = found or 0
        self._dirty = True
        return found

    def remember(self, key: str, compute) -> int:
        """``compute()``'s id for a source with no file to key on (a stored blob), once."""
        if key in self._map:
            if self._map[key] == UNSUPPORTED:
                raise lz4_block.UnsupportedContainer(lz4_block.HGSAVEV2_UNSUPPORTED)
            return self._map[key]
        try:
            found = compute()
        except lz4_block.UnsupportedContainer:
            self._map[key] = UNSUPPORTED
            self._dirty = True
            raise
        self._map[key] = found
        self._dirty = True
        return found

    def flush(self) -> None:
        if not self._dirty:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps({"version": CACHE_VERSION, "ids": self._map}), "utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass  # a cache that cannot be written only costs time next run


def name_key(member, platform: str) -> str:
    """The fallback key of a save without a playthrough id: platform, slot and name."""
    name = (member.info.save_name if member.info else "") or "<unnamed>"
    return f"{platform}:{member.slot}:{name}"


def identity(member, platform: str, cache: IdCache | None = None) -> tuple[str, bool]:
    """(key, identified) for one save: the game's id where there is one, else a fallback.

    The meta hands the id over for free. A meta without one (0) falls back to decoding the
    blob -- through the cache when there is one, directly when the caller turned caching
    off. Disabling the cache must cost time, not identity. A blob in a container this
    version cannot read raises :class:`lz4_block.UnsupportedContainer`.
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
    return name_key(member, platform), False


def entry_location(entry_id: str, path: str | Path = "") -> str:
    """The ``locations`` key of a vault entry: its folder, or - for a store entry, which has
    none - a name made from its id."""
    return str(Path(path)) if path else f"store:{entry_id}"


class _Folding:
    """The playthroughs found so far, one per identity."""

    def __init__(self) -> None:
        self.found: dict[str, Playthrough] = {}
        self.saves = 0

    def add(self, key: str, identified: bool, *, source: str, where: str, slot: int,
            platform: str, name: str, difficulty: str, play_time: int, when: int) -> None:
        self.saves += 1
        p = self.found.get(key)
        if p is None:
            p = self.found[key] = Playthrough(
                key=key, identified=identified, name=name or "<unnamed>", difficulty=difficulty)
        p.copies += 1
        if platform not in p.platforms:
            p.platforms.append(platform)
        if source not in p.sources:
            p.sources.append(source)
        p.locations.add((where, slot))
        if name:
            p.name_seen[name] = max(p.name_seen.get(name, 0), when)
        if play_time >= p.play_time:
            p.play_time = play_time
        if when > p.newest:
            p.newest = when
            # The newest copy owns the display name: a save that was renamed is still one
            # playthrough, and the current name is the useful one.
            p.name = name or p.name
            p.difficulty = difficulty or p.difficulty


def _store_of(vault: Vault, app_state):
    """The vault's version store, or None (old layout, or one that cannot be opened: the
    report then covers the catalog only, and the log says why)."""
    from .state import AppState
    from .store import classic as store_classic

    try:
        return store_classic.store_of(
            vault, app_state=app_state if app_state is not None else AppState(sources=[]),
            start_new=False)
    except Exception:  # noqa: BLE001 - one unreadable store must not sink the report
        log.exception("the play time report could not open the version store (vault %s)",
                      vault.root.name)
        return None


def _store_name_key(entry, r) -> str:
    return f"{entry.platform}:{r.slot}:{r.name or '<unnamed>'}"


def _fold_store(store, folding: _Folding, cache: IdCache | None) -> tuple[int, int]:
    """Fold every member of every store entry; returns (entries scanned, undecodable blobs)."""
    from .store.listing import has_info

    scanned = unsupported = 0
    for entry in store.entries():
        scanned += 1
        for r in entry.members:
            if not has_info(entry, r):
                continue                       # the meta could not be read: nothing to count
            if r.pid:
                key, identified = r.pid, True
            else:
                try:
                    key, identified = _store_blob_identity(store, entry, r, cache)
                except lz4_block.UnsupportedContainer:
                    unsupported += 1
                    key, identified = _store_name_key(entry, r), False
            folding.add(
                key, identified, source=entry.id, where=entry_location(entry.id), slot=r.slot,
                platform=entry.platform, name=r.name,
                difficulty=formats.difficulty_label(r.difficulty, r.game_mode),
                play_time=r.play_time, when=r.meta_ts or int(r.file_time_ns / 1_000_000_000))
    return scanned, unsupported


def _store_blob_identity(store, entry, r, cache: IdCache | None) -> tuple[str, bool]:
    """A stored save whose meta has no id: the id in its data (read from the store once, and
    remembered), else the name key. Raises :class:`lz4_block.UnsupportedContainer` for a
    container this version cannot read."""
    from .store.errors import StoreError

    def read() -> int:
        try:
            return _id_from_bytes(store.read_file(entry.id, r.data), r.size_decompressed) or 0
        except (StoreError, OSError, ValueError):
            return 0

    ident = cache.remember(f"store|{r.vid}", read) if cache is not None else read()
    if ident:
        return f"{ident:016X}", True
    return _store_name_key(entry, r), False


def collect(vault: Vault, live_dirs, use_cache: bool = True, app_state=None) -> PlaytimeReport:
    """Fold every save in ``live_dirs`` and in the vault onto one row per playthrough: the
    catalog's folders and the version store's entries alike (``app_state``: the configured
    live folders, for opening the store; none are needed to read it)."""
    cache = IdCache(vault.root / CACHE_NAME) if use_cache else None
    store = _store_of(vault, app_state)
    stored = {e.id for e in store.entries()} if store is not None else set()
    folders: list[tuple[str, Path]] = [("live", Path(d)) for d in live_dirs]
    folders += [(e.id, Path(e.path)) for e in vault.entries if e.id not in stored]

    folding = _Folding()
    scanned = unsupported = 0
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
                try:
                    key, identified = identity(m, platform, cache)
                except lz4_block.UnsupportedContainer:
                    unsupported += 1
                    key, identified = name_key(m, platform), False
                folding.add(
                    key, identified, source=source, where=str(folder), slot=slot.slot,
                    platform=platform, name=m.info.save_name,
                    difficulty=m.info.difficulty_label, play_time=m.info.total_play_time,
                    when=m.effective_timestamp)
    if store is not None:
        more, blobs = _fold_store(store, folding, cache)
        scanned += more
        unsupported += blobs

    if cache is not None:
        cache.flush()
    ordered = sorted(folding.found.values(), key=lambda p: (-p.play_time, p.name.lower()))
    return PlaytimeReport(playthroughs=ordered, folders=scanned, saves=folding.saves,
                          unsupported=unsupported)
