"""Freeing the old backup folders (D3 4.5): after the upgrade and the fold, the folders of the old
layout still sit in the vault beside the store. On the owner's request only, the ones whose every
file is durably in the store are deleted.

Nothing is deleted unless: a deep check of the whole store passes first; the folder is a managed
entry folder strictly inside ``backups/``, ``snapshots/`` or ``extracts/`` of this vault (never an
in-place folder, a live folder or the pre-upgrade copy); and the folder, read again immediately
before it is deleted, holds exactly the files the store entry holds, with the same sizes and
sha256. Only those checked files are removed: anything that appears in the folder after the check
stays and is reported. A folder that changed or was extended after the fold, or that holds a link
or a junction anywhere, is skipped and reported.

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
from .listing import CopyAsideInfo, _copy_aside, read_migration
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


@dataclass(frozen=True)
class _Verdict:
    candidate: FreeCandidate | None = None       # the folder may go ...
    files: tuple[Path, ...] = ()                 # ... and these are exactly the files checked
    dirs: tuple[Path, ...] = ()                  # the folders in it, deepest first
    reason: str = ""                             # else: why it is left as it is ("" = not ours)


def free_pending(vault_root: str | Path) -> bool:
    """For the front ends: a run of freeing stopped before its end. Reads only."""
    free = read_migration(Path(vault_root)).get("free")
    return isinstance(free, dict) and free.get("state") == "freeing"


def plan_free(vault: Vault, store: VersionStore, *,
              live_roots: Sequence[Path] | None = None,
              cancel: threading.Event | None = None) -> FreePlan:
    """What freeing would delete now, and what it leaves with the reason. Reads only (it reads
    every candidate folder once, to check it against the store)."""
    migration = read_migration(vault.root)
    live = discover.live_folders(live_roots) if live_roots is not None else store.live_folders()
    aside = _copy_aside(vault.root, migration)
    candidates: list[FreeCandidate] = []
    skipped: list[tuple[str, str]] = []
    for entry in sorted(vault.entries, key=lambda e: (e.created, e.id)):
        if cancel is not None and cancel.is_set():
            break
        verdict = _judge(vault, store, entry, migration, live, aside)
        if verdict.candidate is not None:
            candidates.append(verdict.candidate)
        elif verdict.reason:
            skipped.append((entry.id, verdict.reason))
    return FreePlan(vault.root, tuple(candidates), tuple(skipped),
                    sum(c.bytes for c in candidates), aside)


def run_free(store: VersionStore, plan: FreePlan, *,
             live_roots: Sequence[Path] | None = None,
             progress: Callable[[FreeProgress], None] | None = None,
             cancel: threading.Event | None = None,
             faults: FreeFault | None = None) -> FreeResult:
    """Delete the folders of ``plan`` (and only those). A deep check of the store runs first and
    any damage stops the run before anything is deleted (FreeError ``damaged``). Each folder is
    checked against the store again immediately before it is deleted, and only the files that
    check covered are removed: anything else found in it stays and is reported. Safe to stop at
    any point: run it again to finish."""
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
    live = discover.live_folders(live_roots) if live_roots is not None else store.live_folders()
    statuses = _free_statuses(root)
    _save(store, statuses, "freeing")
    freed: list[str] = []
    skipped: list[tuple[str, str]] = []
    leftovers: list[tuple[str, str]] = []
    freed_bytes, cancelled = 0, False
    for n, wanted in enumerate(plan.entries):
        if cancel is not None and cancel.is_set():
            cancelled = True
            break
        if progress is not None:
            progress(FreeProgress(n, len(plan.entries), wanted.id))
        vault = Vault(root).load()
        entry = vault.get(wanted.id)
        migration = read_migration(root)
        verdict = (_judge(vault, store, entry, migration, live, _copy_aside(root, migration))
                   if entry is not None else _Verdict(reason="it is no longer in the catalog"))
        if verdict.candidate is None:
            skipped.append((wanted.id, verdict.reason or "it is no longer a folder to free"))
            continue
        statuses[wanted.id] = {"status": "freeing"}
        _save(store, statuses, "freeing")
        _fault(faults, "free.before_delete")
        left = _delete_listed(verdict.candidate.folder, verdict.files, verdict.dirs)
        _fault(faults, "free.after_delete")
        if left:
            leftovers.append((wanted.id, left[0]))
            continue                            # stays 'freeing'; a rerun finishes it
        _drop_catalog_entry(store, wanted.id)
        statuses[wanted.id] = {"status": "freed"}
        _save(store, statuses, "freeing")
        freed.append(wanted.id)
        freed_bytes += verdict.candidate.bytes
    done = not cancelled and not leftovers
    _save(store, statuses, "done" if done else "freeing")
    return FreeResult(tuple(freed), freed_bytes, tuple(skipped), tuple(leftovers), cancelled,
                      _message(freed, freed_bytes, skipped, leftovers, cancelled))


# --- checking a folder against the store -------------------------------------------------------


def _judge(vault: Vault, store: VersionStore, entry: CatalogEntry, migration: dict, live,
           aside: CopyAsideInfo | None) -> _Verdict:
    """Whether this catalog entry's folder may be freed now. No candidate and no reason: not
    something freeing is about (in-place, not folded, done earlier)."""
    record = _records(migration.get("fold")).get(entry.id)
    if record is None or entry.kind == catalog.KIND_INPLACE or not entry.managed:
        return _Verdict()
    status = _records(migration.get("free")).get(entry.id, {}).get("status")
    resuming = status == "freeing"
    folder = _folder_of(vault, entry)
    if status == "freed" and not folder.exists():
        return _Verdict()                       # done earlier; its catalog entry was kept (a note)
    if record.get("status") == "failed":
        return _Verdict(reason=f"it is not in the store ({record.get('reason', '')})")
    if record.get("status") != "verified":
        return _Verdict()
    if "store_id" in record:
        return _Verdict(reason="its folder changed after an earlier fold")
    if not store.has_entry(entry.id):
        return _Verdict(reason="the store holds no copy of it")
    stored = store.entry(entry.id)
    if (stored.origin or {}).get("legacy_entry") != entry.id:
        return _Verdict(reason="the store entry of that name is not its folded copy")
    if not folder.is_dir():
        if resuming:
            return _Verdict(FreeCandidate(entry.id, folder, 0, True))
        return _Verdict(reason="its folder is already gone")
    if not _managed_folder(vault, entry, folder):
        return _Verdict(reason="its folder is not a backup folder of this vault")
    if discover.is_inside(folder, live):
        return _Verdict(reason="a live save folder, not a backup")
    if aside is not None and aside.found and _inside(folder, vault.root.parent / aside.name):
        return _Verdict(reason="inside the pre-upgrade copy")
    inventory = _inventory(folder)
    if inventory is None:
        return _Verdict(reason="it holds a link, a junction or a special file")
    files, dirs = inventory
    held = {fe.path: fe for fe in stored.files}
    if resuming:
        if not set(files) <= set(held):
            return _Verdict(reason="it holds a file the store lacks")
    elif set(files) != set(held):
        return _Verdict(reason="it was changed or extended after the fold")
    total = 0
    for rel, path in files.items():
        fe = held[rel]
        try:
            data = path.read_bytes()
        except OSError as exc:
            return _Verdict(reason=f"it cannot be read ({exc.strerror or exc})")
        if len(data) != fe.size or sha256(data) != fe.sha256:
            return _Verdict(reason="it was changed or extended after the fold")
        if fe.inline is None and store.objects.size_of(fe.sha256) != fe.size:
            return _Verdict(reason="its stored copy is missing")
        total += len(data)
    return _Verdict(FreeCandidate(entry.id, folder, total, resuming), tuple(files.values()), dirs)


def _folder_of(vault: Vault, entry: CatalogEntry) -> Path:
    """Where the old layout kept this entry: ``<subdir>/<id>`` of this vault. Never the
    recorded path, which may point anywhere."""
    subdir = {catalog.KIND_SNAPSHOT: vault.snapshots_dir,
              catalog.KIND_EXTRACT: vault.extracts_dir}.get(entry.kind, vault.backups_dir)
    return subdir / entry.id


def _managed_folder(vault: Vault, entry: CatalogEntry, folder: Path) -> bool:
    if _is_link(folder) or folder.name != entry.id or folder.parent.name not in MANAGED_SUBDIRS:
        return False
    try:
        return (folder.parent.resolve().parent == vault.root.resolve()
                and folder.resolve().parent == folder.parent.resolve())
    except OSError:
        return False


def _is_link(path: Path) -> bool:
    """A symbolic link or, on Windows, any reparse point: a directory junction is no
    ``is_symlink`` but os.walk and the like follow it."""
    try:
        st = os.lstat(path)
    except OSError:
        return True
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & reparse)


def _inventory(folder: Path) -> tuple[dict[str, Path], tuple[Path, ...]] | None:
    """The plain files of the folder by relative posix path, and its sub-folders deepest first;
    None when it holds anything else (a link, a junction, a special file). Never descends into
    a link."""
    files: dict[str, Path] = {}
    dirs: list[Path] = []
    stack = [folder]
    while stack:
        here = stack.pop()
        try:
            with os.scandir(here) as it:
                items = list(it)
        except OSError:
            return None
        for item in items:
            p = Path(item.path)
            if _is_link(p):
                return None
            if item.is_dir(follow_symlinks=False):
                dirs.append(p)
                stack.append(p)
            elif item.is_file(follow_symlinks=False):
                files[p.relative_to(folder).as_posix()] = p
            else:
                return None
    return files, tuple(sorted(dirs, key=lambda d: len(d.parts), reverse=True))


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return False
    return True


# --- deleting ----------------------------------------------------------------------------------


def _delete_listed(folder: Path, files: Sequence[Path], dirs: Sequence[Path]) -> list[str]:
    """Delete exactly the files that were checked, then the folders they leave empty, then the
    folder. Anything else found in it is not touched: it keeps its folder and is reported.
    Returns what could not be deleted (empty when the folder is gone); never raises on a file."""
    left: list[str] = []
    for path in files:
        _retry(path, os.unlink, left)
    for path in (*dirs, folder):
        _retry(path, os.rmdir, left)
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
