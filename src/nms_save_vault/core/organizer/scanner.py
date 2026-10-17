"""The scanner (D1 2.5): one observation of every slot the app can see - each live folder
and each vault entry - and the version registry built from them.

Reads only. Every slot gives a :class:`Capture` (an empty slot too); every distinct data
file is a version, identified by its sha256. A file whose size and time are unchanged is
not hashed again, and an entry whose files are unchanged is not rescanned (the per-machine
:class:`ScanCache`). Live members are also checked for a whole, consistent save: one that
fails is shown but never journaled. Problems are collected, never raised.
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .. import catalog, discover, formats, lz4_block, msstore, playtime, safety, savedir
from ..catalog import CatalogEntry, Vault
from ..savedir import MemberView, SlotView
from ..state import Source
from .cache import ScanCache
from .model import (
    Capture,
    EntryKind,
    EntryRef,
    LiveRef,
    MemberObs,
    MetaSnap,
    ModelError,
    ObsOrder,
    PairInfo,
    PairKey,
    Pid,
    Platform,
    RawLocation,
    SourceRef,
    VersionInfo,
    Vid,
    head_of,
    legacy_pid,
    pid_from_id,
    unknown_pid,
)

log = logging.getLogger(__name__)

ProblemKind = str   # one of the kinds listed on ScanProblem

#: Entry kinds holding a whole save folder: every slot is an observation, empty ones too.
WHOLE_FOLDER_KINDS = (catalog.KIND_FULL, catalog.KIND_SNAPSHOT, catalog.KIND_INPLACE,
                      catalog.KIND_IMPORTED)
#: Entries the app made by copying a live folder at their ``created`` time.
EXACT_TIME_KINDS = (catalog.KIND_FULL, catalog.KIND_SNAPSHOT)


@dataclass(frozen=True)
class ScanProblem:
    kind: ProblemKind   # folder-missing, folder-unreadable, member-unreadable, member-unstable,
                        # entry-is-live, entry-unmapped, game-running, game-unknown
    where: SourceRef | None
    slot: int | None
    detail: str         # ASCII; folder names and ids only, redacted before display


@dataclass(frozen=True)
class LiveFolderScan:
    ref: LiveRef
    source: Source
    captures: tuple[Capture, ...]   # slots 1-15 in order; none when the folder was unreadable
    readable: bool
    writable: bool
    steam_cloud: bool
    unstable: frozenset[int]        # slots with a member that failed the check: never journaled


@dataclass(frozen=True)
class EntryScan:
    entry_id: str
    kind: str
    managed: bool
    created: int                    # UTC unix
    source: str | None              # the live folder name the entry was taken from
    captures: tuple[Capture, ...]


@dataclass(frozen=True)
class ScanResult:
    at: int
    live: Mapping[LiveRef, LiveFolderScan]
    entries: Mapping[str, EntryScan]
    captures: tuple[Capture, ...]
    versions: Mapping[Vid, VersionInfo]
    pairs: Mapping[PairKey, PairInfo]
    problems: tuple[ScanProblem, ...]
    game_running: bool | None
    timings: Mapping[str, float]


class ScanCancelled(Exception):
    pass


def scan(vault: Vault | None, sources: Sequence[Source], cache: ScanCache, *,
         game_running: bool | None, deep: bool = False,
         now: Callable[[], float] = time.time,
         progress: Callable[[int, int], None] | None = None,
         cancel: threading.Event | None = None) -> ScanResult:
    """Observe ``sources`` (the enabled live folders) and every catalog entry of ``vault``;
    with no vault only the live folders (the operation hook's quick look at one folder).
    ``deep`` hashes every file again and rescans every entry."""
    run = _Run(cache, deep, int(now()))
    if game_running is None:
        run.problem("game-unknown", None, None, "could not tell whether the game is running")
    elif game_running:
        run.problem("game-running", None, None, "the game is running: live saves are not recorded")
    entries = sorted(vault.entries if vault is not None else [],
                     key=lambda e: (_created_utc(e) or 0, e.id))
    total, done = len(entries) + len(sources), 0
    live_paths = [s.path for s in sources]

    started = time.perf_counter()
    entry_scans: dict[str, EntryScan] = {}
    for entry in entries:
        _check(cancel)
        found = run.scan_entry(entry, live_paths)
        if found is not None:
            entry_scans[entry.id] = found
        done += 1
        if progress:
            progress(done, total)
    timings = {"entries": time.perf_counter() - started}

    started = time.perf_counter()
    live: dict[LiveRef, LiveFolderScan] = {}
    for source, ref in zip(sources, _live_refs(sources)):
        _check(cancel)
        live[ref] = run.scan_live(source, ref)
        done += 1
        if progress:
            progress(done, total)
    timings["live"] = time.perf_counter() - started

    started = time.perf_counter()
    ordered = [(c, (c.observed_at, -1)) for e in entry_scans.values() for c in e.captures]
    ordered += [(c, (c.observed_at, 0)) for f in live.values() for c in f.captures]
    versions, pairs = _registry(ordered, entry_scans)
    if vault is not None:          # only a full scan knows what is gone
        cache.prune(run.seen_paths, set(entry_scans))
    cache.save()
    timings["registry"] = time.perf_counter() - started
    return ScanResult(
        at=run.at, live=live, entries=entry_scans, captures=tuple(c for c, _ in ordered),
        versions=versions, pairs=pairs, problems=tuple(run.problems),
        game_running=game_running, timings=timings,
    )


class _Run:
    """The state of one scan: problems, the paths seen and each vid's pid (fixed at its
    first registration, so a pre-Waypoint save keeps its line when it moves slot)."""

    def __init__(self, cache: ScanCache, deep: bool, at: int) -> None:
        self.cache = cache
        self.deep = deep
        self.at = at
        self.problems: list[ScanProblem] = []
        self.seen_paths: set[str] = set()
        self.pids: dict[Vid, tuple[Pid, str]] = {}

    def problem(self, kind: str, where: SourceRef | None, slot: int | None, detail: str) -> None:
        self.problems.append(ScanProblem(kind, where, slot, detail))

    # --- live folders ---------------------------------------------------------

    def scan_live(self, source: Source, ref: LiveRef) -> LiveFolderScan:
        folder = Path(source.path)
        view = self._read_folder(folder, ref)
        if view is None:
            return LiveFolderScan(ref, source, (), False, source.writable, False, frozenset())
        captures, unstable = [], set()
        for slot in range(1, formats.MAX_SAVE_SLOTS + 1):
            capture, bad = self._capture(view.slots[slot], ref, ref.platform, live=True,
                                         at=self.at, quality="exact", src="scan")
            captures.append(capture)
            if bad:
                unstable.add(slot)
        return LiveFolderScan(ref, source, tuple(captures), True, source.writable,
                              view.steam_cloud, frozenset(unstable))

    # --- vault entries --------------------------------------------------------

    def scan_entry(self, entry: CatalogEntry, live_paths: list[str]) -> EntryScan | None:
        where = EntryRef(entry.id)
        folder = Path(entry.path)
        if discover.is_live_folder(folder, live_paths):
            self.problem("entry-is-live", where, None, f"{entry.id} is a live folder, skipped")
            return None
        if entry.kind not in WHOLE_FOLDER_KINDS and entry.kind != catalog.KIND_EXTRACT:
            self.problem("entry-unmapped", where, None, f"{entry.id}: unknown kind {entry.kind!r}")
            return None
        if not folder.is_dir():
            self.problem("folder-missing", where, None, f"{entry.id}: folder {folder.name} is missing")
            return None
        try:
            fingerprint = self._fingerprint(folder)
        except OSError as exc:
            self.problem("folder-unreadable", where, None, f"{entry.id}: {exc.strerror}")
            return None
        created = _created_utc(entry)
        if created is None:
            self.problem("entry-unmapped", where, None, f"{entry.id}: no readable creation time")
        captures = None if self.deep else self.cache.entry(entry.id, fingerprint)
        if captures is None:
            captures = self._entry_captures(entry, folder, where, created or 0)
            if captures is None:
                return None
            self.cache.put_entry(entry.id, fingerprint, captures)
        else:
            for capture in captures:
                for m in capture.members:
                    self.pids.setdefault(m.vid, (m.pid, m.pid_src))
                    if m.pid_src == "legacy" and self.cache.legacy_pid(m.vid) is None:
                        self.cache.put_legacy_pid(m.vid, m.pid)
        return EntryScan(entry.id, entry.kind, entry.managed, created or 0, entry.source, captures)

    def _entry_captures(self, entry: CatalogEntry, folder: Path, where: EntryRef,
                        created: int) -> tuple[Capture, ...] | None:
        view = self._read_folder(folder, where)
        if view is None:
            return None
        platform: Platform = "xbox" if savedir.platform_of(folder) == "xbox" else "steam"
        if entry.kind in WHOLE_FOLDER_KINDS:
            slots = list(range(1, formats.MAX_SAVE_SLOTS + 1))
        else:
            slots = sorted({s.slot for s in entry.slots if s.slot in view.slots})
        at, quality = created, "exact" if entry.kind in EXACT_TIME_KINDS else "estimated"
        if entry.kind == catalog.KIND_INPLACE:
            # A folder made by hand: its newest save is the best guess of when it was taken.
            times = [m.effective_timestamp for sv in view.slots.values() for m in sv.present_members]
            at = max(times, default=created)
        return tuple(self._capture(view.slots[s], where, platform, live=False, at=at,
                                   quality=quality, src="entry")[0] for s in slots)

    def _fingerprint(self, folder: Path) -> str:
        """sha256 over ``relpath|size|mtime_ns`` of the files that make the entry's saves:
        Steam ``save*.hg`` / ``mf_save*.hg``; Xbox ``containers.index`` and the blob folders."""
        xbox = savedir.platform_of(folder) == "xbox"
        lines = []
        for path in sorted(folder.rglob("*")):
            rel = path.relative_to(folder)
            top = len(rel.parts) == 1
            if xbox:
                wanted = not top or rel.name == "containers.index"
            else:
                wanted = top and (rel.name.startswith("save") or rel.name.startswith("mf_save"))
            if not wanted or not path.is_file():
                continue
            st = path.stat()
            lines.append(f"{rel.as_posix()}|{st.st_size}|{st.st_mtime_ns}")
            self.seen_paths.add(str(path))
        return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()

    # --- slots and members ----------------------------------------------------

    def _read_folder(self, folder: Path, where: SourceRef) -> savedir.SaveDirView | None:
        name = where.entry_id if isinstance(where, EntryRef) else folder.name
        if not folder.is_dir():
            self.problem("folder-missing", where, None, f"{name} is missing")
            return None
        try:
            return savedir.scan_any(folder)
        except OSError as exc:
            self.problem("folder-unreadable", where, None, f"{name}: {exc.strerror}")
            return None

    def _capture(self, sv: SlotView, where: SourceRef, platform: Platform, *, live: bool,
                 at: int, quality: str, src: str) -> tuple[Capture, bool]:
        """One slot's capture, and whether a live member of it failed the check or could not
        be read at all - such a slot is never journaled, so a save that is there but
        unreadable is never recorded as gone."""
        members: dict[str, MemberObs] = {}
        unstable = False
        for mv in sv.members:
            obs, ok = self._member(mv, where, platform, live)
            if obs is not None:
                members[obs.member] = obs
            if live and not ok and (obs is not None or _present_but_unread(mv)):
                unstable = True
                self.problem("member-unstable", where, sv.slot,
                             f"slot {sv.slot}{mv.label}: not a whole, readable save")
        newest = sv.newest
        if newest is not None and newest.label in members:
            head = newest.label
        else:   # the loaded member could not be read: the rule on what was
            found = head_of(members.get("A"), members.get("B"))
            head = found.member if found else None
        capture = Capture(where, sv.slot, members.get("A"), members.get("B"), at,
                          quality, src, head)   # type: ignore[arg-type]
        return capture, unstable

    def _member(self, mv: MemberView, where: SourceRef, platform: Platform,
                live: bool) -> tuple[MemberObs | None, bool]:
        if not mv.exists:
            if mv.note:
                self.problem("member-unreadable", where, mv.slot, f"slot {mv.slot}{mv.label}: {mv.note}")
            return None, False
        path = mv.data_path
        try:
            st = path.stat()
            hit = None if self.deep else self.cache.file(path, st.st_size, st.st_mtime_ns)
            data = None
            # The verdict ties the data to its meta: a new meta beside the same data (moved,
            # promoted, edited) is checked again.
            if hit is None or (live and (hit.whole is None or hit.meta_sha != mv.meta_sha256)):
                data = path.read_bytes()
                vid, ok = safety.sha256_bytes(data), (_whole(mv, data, platform) if live else None)
                self.cache.put_file(path, st.st_size, st.st_mtime_ns, vid, ok, mv.meta_sha256)
            else:
                vid, ok = hit.vid, hit.whole
        except OSError as exc:
            self.problem("member-unreadable", where, mv.slot, f"slot {mv.slot}{mv.label}: {exc.strerror}")
            return None, False
        self.seen_paths.add(str(path))
        if not mv.valid:
            self.problem("member-unreadable", where, mv.slot,
                         f"slot {mv.slot}{mv.label}: meta unreadable{': ' + mv.note if mv.note else ''}")
        if vid not in self.pids:
            self.pids[vid] = self._pid(mv, vid, platform, data)
        pid, pid_src = self.pids[vid]
        sync = getattr(mv.xbox, "sync_state", None) if live and platform == "xbox" else None
        return MemberObs(mv.label, vid, pid, pid_src, platform, _snap(mv, platform),  # type: ignore[arg-type]
                         sync), bool(ok)

    def _pid(self, mv: MemberView, vid: Vid, platform: Platform,
             data: bytes | None) -> tuple[Pid, str]:
        """D1 2.2: the meta's id; else the id in the data (looked up once per vid); else the
        legacy key by platform, slot and name; else unknown."""
        info = mv.info
        if mv.valid and info is not None and info.slot_identifier:
            return pid_from_id(info.slot_identifier), "meta"
        ident = self.cache.data_id(vid)
        if ident is None:
            ident = _data_id(mv, vid, data)
            if ident is not None:       # "could not look" is not "has none": ask again later
                self.cache.put_data_id(vid, ident)
        if ident:
            try:
                return pid_from_id(ident), "data"
            except ModelError:
                pass
        if mv.valid and info is not None:
            pid = self.cache.legacy_pid(vid) or legacy_pid(platform, mv.slot, info.save_name)
            self.cache.put_legacy_pid(vid, pid)
            return pid, "legacy"
        return unknown_pid(vid), "unknown"


def _snap(mv: MemberView, platform: Platform) -> MetaSnap:
    info = mv.info
    if mv.timestamp:
        ts_src = "meta"
    else:
        ts_src = "index" if getattr(mv.xbox, "last_write", 0) else "mtime"
    return MetaSnap(
        play_time=info.total_play_time if info else 0, meta_ts=mv.effective_timestamp,
        ts_src=ts_src, name=info.save_name if info else "", summary=info.save_summary if info else "",
        difficulty=info.difficulty if info else 0, game_mode=info.game_mode if info else 0,
        season=info.season if info else 0, base_version=info.base_version if info else 0,
        meta_format=info.meta_format if info else 0,
        size_decompressed=info.size_decompressed if info else 0, data_size=mv.data_size,
        meta_sha=mv.meta_sha256, valid=mv.valid, file_time=mv.data_mtime,
    )


def _whole(mv: MemberView, data: bytes, platform: Platform) -> bool:
    """A live save is recorded only when it is whole: Steam's chunks add up to the meta's
    SizeDecompressed (the reliable tie, DESIGN.md); an Xbox stream walks to its end."""
    if not mv.valid or mv.info is None:
        return False
    try:
        st = lz4_block.stats(data)
    except ValueError:
        return False
    if platform == "steam":
        return st.chunk_count > 0 and st.total_decompressed == mv.info.size_decompressed
    if data[:4] == formats.SAVE_STREAMING_HEADER:
        return st.chunk_count > 0 and st.trailing == 0
    return True     # a pre-Waypoint bare LZ4 block has no chunks to walk


def _data_id(mv: MemberView, vid: Vid, data: bytes | None) -> int | None:
    """The playthrough id inside the save data (first chunk only), 0 when it has none, None
    when the data could not be looked into."""
    try:
        raw = data if data is not None else mv.data_path.read_bytes()
        expected = mv.info.size_decompressed if mv.info else 0
        return playtime._id_from_bytes(raw, expected) or 0
    except lz4_block.UnsupportedContainer as exc:
        log.warning("version %s: could not look for its id in the data: %s", vid[:8], exc)
    except OSError as exc:
        log.warning("version %s: could not read its data for the id: %s", vid[:8], exc.strerror)
    return None


def _present_but_unread(mv: MemberView) -> bool:
    """A member that is there but could not be taken in: half of a Steam pair, or an Xbox
    record whose blob is missing (a record marked deleted is a save that is gone)."""
    if mv.exists:
        return True
    if mv.xbox is not None:
        return getattr(mv.xbox, "sync_state", None) != msstore.SYNC_STATE_DELETED
    return mv.data_path.is_file() or mv.meta_path.is_file()


def _created_utc(entry: CatalogEntry) -> int | None:
    """Catalog v1 stores local naive ISO times; this machine's zone turns them into UTC."""
    try:
        return int(datetime.fromisoformat(entry.created).astimezone(timezone.utc).timestamp())
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _live_refs(sources: Sequence[Source]) -> list[LiveRef]:
    """One LiveRef per source: its folder name, plus an instance tag only when two live
    folders of one platform share that name here (two Proton prefixes)."""
    def platform_of(s: Source) -> Platform:
        return "xbox" if s.platform == "xbox" else "steam"

    names = [(platform_of(s), Path(s.path).name) for s in sources]
    refs = []
    for source, key in zip(sources, names):
        instance = ""
        if names.count(key) > 1:
            parent = str(Path(source.path).resolve().parent)
            instance = hashlib.sha1(parent.encode("utf-8")).hexdigest()[:8]
        refs.append(LiveRef(key[0], key[1], instance))
    return refs


def _registry(ordered: list[tuple[Capture, ObsOrder]], entries: Mapping[str, EntryScan]
              ) -> tuple[dict[Vid, VersionInfo], dict[PairKey, PairInfo]]:
    """One VersionInfo per vid (its earliest observation gives the meta and pid; later ones
    add locations and roles) and one PairInfo per distinct non-empty pair."""
    kinds = {eid: e.kind for eid, e in entries.items()}
    managed = {eid: e.managed for eid, e in entries.items()}
    builders: dict[Vid, dict] = {}
    pair_builders: dict[PairKey, dict] = {}
    for capture, first in sorted(ordered, key=lambda item: item[1]):
        where = capture.where
        if isinstance(where, EntryRef):
            kind: EntryKind = kinds[where.entry_id]  # type: ignore[assignment]
            is_managed = managed[where.entry_id]
        else:
            kind, is_managed = "live", False
        for m in capture.members:
            location = RawLocation(where, capture.slot, m.member, kind, is_managed,
                                   capture.observed_at)
            b = builders.get(m.vid)
            if b is None:
                builders[m.vid] = {"obs": m, "first": first, "roles": {m.member},
                                   "ts_last": m.meta.meta_ts, "locations": [location]}
            else:
                b["roles"].add(m.member)
                b["ts_last"] = max(b["ts_last"], m.meta.meta_ts)
                b["locations"].append(location)
        head = capture.head
        if head is None:
            continue
        pb = pair_builders.get(capture.pair)
        if pb is None:
            partner = capture.b if head.member == "A" else capture.a
            pair_builders[capture.pair] = {"head": head, "partner": partner, "first": first,
                                           "seen": [(where, capture.slot)]}
        else:
            pb["seen"].append((where, capture.slot))
    versions = {
        vid: VersionInfo(vid, b["obs"].pid, b["obs"].pid_src, b["obs"].platform, b["obs"].meta,
                         b["ts_last"], frozenset(b["roles"]), b["first"], tuple(b["locations"]))
        for vid, b in builders.items()
    }
    pairs = {
        pair: PairInfo(pair, pb["head"].vid, pb["head"].member,
                       pb["partner"].vid if pb["partner"] else None,
                       versions[pb["head"].vid].raw_pid, pb["first"], tuple(pb["seen"]))
        for pair, pb in pair_builders.items()
    }
    return versions, pairs


def _check(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise ScanCancelled()
