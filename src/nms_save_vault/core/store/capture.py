"""Building store entries from folders (D3 2.6.1, 2.6.2): which files a capture takes, reading
them stably, and describing the slot members from what was read.

Captures only read their source. Member records come from the same scanners the rest of the
app uses (``savedir.scan_any``), run after the bytes were read; then every file read must be
unchanged and a wgs index must still be the one read first, so the files and the records are
one state of the folder and a store entry describes its slots exactly as the classic catalog
describes a folder.
"""
from __future__ import annotations

import logging
import os
import struct
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

from .. import lz4_block, msstore, savedir
from ..formats import ACCOUNT_DATA_NAME, ACCOUNT_META_NAME, CACHE_DIR, MAX_SAVE_SLOTS, STEAM_AUTOCLOUD
from ..meta import MetaInfo
from ..organizer.model import Platform, pid_from_id
from ..slotmap import data_filename, meta_filename, parse_data_filename, slot_file_numbers
from .errors import SourceChanged, StoreError
from .manifest import FileEntry, MemberRecord
from .objects import sha256

log = logging.getLogger(__name__)

STABLE_TRIES = 3
INDEX_NAME = "containers.index"
STEAM_ACCOUNT_FILES = {"data": ACCOUNT_DATA_NAME, "meta": ACCOUNT_META_NAME,
                       "autocloud": STEAM_AUTOCLOUD}


@dataclass(frozen=True)
class CaptureScope:
    slots: tuple[int, ...] | None = None    # None = the whole folder
    account_files: bool = True
    cache: Literal["include", "omit"] = "omit"


@dataclass
class CapturePlan:
    """What one capture reads: source files by their entry path, files the capture builds
    itself (the rebuilt Xbox index), and the manifest records that follow from the scope."""

    folder: Path
    platform: Platform
    files: list[tuple[str, Path]]
    sizes: dict[str, int]
    scope: dict[str, object]
    slots: tuple[int, ...] | None
    xbox: dict[str, str] | None = None
    made: dict[str, tuple[bytes, int]] = field(default_factory=dict)       # path -> (bytes, mtime_ns)
    account: dict[str, str] = field(default_factory=dict)   # account-level files by role
    index_sha: str | None = None    # wgs: the containers.index the plan was made from

    def object_bytes(self, inline_max: int) -> int:
        """The most the capture can add to the store: every file too big to be inline."""
        sizes = list(self.sizes.values()) + [len(b) for b, _ in self.made.values()]
        return sum(s for s in sizes if s > inline_max)

    def require_consistent(self, records: Mapping[str, FileEntry]) -> None:
        """Run after the members were described (the folder scanned): every file read is still
        what was read, and a wgs index is still the one the plan (and a verbatim capture of
        it) holds, so the files and the member records describe one state of the folder."""
        for rel, path in self.files:
            require_unchanged(path, rel, records[rel].size, records[rel].mtime_ns)
        if self.index_sha is None:
            return
        try:
            now = sha256(_read_file(self.folder / INDEX_NAME))
        except FileNotFoundError as exc:
            raise SourceChanged(INDEX_NAME, vanished=True) from exc
        captured = records.get(INDEX_NAME)
        verbatim = self.xbox is not None and self.xbox.get("index_kind") == "verbatim"
        if now != self.index_sha or (verbatim and (captured is None
                                                   or captured.sha256 != self.index_sha)):
            raise SourceChanged(INDEX_NAME)


def plan(folder: Path, platform: Platform, *, kind: str, scope: CaptureScope,
         live: bool) -> CapturePlan:
    if scope.slots is not None:
        bad = [s for s in scope.slots if not 1 <= s <= MAX_SAVE_SLOTS]
        if bad or not scope.slots:
            raise StoreError(f"Not a slot: {bad or 'none given'}.")
    if platform == "steam":
        return _plan_steam(folder, kind=kind, scope=scope, live=live)
    return _plan_xbox(folder, scope=scope)


def _plan_steam(folder: Path, *, kind: str, scope: CaptureScope, live: bool) -> CapturePlan:
    account_names = set(STEAM_ACCOUNT_FILES.values())
    if scope.slots is not None:
        # Every save file of the slot, a data file without its meta (or the reverse) too:
        # it is kept as a file with a note (D3 2.6.1), never dropped.
        rels = [name for slot in scope.slots for fno in slot_file_numbers(slot)
                for name in (data_filename(fno), meta_filename(fno))
                if _is_regular(folder / name)]
        if not rels:
            raise StoreError(f"Slot {_slots_text(scope.slots)} holds no saves to keep.")
        record = {"slots": list(scope.slots), "account_files": False, "cache": "none"}
    elif live and kind == "snapshot":
        # The save state only, as the classic snapshot (``operations._save_state_files``).
        rels = sorted(p.name for p in folder.iterdir() if _is_regular(p)
                      and (p.name.lower().endswith(".hg") or p.name == STEAM_AUTOCLOUD))
        if not scope.account_files:
            rels = [r for r in rels if r not in account_names]
        record = {"slots": "all", "account_files": scope.account_files, "cache": "none"}
    else:
        rels = walk(folder)
        if scope.cache == "omit":
            rels = [r for r in rels if not r.startswith(CACHE_DIR + "/")]
        if not scope.account_files:
            rels = [r for r in rels if r not in account_names]
        record = {"slots": "all", "account_files": scope.account_files,
                  "cache": "included" if scope.cache == "include" else "omitted"}
    files = [(rel, folder.joinpath(*rel.split("/"))) for rel in sorted(rels)]
    account = {k: name for k, name in STEAM_ACCOUNT_FILES.items() if name in rels}
    return CapturePlan(folder, "steam", files, _sizes(files), record, scope.slots,
                       account=account)


def _plan_xbox(folder: Path, *, scope: CaptureScope) -> CapturePlan:
    """A wgs account folder (``containers.index`` + one folder per save), or an xgs account
    folder (one folder per identifier, no index). The whole folder is taken verbatim; a slot
    scope takes the slot's folders and, for wgs, an index rebuilt from just their records (as
    the classic extract does), so the entry is a self-contained wgs folder."""
    index = folder / INDEX_NAME
    wgs = _is_regular(index)
    index_sha = None
    if wgs:     # read first: the end of the capture checks the folder still has this index
        index_bytes, index_stat = read_stable(index, INDEX_NAME)
        index_sha = sha256(index_bytes)
    if scope.slots is None:
        rels = walk(folder)
        files = [(rel, folder.joinpath(*rel.split("/"))) for rel in rels]
        xbox = {"index": INDEX_NAME, "index_kind": "verbatim"} if wgs else None
        record = {"slots": "all", "account_files": True, "cache": "none"}
        return CapturePlan(folder, "xbox", files, _sizes(files), record, None, xbox=xbox,
                           account=_xbox_account(folder, wgs), index_sha=index_sha)
    wanted = {f"Slot{s}{kind}" for s in scope.slots for kind in ("Auto", "Manual")}
    made: dict[str, tuple[bytes, int]] = {}
    if wgs:
        info, containers = _parse_index(folder)
        picked = [c for c in containers if c.identifier in wanted
                  and c.data_file is not None and c.data_file.is_file()]
        dirs = [c.directory for c in picked]
        if picked:
            subset = replace(info, trailer=b"")      # the source's trailing bytes are not ours
            made[INDEX_NAME] = (msstore.build_containers_index(subset, picked),
                                index_stat.st_mtime_ns)
        xbox = {"index": INDEX_NAME, "index_kind": "rebuilt"}
    else:
        dirs = [folder / name for name in sorted(wanted) if (folder / name / "data").is_file()]
        xbox = None
    if not dirs:
        raise StoreError(f"Slot {_slots_text(scope.slots)} holds no saves to keep.")
    rels = sorted(f"{d.name}/{rel}" for d in dirs for rel in walk(d))
    files = [(rel, folder.joinpath(*rel.split("/"))) for rel in rels]
    record = {"slots": list(scope.slots), "account_files": False, "cache": "none"}
    return CapturePlan(folder, "xbox", files, _sizes(files), record, scope.slots, xbox=xbox,
                       made=made, index_sha=index_sha)


def _parse_index(folder: Path):
    try:
        return msstore.parse_containers_index(folder)
    except (OSError, ValueError, IndexError, struct.error) as exc:
        raise StoreError(f"The Xbox save index cannot be read ({exc}).") from exc


def _xbox_account(folder: Path, wgs: bool) -> dict[str, str]:
    """Where the account-level save (``AccountData``) of a whole Xbox folder lives."""
    if not wgs:
        return {"data": "AccountData/data", "meta": "AccountData/meta"}
    _info, containers = _parse_index(folder)
    for c in containers:
        if c.identifier == "AccountData":
            return {k: f"{c.dir_guid}/{p.name}"
                    for k, p in (("data", c.data_file), ("meta", c.meta_file)) if p is not None}
    return {}


def walk(folder: Path) -> list[str]:
    """Every regular file under ``folder`` as a sorted posix path; links are skipped."""
    out = []
    for dirpath, dirnames, filenames in os.walk(folder):
        here = Path(dirpath)
        for name in list(dirnames):
            if (here / name).is_symlink():
                log.warning("skipped the linked folder %s while keeping a copy", name)
                dirnames.remove(name)
        for name in filenames:
            p = here / name
            if _is_regular(p):
                out.append(p.relative_to(folder).as_posix())
            else:
                log.warning("skipped %s while keeping a copy: not a regular file", name)
    return sorted(out)


def _is_regular(p: Path) -> bool:
    return p.is_file() and not p.is_symlink()


def _sizes(files: list[tuple[str, Path]]) -> dict[str, int]:
    sizes = {}
    for rel, path in files:
        try:
            sizes[rel] = path.stat().st_size
        except FileNotFoundError as exc:
            raise SourceChanged(rel, vanished=True) from exc
    return sizes


def _slots_text(slots: tuple[int, ...]) -> str:
    return ", ".join(str(s) for s in slots)


# --- reading ----------------------------------------------------------------------------------


def _read_file(path: Path) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def read_stable(path: Path, rel: str) -> tuple[bytes, os.stat_result]:
    """Read a whole file whose size and mtime are the same before and after the read; retried,
    then SourceChanged (the game or a sync was writing it)."""
    for _ in range(STABLE_TRIES):
        try:
            before = os.stat(path)
            data = _read_file(path)
            after = os.stat(path)
        except FileNotFoundError as exc:
            raise SourceChanged(rel, vanished=True) from exc
        except OSError as exc:
            raise StoreError(f"Could not read {rel} ({exc.strerror or exc}).") from exc
        if (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns) \
                and len(data) == after.st_size:
            return data, after
    raise SourceChanged(rel)


def require_unchanged(path: Path, rel: str, size: int, mtime_ns: int) -> None:
    try:
        st = os.stat(path)
    except FileNotFoundError as exc:
        raise SourceChanged(rel, vanished=True) from exc
    if (st.st_size, st.st_mtime_ns) != (size, mtime_ns):
        raise SourceChanged(rel)


def wants_stats(platform: Platform, rel: str) -> bool:
    """Steam save data at the top of the folder: its chunk totals check the meta (``valid``)."""
    return platform == "steam" and "/" not in rel and parse_data_filename(rel) is not None


def stats_of(data: bytes) -> lz4_block.ContainerStats:
    return lz4_block.stats(data)


# --- describing members -----------------------------------------------------------------------


def describe_members(folder: Path, platform: Platform, records: Mapping[str, FileEntry],
                     stats: Mapping[str, lz4_block.ContainerStats],
                     slots: tuple[int, ...] | None
                     ) -> tuple[list[MemberRecord], list[str], dict[str, str]]:
    """One record per slot member, from a scan of the folder made after it was read; a note
    per member file kept without its partner; and the scanner's note per member it saw
    without a save (``"7B"`` -> note). A member the scan finds must be wholly in the capture
    (else the folder changed meanwhile: SourceChanged); the caller then checks that nothing
    read has changed (:meth:`CapturePlan.require_consistent`)."""
    view = savedir.scan_any(folder)
    members, notes, absent = [], [], {}
    for slot in slots or range(1, MAX_SAVE_SLOTS + 1):
        for mv in view.slots[slot].members:
            data_rel, meta_rel = _rel(folder, mv.data_path), _rel(folder, mv.meta_path)
            if not mv.exists:
                if mv.note:
                    absent[f"{slot}{mv.label}"] = mv.note
                kept = [r for r in (data_rel, meta_rel) if r is not None and r in records]
                if kept:
                    notes.append(f"slot {slot}{mv.label}: {', '.join(kept)} kept without its "
                                 f"partner ({mv.note or 'incomplete'})")
                continue
            needed = [data_rel, meta_rel]
            container = getattr(mv.xbox, "blob_container_file", None)
            if container is not None:
                needed.append(_rel(folder, container))
            missing = [r for r in needed if r is None or r not in records]
            if missing:
                raise SourceChanged(missing[0] or mv.data_path.name)
            fd, fm = records[data_rel], records[meta_rel]   # type: ignore[index]
            if platform == "xbox" and mv.meta_sha256 and mv.meta_sha256 != fm.sha256:
                raise SourceChanged(fm.path)
            members.append(_record(slot, mv, fd, fm, platform, stats.get(fd.path)))
    return members, notes, absent


def steam_valid(info: MetaInfo | None, st: lz4_block.ContainerStats | None) -> bool:
    """A Steam member's ``valid``, the one rule capture, preserve and fsck all apply: the meta
    decrypted with the right header and its SizeDecompressed equals the data's chunk total (as
    the classic ``_validate_pair``)."""
    return bool(info is not None and info.valid and st is not None and st.chunk_count
                and st.total_decompressed == info.size_decompressed)


def _record(slot: int, mv: savedir.MemberView, fd: FileEntry, fm: FileEntry, platform: Platform,
            st: lz4_block.ContainerStats | None) -> MemberRecord:
    info = mv.info
    if platform == "steam":
        valid = steam_valid(info, st)
        ordinal, file_time_ns, identifier = mv.ordinal_used, fd.mtime_ns, None
    else:
        valid, ordinal = mv.valid, None
        file_time_ns = round(mv.data_mtime * 1_000_000_000)
        identifier = getattr(mv.xbox, "identifier", None) or fd.path.split("/")[0]
    ident = info.slot_identifier if info is not None else 0
    return MemberRecord(
        slot=slot, member=mv.label, data=fd.path, meta=fm.path, vid=fd.sha256,  # type: ignore[arg-type]
        ordinal=ordinal, meta_plain_sha256=mv.meta_sha256 or None,
        pid=pid_from_id(ident) if ident else None,
        play_time=info.total_play_time if info else 0, meta_ts=info.timestamp if info else 0,
        name=info.save_name if info else "", summary=info.save_summary if info else "",
        difficulty=info.difficulty if info else 0, game_mode=info.game_mode if info else 0,
        season=info.season if info else 0, base_version=info.base_version if info else 0,
        meta_format=info.meta_format if info else 0,
        size_decompressed=info.size_decompressed if info else 0, valid=valid, note=mv.note,
        xbox_identifier=identifier, file_time_ns=file_time_ns)


def _rel(folder: Path, path: Path) -> str | None:
    try:
        return path.relative_to(folder).as_posix()
    except ValueError:
        return None


def account_paths(plan: CapturePlan, records: Mapping[str, FileEntry]) -> dict[str, str]:
    """The entry's ``account`` record: the account-level files it holds, by role."""
    return {k: rel for k, rel in plan.account.items() if rel in records}
