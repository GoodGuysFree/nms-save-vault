"""Freeing the old backup folders (D3 4.5): after the upgrade and the fold, the folders of the old
layout still sit in the vault beside the store. On the owner's request only, the ones whose every
file is durably in the store are deleted.

Nothing is deleted unless: a deep check of the whole store passes first; the folder is a managed
entry folder strictly inside ``backups/``, ``snapshots/`` or ``extracts/`` of this vault (never an
in-place folder, a live folder or the pre-upgrade copy); and the folder, read again now, holds
exactly the files the store entry holds, with the same sizes and sha256. A folder that changed
or was extended after the fold is skipped and reported.

``store/state/migration.json`` records the run (``free``): an entry is ``freeing`` before its
folder is touched and ``freed`` when its folder and catalog entry are gone, so an interrupted
run resumes where it stopped. The listing never changes: a folded entry is listed from the store
under the same id whether its folder is there or not.
"""
from __future__ import annotations

import logging
import os
import stat
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .. import catalog, discover, safety
from ..catalog import CatalogEntry, Vault
from .errors import FreeError, StoreBusy, StoreNotUpgraded
from .listing import CopyAsideInfo, _copy_aside, entry_folder, read_migration
from .migrate import MANAGED_SUBDIRS, write_migration
from .objects import sha256
from .upgrade import upgrade_lock_path
from .versionstore import VersionStore, vault_mode

log = logging.getLogger(__name__)

FreeFault = Callable[[str], None]       # tests: raise at a named point to simulate a crash


@dataclass(frozen=True)
class FreeCandidate:
    id: str
    folder: Path
    bytes: int                          # what deleting it gives back
    resuming: bool                      # an earlier run began on it (it may be part-deleted)


@dataclass(frozen=True)
class FreePlan:
    vault_root: Path
    entries: tuple[FreeCandidate, ...]
    skipped: tuple[tuple[str, str], ...]        # (entry id, ASCII reason) left as they are
    bytes: int
    copy_aside: CopyAsideInfo | None            # for the owner's message; never touched here


@dataclass(frozen=True)
class FreeProgress:
    entries_done: int
    entries_total: int
    current: str


@dataclass(frozen=True)
class FreeResult:
    freed: tuple[str, ...]
    freed_bytes: int
    skipped: tuple[tuple[str, str], ...]
    leftovers: tuple[tuple[str, str], ...]      # (entry id, what could not be deleted)
    cancelled: bool
    message: str


def free_pending(vault_root: str | Path) -> bool:
    """For the front ends: a run of freeing stopped before its end. Reads only."""
    free = read_migration(Path(vault_root)).get("free")
    return isinstance(free, dict) and free.get("state") == "freeing"


def plan_free(vault: Vault, store: VersionStore, *,
              live_roots: Sequence[Path] | None = None,
              cancel: threading.Event | None = None) -> FreePlan:
    """What freeing would delete now, and what it leaves with the reason. Reads only (it reads
    every candidate folder once, to check it against the store)."""
    root = vault.root
    migration = read_migration(root)
    fold = _records(migration.get("fold"))
    freed = _records(migration.get("free"))
    live = discover.live_folders(live_roots) if live_roots is not None else store.live_folders()
    aside = _copy_aside(root, migration)
    candidates: list[FreeCandidate] = []
    skipped: list[tuple[str, str]] = []
    for entry in sorted(vault.entries, key=lambda e: (e.created, e.id)):
        if cancel is not None and cancel.is_set():
            break
        record = fold.get(entry.id)
        if record is None or entry.kind == catalog.KIND_INPLACE or not entry.managed:
            continue
        resuming = freed.get(entry.id, {}).get("status") == "freeing"
        if freed.get(entry.id, {}).get("status") == "freed" and not entry_folder(
                vault, entry).is_dir():
            continue                            # done earlier; its catalog entry was kept (a note)
        if record.get("status") == "failed":
            skipped.append((entry.id, f"it is not in the store ({record.get('reason', '')})"))
            continue
        if record.get("status") != "verified":
            continue
        why, size = _assess(vault, store, entry, record, live, aside, resuming)
        if why:
            skipped.append((entry.id, why))
        else:
            candidates.append(FreeCandidate(entry.id, _folder_of(vault, entry), size, resuming))
    return FreePlan(root, tuple(candidates), tuple(skipped), sum(c.bytes for c in candidates),
                    aside)


def run_free(store: VersionStore, plan: FreePlan, *,
             live_roots: Sequence[Path] | None = None,
             progress: Callable[[FreeProgress], None] | None = None,
             cancel: threading.Event | None = None,
             faults: FreeFault | None = None) -> FreeResult:
    """Delete the folders of ``plan`` (and only those) that still check out. A deep check of the
    store runs first and any damage stops the run before anything is deleted (FreeError
    ``damaged``). Every folder is checked again just before it goes. Safe to stop at any
    point: run it again to finish."""
    store._require_writable()
    root = store.vault_root
    try:
        held = safety.exclusive_lock(upgrade_lock_path(root), timeout=0)
        held.__enter__()
    except (safety.LockTimeout, RuntimeError) as exc:
        raise FreeError("busy", "The vault is being upgraded or the upgrade undone; try again "
                                "afterwards.") from exc
    try:
        return _run_free(store, plan, live_roots, progress, cancel, faults)
    finally:
        held.__exit__(None, None, None)


def _run_free(store, plan, live_roots, progress, cancel, faults) -> FreeResult:
    root = store.vault_root
    if vault_mode(root) != "store":
        raise StoreNotUpgraded("The vault is not in store mode.")
    report = store.fsck(deep=True)
    if not report.ok:
        left = len(report.problems) - report.repaired
        raise FreeError("damaged", f"The check of the version store found {left} problem(s); "
                                   "nothing was deleted. Run 'nmsvault store fsck --deep "
                                   "--repair' first.")
    wanted = {c.id for c in plan.entries}
    fresh = plan_free(Vault(root).load(), store, live_roots=live_roots, cancel=cancel)
    todo = [c for c in fresh.entries if c.id in wanted]
    skipped = [(k, why) for k, why in fresh.skipped if k in wanted]
    skipped += [(k, "it is no longer a folder to free") for k in sorted(wanted)
                if k not in {c.id for c in fresh.entries} | {k for k, _ in fresh.skipped}]
    statuses = _free_statuses(root)
    _save(store, statuses, "freeing")
    freed: list[str] = []
    leftovers: list[tuple[str, str]] = []
    freed_bytes, cancelled = 0, False
    for n, cand in enumerate(todo):
        if cancel is not None and cancel.is_set():
            cancelled = True
            break
        if progress is not None:
            progress(FreeProgress(n, len(todo), cand.id))
        statuses[cand.id] = {"status": "freeing"}
        _save(store, statuses, "freeing")
        _fault(faults, "free.before_delete")
        left = _delete_tree(cand.folder)
        _fault(faults, "free.after_delete")
        if left:
            leftovers.append((cand.id, left[0]))
            continue                            # stays 'freeing'; a rerun finishes it
        _drop_catalog_entry(store, cand.id)
        statuses[cand.id] = {"status": "freed"}
        _save(store, statuses, "freeing")
        freed.append(cand.id)
        freed_bytes += cand.bytes
    done = not cancelled and not leftovers
    _save(store, statuses, "done" if done else "freeing")
    return FreeResult(tuple(freed), freed_bytes, tuple(skipped), tuple(leftovers), cancelled,
                      _message(freed, freed_bytes, skipped, leftovers, cancelled))


# --- checking a folder against the store -------------------------------------------------------


def _assess(vault: Vault, store: VersionStore, entry: CatalogEntry, record: dict,
            live, aside: CopyAsideInfo | None, resuming: bool) -> tuple[str, int]:
    """("", bytes it would free) when the folder may go, else (why not, 0)."""
    if "store_id" in record:
        return "its folder changed after an earlier fold", 0
    if not store.has_entry(entry.id):
        return "the store holds no copy of it", 0
    stored = store.entry(entry.id)
    if (stored.origin or {}).get("legacy_entry") != entry.id:
        return "the store entry of that name is not its folded copy", 0
    folder = _folder_of(vault, entry)
    if not folder.is_dir():
        if resuming:
            return "", 0
        return "its folder is already gone", 0
    if not _managed_folder(vault, entry, folder):
        return "its folder is not a backup folder of this vault", 0
    if discover.is_inside(folder, live):
        return "a live save folder, not a backup", 0
    if aside is not None and aside.found and _inside(
            folder, vault.root.parent / aside.name):
        return "inside the pre-upgrade copy", 0
    files = _folder_files(folder)
    if files is None:
        return "it holds a link or a special file", 0
    held = {fe.path: fe for fe in stored.files}
    if resuming:
        if not set(files) <= set(held):
            return "it holds a file the store lacks", 0
    elif set(files) != set(held):
        return "it was changed or extended after the fold", 0
    total = 0
    for rel, path in files.items():
        fe = held[rel]
        try:
            data = path.read_bytes()
        except OSError as exc:
            return f"it cannot be read ({exc.strerror or exc})", 0
        if len(data) != fe.size or sha256(data) != fe.sha256:
            return "it was changed or extended after the fold", 0
        if fe.inline is None and store.objects.size_of(fe.sha256) != fe.size:
            return "its stored copy is missing", 0
        total += len(data)
    return "", total


def _folder_of(vault: Vault, entry: CatalogEntry) -> Path:
    """Where the old layout kept this entry: ``<subdir>/<id>`` of this vault. Never the
    recorded path, which may point anywhere."""
    subdir = {catalog.KIND_SNAPSHOT: vault.snapshots_dir,
              catalog.KIND_EXTRACT: vault.extracts_dir}.get(entry.kind, vault.backups_dir)
    return subdir / entry.id


def _managed_folder(vault: Vault, entry: CatalogEntry, folder: Path) -> bool:
    if folder.is_symlink() or folder.name != entry.id or folder.parent.name not in MANAGED_SUBDIRS:
        return False
    try:
        return (folder.parent.resolve().parent == vault.root.resolve()
                and folder.resolve().parent == folder.parent.resolve())
    except OSError:
        return False


def _folder_files(folder: Path) -> dict[str, Path] | None:
    """Every file of the folder by relative posix path; None when it holds anything but plain
    files and folders (a link, a special file): that is not a folder this app made."""
    out: dict[str, Path] = {}
    for dirpath, dirnames, filenames in os.walk(folder):
        here = Path(dirpath)
        if any((here / d).is_symlink() for d in dirnames):
            return None
        for name in filenames:
            p = here / name
            if p.is_symlink() or not p.is_file():
                return None
            out[p.relative_to(folder).as_posix()] = p
    return out


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return False
    return True


# --- deleting ----------------------------------------------------------------------------------


def _delete_tree(folder: Path) -> list[str]:
    """Delete the folder bottom-up. A read-only file gets its attribute cleared and one more try.
    Returns what could not be deleted (empty when the folder is gone); never raises on a file."""
    left: list[str] = []
    if not folder.exists():
        return left
    for dirpath, dirnames, filenames in os.walk(folder, topdown=False):
        here = Path(dirpath)
        for name in filenames:
            _retry(here / name, os.unlink, left)
        for name in dirnames:
            _retry(here / name, os.rmdir, left)
    _retry(folder, os.rmdir, left)
    return left


def _retry(path: Path, remove: Callable[[Path], None], left: list[str]) -> None:
    try:
        remove(path)
        return
    except FileNotFoundError:
        return
    except OSError:
        pass
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        remove(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("could not delete %s: %s", path.name, exc)
        left.append(f"{path.name} ({exc.strerror or exc})")


def _drop_catalog_entry(store: VersionStore, entry_id: str) -> None:
    """Under the vault lock, remove the entry from the catalog (the store lists it under the same
    id). An entry carrying the owner's note stays: the note is not in the store."""
    try:
        with store._lock():
            vault = Vault(store.vault_root).load()
            entry = vault.get(entry_id)
            if entry is not None and not entry.note:
                vault.remove(entry_id)
    except safety.LockTimeout as exc:
        raise StoreBusy() from exc


# --- the record --------------------------------------------------------------------------------


def _records(value: object) -> dict[str, dict]:
    entries = value.get("entries") if isinstance(value, dict) else None
    return {k: v for k, v in (entries or {}).items() if isinstance(v, dict)}


def _free_statuses(root: Path) -> dict[str, dict]:
    return {k: dict(v) for k, v in _records(read_migration(root).get("free")).items()}


def _save(store: VersionStore, statuses: dict, state: str) -> None:
    """Under the vault lock: the record of freeing; the rest of migration.json kept as it is."""
    try:
        with store._lock():
            if vault_mode(store.vault_root) != "store":      # never recreate store/state/
                raise StoreNotUpgraded("The vault is not in store mode.")
            body = read_migration(store.vault_root)
            body["free"] = {"state": state, "entries": statuses}
            write_migration(store.vault_root, body)
    except safety.LockTimeout as exc:
        raise StoreBusy() from exc


def _fault(faults: FreeFault | None, point: str) -> None:
    if faults is not None:
        faults(point)


def _message(freed, freed_bytes, skipped, leftovers, cancelled) -> str:
    gb = freed_bytes / (1 << 30)
    size = f"{gb:.1f} GB" if freed_bytes >= (1 << 27) else f"{freed_bytes / (1 << 20):.1f} MB"
    if cancelled:
        text = f"Stopped: freed {size}; {len(freed)} old folders deleted so far."
    else:
        text = f"Freed {size}. {len(freed)} old folders deleted; every backup is still listed."
    if skipped:
        text += f" {len(skipped)} folders were left as they are: " + "; ".join(
            f"{k} ({why})" for k, why in skipped)
    if leftovers:
        text += f" {len(leftovers)} folders could not be fully deleted (run it again): " + "; ".join(
            f"{k} ({what})" for k, what in leftovers)
    return text
