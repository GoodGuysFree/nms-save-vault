"""Materialize and export: write a store entry back out as a plain folder (D3 2.7).

The folder is what was captured, byte for byte, with the captured modification times: a Steam
entry gives an ``st_``-shaped folder, an Xbox entry a ``wgs``-shaped one (index + GUID
folders), so the classic operations and any folder-reading tool can use it. A slot selection of
an Xbox entry gets an index rebuilt for those slots. Every written file is read back and
checked against its sha256.
"""
from __future__ import annotations

import logging
import os
import shutil
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .. import discover, msstore
from ..formats import CACHE_DIR, MAX_SAVE_SLOTS
from ..slotmap import data_filename, meta_filename, slot_file_numbers
from .errors import StoreError
from .manifest import FileEntry, StoreEntry
from .objects import sha256

if TYPE_CHECKING:
    from .versionstore import VersionStore

log = logging.getLogger(__name__)

BAD_DESTINATION = ("Choose an empty folder outside the vault's store and outside the game's "
                   "save folders.")


@dataclass(frozen=True)
class MaterializeResult:
    dest: Path
    files: int
    bytes: int


def select_files(entry: StoreEntry, *, slots: Iterable[int] | None = None,
                 account_files: bool = True, cache: bool = True) -> list[FileEntry]:
    """The files of ``entry`` a materialization writes. ``slots``: only those slots' saves
    (Xbox: plus the entry's index), with the account-level files when ``account_files``."""
    chosen = None if slots is None else set(slots)
    if chosen is not None and any(not 1 <= s <= MAX_SAVE_SLOTS for s in chosen):
        raise ValueError(f"not a slot: {sorted(chosen)}")
    account = set(entry.account.values())
    out = []
    if entry.platform == "xbox":
        account_dirs = {_top(p) for p in account} - {None}
        slot_dirs = None if chosen is None else {
            _top(path) for m in entry.members if m.slot in chosen for path in (m.data, m.meta)}
        index = entry.xbox.get("index") if entry.xbox else None
        for fe in entry.files:
            top = _top(fe.path)
            if fe.path in account or top in account_dirs:
                if account_files:
                    out.append(fe)
            elif slot_dirs is None or fe.path == index or top in slot_dirs:
                out.append(fe)
        return out
    names = None if chosen is None else {
        name for s in chosen for fno in slot_file_numbers(s)
        for name in (data_filename(fno), meta_filename(fno))}
    for fe in entry.files:
        if fe.path in account:
            if account_files:
                out.append(fe)
        elif fe.path.startswith(CACHE_DIR + "/"):
            if cache and names is None:
                out.append(fe)
        elif names is None or fe.path in names:
            out.append(fe)
    return out


def _top(path: str) -> str | None:
    return path.split("/", 1)[0] if "/" in path else None


def files_to_write(store: "VersionStore", entry: StoreEntry, *, slots: Iterable[int] | None,
                   account_files: bool, cache: bool) -> list[FileEntry]:
    """:func:`select_files`, with the index of an Xbox entry rebuilt for a slot selection: just
    the chosen slots' records (and AccountData's with the account files), as the classic
    extract writes it, so the folder holds no record of a save it lacks."""
    files = select_files(entry, slots=slots, account_files=account_files, cache=cache)
    index = entry.xbox.get("index") if entry.xbox else None
    if entry.platform != "xbox" or slots is None or index is None:
        return files
    chosen = set(slots)
    keep = {m.xbox_identifier for m in entry.members if m.slot in chosen}
    if account_files and entry.account:
        keep.add("AccountData")
    stored = entry.file(index)
    rebuilt = rebuilt_index(store.file_bytes(stored), keep)
    made = FileEntry(index, len(rebuilt), sha256(rebuilt), stored.mtime_ns, inline=rebuilt)
    return [made if fe.path == index else fe for fe in files]


def rebuilt_index(raw: bytes, identifiers: set[str | None]) -> bytes:
    """``raw`` (a containers.index) with only the records of ``identifiers``, each verbatim; the
    trailing bytes after the last record are dropped, as the classic extract drops them."""
    try:
        header, records, _trailer = msstore.split_index(raw)
    except ValueError as exc:
        raise StoreError(f"The stored Xbox save index cannot be read ({exc}).") from exc
    return msstore.join_index(header, [(i, rec) for i, rec in records if i in identifiers])


def check_destination(dest: Path, *, refuse_inside: Sequence[Path],
                      live: Sequence[Path]) -> None:
    """``dest`` must be an empty or absent folder, not inside any of ``refuse_inside`` nor in a
    live save folder (``live``: :func:`discover.live_folders`, xgs twins included)."""
    if any(_inside(dest, root) for root in refuse_inside) or discover.is_inside(dest, live):
        raise StoreError(BAD_DESTINATION)
    if dest.exists() and (not dest.is_dir() or any(dest.iterdir())):
        raise StoreError(BAD_DESTINATION)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def write_entry(store: "VersionStore", entry: StoreEntry, files: Sequence[FileEntry],
                dest: Path, *, keep_mtimes: bool = True) -> MaterializeResult:
    """Write ``files`` of ``entry`` under ``dest`` (an empty or absent folder), then read every
    one back. On any failure what this call wrote is removed and the error raised."""
    created = not dest.exists()
    dest.mkdir(parents=True, exist_ok=True)
    try:
        total = 0
        for fe in files:
            target = dest.joinpath(*fe.path.split("/"))
            if not _inside(target, dest):          # the loader already refuses such paths
                raise StoreError(f"Entry {entry.id} names a file outside its folder.")
            target.parent.mkdir(parents=True, exist_ok=True)
            data = store.file_bytes(fe)
            _write_file(target, data)
            if keep_mtimes:
                os.utime(target, ns=(fe.mtime_ns, fe.mtime_ns))
            total += fe.size
        for fe in files:
            if sha256(dest.joinpath(*fe.path.split("/")).read_bytes()) != fe.sha256:
                raise StoreError(f"The copy of {fe.path} written to the destination does not "
                                 "match. Check the drive.")
    except BaseException:
        _remove_written(dest, created)
        raise
    return MaterializeResult(dest, len(files), total)


def _write_file(path: Path, data: bytes) -> None:
    with open(path, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def _remove_written(dest: Path, created: bool) -> None:
    """Undo a failed materialization: ``dest`` was empty or absent before it started."""
    try:
        if created:
            shutil.rmtree(dest)
        else:
            for child in dest.iterdir():
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink()
    except OSError as exc:
        log.warning("could not remove an unfinished copy of a stored entry: %s", exc)
