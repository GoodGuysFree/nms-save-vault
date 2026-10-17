"""Folding the old backups into the store (D3 4.2): each managed folder of the old layout becomes
a store entry under the same id, read and then checked against its folder. Nothing is deleted:
the folders stay until the owner asks to free them (phase 5).

``store/state/migration.json`` records the fold per entry - ``pending``, ``folded``,
``verified``, or ``skipped`` / ``failed`` with a reason - so an interrupted, cancelled or
space-starved fold resumes where it stopped. It runs when the upgrade or the owner starts it,
never by itself.
"""
from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .. import catalog, discover, safety
from ..catalog import CatalogEntry, Vault
from . import capture
from .capture import CaptureScope
from .errors import InsufficientSpace, StoreError
from .listing import entry_folder, read_migration
from .manifest import ENTRY_KINDS
from .objects import sha256
from .versionstore import STATE, VersionStore, iso_utc, store_root

log = logging.getLogger(__name__)

MIGRATION_JSON = "migration.json"
MANAGED_SUBDIRS = ("backups", "snapshots", "extracts")


@dataclass(frozen=True)
class FoldProgress:
    entries_done: int
    entries_total: int
    current: str                      # the entry being folded


@dataclass(frozen=True)
class FoldReport:
    total: int                        # old backups to fold (skipped ones not counted)
    verified: int
    skipped: tuple[tuple[str, str], ...]     # (entry id, ASCII reason)
    failed: tuple[tuple[str, str], ...]
    waiting_for_space: int            # bytes still needed on the vault drive, 0 if none
    cancelled: bool
    fsck_ok: bool | None              # the deep check after a complete fold; None if not run
    message: str


def write_migration(vault_root: Path, body: dict) -> None:
    raw = json.dumps(body, indent=1, sort_keys=True, ensure_ascii=True).encode("ascii")
    safety.atomic_write_bytes(store_root(vault_root) / STATE / MIGRATION_JSON, raw,
                              temp_prefix=".migration.tmp_")


def fold_pending(vault_root: str | Path) -> bool:
    """For the front ends (a seam): the upgrade left old backups to fold, or a fold stopped
    before the end. Reads only."""
    fold = read_migration(Path(vault_root)).get("fold")
    return isinstance(fold, dict) and fold.get("state") not in (None, "done")


def run_fold(store: VersionStore, *, live_roots: Sequence[Path] | None = None,
             progress: Callable[[FoldProgress], None] | None = None,
             cancel: threading.Event | None = None) -> FoldReport:
    """Fold every managed old backup not folded yet, oldest first; check each against its
    folder; then a deep check of the store when the fold is complete."""
    root = store.vault_root
    vault = Vault(root).load()
    live = discover.live_folders(live_roots) if live_roots is not None else store.live_folders()
    entries = sorted(vault.entries, key=lambda e: (e.created, e.id))
    statuses = _statuses(root)
    plan: list[tuple[CatalogEntry, Path]] = []
    for entry in entries:
        why, folder = _skip_reason(vault, entry, live)
        if why:
            statuses[entry.id] = {"status": "skipped", "reason": why}
        elif statuses.get(entry.id, {}).get("status") != "verified":
            plan.append((entry, folder))
            statuses.setdefault(entry.id, {"status": "pending"})
    _save(store, statuses, state="running", waiting=0)

    waiting, cancelled = 0, False
    for n, (entry, folder) in enumerate(plan, start=1):
        if cancel is not None and cancel.is_set():
            cancelled = True
            break
        if progress is not None:
            progress(FoldProgress(n - 1, len(plan), entry.id))
        try:
            statuses[entry.id] = _fold_one(store, entry, folder)
        except InsufficientSpace as exc:
            waiting = exc.needed
            break
        except (StoreError, OSError) as exc:
            log.warning("could not fold %s: %s", entry.id, exc)
            statuses[entry.id] = {"status": "failed", "reason": str(exc)}
        _save(store, statuses, state="running", waiting=0)
    complete = not cancelled and not waiting
    state = "done" if complete else "waiting-for-space" if waiting else "cancelled"
    _save(store, statuses, state=state, waiting=waiting)
    fsck_ok = store.fsck(deep=True).ok if complete else None
    return _report(statuses, waiting, cancelled, fsck_ok)


def _fold_one(store: VersionStore, entry: CatalogEntry, folder: Path) -> dict:
    subdir = folder.parent.name
    kept = store.add_from_folder(
        folder, kind=entry.kind, label=entry.label, entry_id=entry.id,  # type: ignore[arg-type]
        created_local=entry.created, source={"type": "folder", "name": entry.source or folder.name},
        scope=CaptureScope(cache="include"),
        origin={"legacy_entry": entry.id, "legacy_subdir": subdir,
                "folded_utc": iso_utc(store.clock())})
    record: dict = {"status": "folded"}
    if kept.id != entry.id:
        record["store_id"] = kept.id
        record["note"] = f"its folder changed after an earlier fold; kept as {kept.id}"
    problem = verify_folded(store, kept.id, folder)
    if problem:
        return {**record, "status": "failed", "reason": problem}
    return {**record, "status": "verified"}


def verify_folded(store: VersionStore, entry_id: str, folder: Path) -> str | None:
    """Why a folded entry does not match its folder, or None: every file of the folder is in
    the entry with the same size and sha256, none is missing, and every stored copy re-hashes."""
    entry = store.entry(entry_id)
    files = {f.path: f for f in entry.files}
    on_disk = set(capture.walk(folder))
    if on_disk != set(files):
        extra, missing = sorted(on_disk - set(files)), sorted(set(files) - on_disk)
        return f"the folder and the store differ ({(extra + missing)[0]})"
    for rel, fe in files.items():
        data = (folder / rel).read_bytes()
        if len(data) != fe.size or sha256(data) != fe.sha256:
            return f"the folder and the store differ ({rel})"
        store.file_bytes(fe)                      # the stored copy re-hashes
    return None


def _skip_reason(vault: Vault, entry: CatalogEntry, live) -> tuple[str, Path]:
    folder = entry_folder(vault, entry)
    if entry.kind == catalog.KIND_INPLACE or not entry.managed:
        return "a folder kept outside the vault (it can be copied in)", folder
    if entry.kind not in ENTRY_KINDS:
        return f"an unknown kind of entry ({entry.kind})", folder
    if discover.is_inside(folder, live):
        return "a live save folder, not a backup", folder
    if not folder.is_dir():
        return "its folder is missing", folder
    if folder.parent.name not in MANAGED_SUBDIRS or folder.parent.parent != vault.root:
        return "its folder is not in this vault", folder
    return "", folder


def _statuses(root: Path) -> dict[str, dict]:
    fold = read_migration(root).get("fold")
    entries = fold.get("entries") if isinstance(fold, dict) else None
    return {k: dict(v) for k, v in (entries or {}).items() if isinstance(v, dict)}


def _save(store: VersionStore, statuses: dict, *, state: str, waiting: int) -> None:
    """Under the vault lock: the record of the fold, the copy-aside record kept as it is."""
    with store._lock():
        body = read_migration(store.vault_root)
        body["fold"] = {"state": state, "waiting_for_space_bytes": waiting,
                        "entries": statuses}
        write_migration(store.vault_root, body)


def _report(statuses: dict, waiting: int, cancelled: bool, fsck_ok: bool | None) -> FoldReport:
    todo = {k: v for k, v in statuses.items() if v.get("status") != "skipped"}
    verified = sum(1 for v in todo.values() if v.get("status") == "verified")
    skipped = tuple(sorted((k, v.get("reason", "")) for k, v in statuses.items()
                           if v.get("status") == "skipped"))
    failed = tuple(sorted((k, v.get("reason", "")) for k, v in todo.items()
                          if v.get("status") == "failed"))
    if waiting:
        message = (f"{verified} of your {len(todo)} old backups are in the store; the rest need "
                   f"{waiting / (1 << 30):.1f} GB more free space on the vault drive.")
    elif cancelled:
        message = f"Stopped: {verified} of your {len(todo)} old backups are in the store so far."
    else:
        message = (f"Your {verified} old backups are now in the store. The old folders stay "
                   "where they are.")
    if failed:
        message += f" {len(failed)} stayed as folders: " + "; ".join(
            f"{k} ({why})" for k, why in failed)
    return FoldReport(len(todo), verified, skipped, failed, waiting, cancelled, fsck_ok, message)
