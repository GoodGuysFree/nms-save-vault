"""Undo the vault upgrade (D3 4.4): the vault goes back to the old layout with everything it
held before the upgrade and everything made since, as plain folders. The pre-upgrade copy is
only read; the store is moved next to the vault, never deleted.

Steps: the copy is checked against its recorded tree hash; every store entry the copy's
catalog does not know is written out as a folder (a ``preserve`` as an ``extract``); every old
folder missing from the vault or changed is put back from the copy (a changed one is moved
aside first, not deleted); the catalog becomes the union; last, ``store/`` is moved out of the
vault (the commit point). An interrupted run is finished, or the upgrade kept, on request.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import shutil
import threading
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .. import catalog, safety
from ..catalog import CatalogEntry, Vault
from . import capture
from .errors import RollbackError
from .listing import MIGRATION_JSON, entry_folder, read_migration
from .manifest import StoreEntry
from .objects import sha256
from .upgrade import (
    _rename_retrying,
    _upgrade_lock_held,
    _write_json,
    is_pre_upgrade_copy,
    upgrade_lock_path,
)
from .versionstore import (
    STATE,
    UPGRADE_JSON,
    VersionStore,
    iso_utc,
    store_root,
    utc_now,
    vault_lock,
    vault_mode,
)

log = logging.getLogger(__name__)

STAGING = ".rollback"
ROLLED_BACK_INFIX = ".store-rolled-back-"
SUBDIR = {"snapshot": "snapshots", "extract": "extracts", "preserve": "extracts"}


@dataclass(frozen=True)
class RollbackPlan:
    vault_root: Path
    copy: Path | None                     # None: the vault was upgraded empty, without a copy
    copy_ok: bool                         # re-hashed, equals migration.json's tree_sha256
    export: tuple[tuple[str, str, str], ...]   # (store entry id, folder id, catalog kind)
    restore: tuple[str, ...]              # copy entries whose vault folder is missing or differs
    bytes_needed: int
    free: int
    problems: tuple[tuple[str, str], ...]  # (RollbackError code, ASCII text)
    skipped: tuple[tuple[str, str], ...] = ()   # (entry id, why) left as they are
    corrupt: tuple[str, ...] = ()         # store manifests that cannot be read (left behind)
    force: bool = False                   # the owner accepted leaving ``corrupt`` behind


@dataclass(frozen=True)
class RollbackResult:
    exported: tuple[str, ...]
    restored: tuple[str, ...]
    store_moved_to: str                   # the folder name next to the vault
    message: str
    skipped: tuple[tuple[str, str], ...] = ()   # (entry id, why) not put back


def rollback_interrupted(vault_root: str | Path) -> bool:
    """For the front ends (a seam): an undo of the upgrade stopped part-way; the owner chooses
    to finish it (run it again) or keep the upgrade (:func:`keep_upgrade`)."""
    marker = store_root(vault_root) / STATE / UPGRADE_JSON
    try:
        return json.loads(marker.read_bytes()).get("state") == "rolling-back"
    except (OSError, ValueError, AttributeError):
        return False


def keep_upgrade(vault_root: str | Path) -> None:
    """After an interrupted undo: stay in store mode. The staging folder goes; folders already
    written out stay as catalog entries, hidden by their store entries of the same id."""
    root = Path(vault_root)
    if not root.is_dir():
        return                                  # the lock must not create a missing vault
    with safety.exclusive_lock(upgrade_lock_path(root), timeout=0):
        if not rollback_interrupted(root):
            return
        staging = root / STAGING
        if staging.exists():
            for child in staging.iterdir():
                if not child.name.startswith("replaced-"):      # never what was moved aside
                    shutil.rmtree(child)
        (store_root(root) / STATE / UPGRADE_JSON).unlink()


NEW_VAULT_TEXT = ("This vault was made as a version store, so there is no upgrade to undo.")


def created_as_store(vault_root: str | Path) -> bool:
    """A vault that began in store mode (no upgrade ever ran on it, so none can be undone)."""
    migration = read_migration(Path(vault_root))
    return bool(migration.get("created_in_store_mode")) and "copy_aside" not in migration


def plan_rollback(vault_root: str | Path, store: VersionStore, *, copy: str | Path | None = None,
                  cancel: threading.Event | None = None, force: bool = False) -> RollbackPlan:
    """What undoing the upgrade would do, and why it cannot run now. Reads only (it re-hashes
    the whole copy: about as long as the upgrade's copy took). ``force`` accepts leaving
    unreadable store manifests behind (they are listed in the plan, never dropped silently)."""
    root = Path(vault_root)
    problems: list[tuple[str, str]] = []
    if vault_mode(root) == "legacy":
        problems.append(("not-upgraded", "The vault is not upgraded."))
    if created_as_store(root):
        return RollbackPlan(root, None, False, (), (), 0, 0, (("new-vault", NEW_VAULT_TEXT),))
    migration = _strict_migration(root)
    recorded = migration.get("copy_aside") if migration is not None else None
    folder: Path | None = Path(copy) if copy is not None else (
        root.parent / recorded["name"] if isinstance(recorded, dict) else None)
    if migration is None:
        # No record of the upgrade: never assume "upgraded without a copy".
        copy_ok = False
        if folder is None:
            problems.append(("migration-missing", "The record of the upgrade (migration.json) "
                                                  "is missing or unreadable, so the copy made "
                                                  "before it cannot be found. Choose the folder "
                                                  "the copy is in (the Choose button, or "
                                                  "nmsvault store rollback --copy <folder>)."))
        elif not (folder.is_dir() and catalog.looks_like_vault_dir(folder)
                  and is_pre_upgrade_copy(folder)):
            problems.append(("copy-mismatch", "That folder is not the copy of the vault made "
                                              "before the upgrade."))
        else:
            copy_ok = True       # no recorded hash to check it against
    else:
        copy_ok = recorded is None
    if isinstance(recorded, dict):
        if folder is None or not folder.is_dir():
            problems.append(("copy-missing", "The copy of the vault made before the upgrade was "
                                             "not found. Choose the folder it is in (the Choose "
                                             "button, or nmsvault store rollback --copy "
                                             "<folder>)."))
        elif _tree_sha(folder, cancel) != recorded.get("tree_sha256"):
            problems.append(("copy-mismatch", "That folder is not the copy of the vault made "
                                              "before the upgrade, or it has changed since."))
        else:
            copy_ok = True
    use_copy = folder if copy_ok and (recorded is not None or migration is None) else None
    work = _work(root, store, use_copy)
    corrupt = tuple(sorted(store.corrupt_manifests))
    if corrupt and not force:
        problems.append(("corrupt-manifests", f"{len(corrupt)} backups in the store cannot be "
                                              f"read ({', '.join(corrupt[:5])}); undoing the "
                                              "upgrade would leave them behind. Check the "
                                              "store first (nmsvault store fsck), or undo "
                                              "anyway (nmsvault store rollback --force)."))
    needed = sum(f.size for sid, _fid, _kind in work.export for f in store.entry(sid).files)
    needed += sum(_tree_bytes(entry_folder(Vault(use_copy, read_only=True), e))
                  for e in work.copy_entries if e.id in work.restore)
    try:
        free = shutil.disk_usage(root).free
    except OSError:
        free = 0
    if free < needed + (64 << 20):
        problems.append(("space", f"Undoing the upgrade needs {needed / (1 << 30):.1f} GB free on "
                                  f"the vault drive; there are {free / (1 << 30):.1f} GB."))
    if _upgrade_lock_held(root):
        problems.append(("in-progress", "The vault is being upgraded, or the upgrade undone, by "
                                        "another NMS Save Vault window."))
    return RollbackPlan(root, use_copy, copy_ok, work.export, work.restore, needed, free,
                        tuple(problems), work.skipped, corrupt, force)


@dataclass(frozen=True)
class _Work:
    copy_entries: list[CatalogEntry]
    export: tuple[tuple[str, str, str], ...]
    restore: tuple[str, ...]
    skipped: tuple[tuple[str, str], ...]


def _work(root: Path, store: VersionStore, copy: Path | None) -> _Work:
    """What the undo would do right now. A copy entry whose folder is missing from the copy
    (deleted by hand before the upgrade) is skipped and reported, as the fold skipped it."""
    copy_entries = _entries_of(copy) if copy is not None else []
    export = tuple(_exports(root, store, {e.id for e in copy_entries}))
    restore: list[str] = []
    skipped: list[tuple[str, str]] = []
    if copy is not None:
        copy_vault = Vault(copy, read_only=True)
        for e in copy_entries:
            if not _managed(e):
                continue
            src = entry_folder(copy_vault, e)
            if not src.is_dir():
                skipped.append((e.id, "its folder is missing from the copy"))
            elif not _same_tree(src, _vault_folder(root, e)):
                restore.append(e.id)
    return _Work(copy_entries, export, tuple(restore), tuple(skipped))


def _strict_migration(root: Path) -> dict | None:
    """migration.json as written by the upgrade (it always has ``copy_aside``), else None."""
    try:
        body = json.loads((store_root(root) / STATE / MIGRATION_JSON).read_bytes().decode("utf-8"))
    except (OSError, ValueError):
        return None
    return body if isinstance(body, dict) and "copy_aside" in body else None


def open_live_ops(root: Path) -> bool:
    """Is a live operation of any window or machine still open in this vault's journal (an
    interrupted one waits for its recovery, which reads the store this undo moves away)?"""
    from ..liveops import opjournal
    from ..organizer.store import store_root as organizer_root

    path = organizer_root(root) / opjournal.OPS
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return False
    events = [e for n, line in enumerate(raw.splitlines(), 1)
              if (e := opjournal._parse_line(line, n)) is not None]
    return any(rec.open for rec in opjournal.fold(events).values())


def run_rollback(plan: RollbackPlan, store: VersionStore, *,
                 vault_lock_factory: Callable[[], AbstractContextManager] | None = None,
                 cancel: threading.Event | None = None,
                 now: Callable[[], datetime] = utc_now,
                 faults: Callable[[str], None] | None = None) -> RollbackResult:
    """Undo the upgrade (D3 4.4 steps 1-6). Raises RollbackError before anything changed, or
    leaves an interrupted undo to finish or abandon (``rollback_interrupted``)."""
    if plan.problems:
        code, text = plan.problems[0]
        raise RollbackError(code, text)
    root = plan.vault_root
    if not root.is_dir():
        raise RollbackError("not-upgraded", "The vault folder was not found.")
    lock = vault_lock_factory or vault_lock(root)
    from ..organizer.store import liveop_lock_path

    live_lock_file = liveop_lock_path(root)
    live_lock = safety.exclusive_lock(live_lock_file, timeout=0)
    if live_lock_file.parent.is_dir():       # before the store lock, like every live write
        try:
            live_lock.__enter__()
        except (safety.LockTimeout, RuntimeError) as exc:
            raise RollbackError("live-op-busy", "A move of live saves is running in another "
                                                "window. Try again when it has finished.") from exc
    else:
        live_lock = None
    try:
        if open_live_ops(root):
            raise RollbackError("recovery-pending", "An earlier move of live saves was "
                                "interrupted. Resolve it first (it needs the current vault "
                                "layout); nothing was changed.")
        return _run_rollback_locked(plan, store, root, lock, cancel, now, faults)
    finally:
        if live_lock is not None:
            live_lock.__exit__(None, None, None)


def _run_rollback_locked(plan, store, root, lock, cancel, now, faults) -> RollbackResult:
    state_dir = store_root(root) / STATE
    staging = root / STAGING
    upgrade_lock = safety.exclusive_lock(upgrade_lock_path(root), timeout=0)
    try:
        upgrade_lock.__enter__()
    except (safety.LockTimeout, RuntimeError) as exc:
        raise RollbackError("in-progress", "The upgrade is being undone by another NMS Save "
                                           "Vault window.") from exc
    try:
        # Under the lock the plan is made again: the vault may have changed since it was made.
        mode = vault_mode(root)
        if mode == "legacy":
            raise RollbackError("not-upgraded", "The vault is not upgraded (any more).")
        if mode == "upgrading" and not rollback_interrupted(root):
            raise RollbackError("in-progress", "The vault is being upgraded.")
        if store.corrupt_manifests and not plan.force:
            raise RollbackError("corrupt-manifests", "Some backups in the store cannot be read; "
                                                     "check the store first, or force the undo.")
        fresh = _work(root, store, plan.copy)
        if (fresh.export, fresh.restore) != (plan.export, plan.restore):
            raise RollbackError("vault-changed", "The vault changed after the undo was planned. "
                                                 "Nothing was changed; start it again.")
        ids = {e.id for e in store.entries()}
        with lock():
            if {e.id for e in store.entries()} != ids or not state_dir.is_dir():
                raise RollbackError("vault-changed", "The vault changed after the undo was "
                                                     "planned. Nothing was changed; start it "
                                                     "again.")
            _write_json(state_dir / UPGRADE_JSON, {"state": "rolling-back",
                                                   "started_utc": iso_utc(now())})
        staging.mkdir(exist_ok=True)
        copy_vault = Vault(plan.copy, read_only=True) if plan.copy is not None else None
        copy_entries = {e.id: e for e in fresh.copy_entries}
        exported: list[CatalogEntry] = []
        for store_id, folder_id, kind in plan.export:
            _check(cancel)
            exported.append(_export(root, store, store.entry(store_id), folder_id, kind))
        _fault(faults, "rollback.after_export")
        restored: list[str] = []
        not_restored = list(plan.skipped)
        for entry_id in plan.restore:
            _check(cancel)
            try:
                if _restore(root, copy_vault, copy_entries[entry_id]):
                    restored.append(entry_id)
            except (RollbackError, OSError) as exc:
                # One bad folder never leaves the vault stuck: it stays as it is, and is reported.
                log.warning("could not put back %s: %s", entry_id, exc)
                not_restored.append((entry_id, str(exc) or type(exc).__name__))
        with lock():
            vault = Vault(root).load()
            for entry in list(copy_entries.values()) + exported:
                kept = vault.get(entry.id)
                if kept is None:
                    vault.upsert(entry)
                elif entry in exported:     # the owner's notes and label stay; the folder is ours
                    vault.upsert(dataclasses.replace(kept, path=entry.path, slots=entry.slots,
                                                     managed=True))
        _fault(faults, "rollback.before_store_move")
        stamp = now().astimezone(timezone.utc)
        moved = root.parent / f"{root.name}{ROLLED_BACK_INFIX}{stamp:%Y%m%dT%H%M%SZ}"
        _rename_retrying(store_root(root), moved)              # COMMIT POINT: the old layout
        safety.fsync_dir(root)
        _remove_empty(staging)
    finally:
        upgrade_lock.__exit__(None, None, None)
    size = sum(p.stat().st_size for p in moved.rglob("*") if p.is_file())
    message = (f"Upgrade undone. Your vault is back in the old layout with everything made since "
               f"({len(exported)} backups added as folders). The version store was moved to "
               f"{moved.name} ({size / (1 << 30):.1f} GB); delete it when you no longer need it. "
               "The pre-upgrade copy was not changed.")
    if not_restored:
        message += f" {len(not_restored)} old folders were not put back: " + "; ".join(
            f"{k} ({why})" for k, why in not_restored)
    if plan.corrupt:
        message += (f" {len(plan.corrupt)} unreadable backups stayed in the moved store: "
                    + ", ".join(plan.corrupt))
    return RollbackResult(tuple(e.id for e in exported), tuple(restored), moved.name, message,
                          tuple(not_restored))


# --- steps ------------------------------------------------------------------------------------


def _exports(root: Path, store: VersionStore, known: set[str]):
    """The store entries the old layout lacks: every one the copy's catalog does not know;
    a copied-in folder only when its hand-made folder is gone or no longer matches."""
    vault = Vault(root).load()
    for entry in store.entries():
        kind = "extract" if entry.kind == "preserve" else entry.kind
        origin = entry.origin or {}
        if "inplace_entry" in origin:
            original = vault.get(origin["inplace_entry"])
            if original is None or not _matches(Path(original.path), entry):
                yield entry.id, f"{entry.id}-copy", catalog.KIND_IMPORTED
            continue
        if entry.id not in known:
            yield entry.id, entry.id, kind


def _export(root: Path, store: VersionStore, entry: StoreEntry, folder_id: str,
            kind: str) -> CatalogEntry:
    """A store entry written out as a managed folder, verified, with its catalog entry."""
    final = root / SUBDIR.get(entry.kind, "backups") / folder_id
    staging = root / STAGING / folder_id
    if final.exists() and not _matches(final, entry):
        _move_aside(root, final, folder_id)
    if not final.exists():
        if staging.exists():
            shutil.rmtree(staging)                  # an earlier, unfinished attempt of ours
        store.materialize(entry.id, staging)        # verified file by file
        safety.fsync_tree(staging)
        final.parent.mkdir(parents=True, exist_ok=True)
        _rename_retrying(staging, final)
    slots = entry.scope.get("slots") if isinstance(entry.scope.get("slots"), list) else None
    made = Vault(root).make_entry_from_dir(folder_id, kind, entry.label, final, managed=True,
                                           source=entry.source.get("account_key")
                                           or entry.source.get("name"), slots=slots)
    return dataclasses.replace(made, created=entry.created_local)


def _restore(root: Path, copy_vault: Vault, entry: CatalogEntry) -> bool:
    """Put an old folder back from the copy when the vault's is missing or differs (a
    differing one is moved aside, not deleted). False when it already matches."""
    src = entry_folder(copy_vault, entry)
    dest = _vault_folder(root, entry)
    if _same_tree(src, dest):
        return False
    staging = root / STAGING / entry.id
    if staging.exists():
        shutil.rmtree(capture.long_path(staging))
    shutil.copytree(capture.long_path(src), capture.long_path(staging),
                    copy_function=_copy_synced)
    if not _same_tree(src, staging):
        raise RollbackError("copy-mismatch", f"The folder of {entry.id} could not be put back "
                                             "intact. Check the drive.")
    safety.fsync_tree(staging)      # on disk before the rename that makes it the vault's folder
    if dest.exists():
        _move_aside(root, dest, entry.id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    _rename_retrying(staging, dest)
    return True


# --- helpers ----------------------------------------------------------------------------------


def _copy_synced(src, dst):
    """``shutil.copy2`` that also flushes the new file to disk."""
    out = shutil.copy2(src, dst)
    with open(out, "rb+") as f:
        os.fsync(f.fileno())
    return out


def _managed(entry: CatalogEntry) -> bool:
    return entry.managed and entry.kind != catalog.KIND_INPLACE


def _vault_folder(root: Path, entry: CatalogEntry) -> Path:
    subdir = {catalog.KIND_SNAPSHOT: "snapshots", catalog.KIND_EXTRACT: "extracts"}.get(
        entry.kind, "backups")
    return root / subdir / entry.id


def _entries_of(copy: Path) -> list[CatalogEntry]:
    return Vault(copy, read_only=True).load().entries if catalog.looks_like_vault_dir(copy) else []


def _tree(folder: Path) -> dict[str, tuple[int, str]]:
    if not capture.long_path(folder).is_dir():
        return {}
    return {rel: (capture.file_size(folder / rel), sha256(capture.read_file(folder / rel)))
            for rel in capture.walk(folder)}


def _same_tree(a: Path, b: Path) -> bool:
    return capture.long_path(b).is_dir() and _tree(a) == _tree(b)


def _matches(folder: Path, entry: StoreEntry) -> bool:
    return capture.long_path(folder).is_dir() and _tree(folder) == {f.path: (f.size, f.sha256) for f in entry.files}


def _tree_sha(copy: Path, cancel: threading.Event | None) -> str:
    """The copy's tree hash as the upgrade recorded it (its note left out)."""
    lines = []
    for rel in capture.walk(copy):
        _check(cancel)
        if rel == catalog.PRE_UPGRADE_COPY:
            continue
        p = capture.long_path(copy / rel)
        lines.append(f"{rel}|{p.stat().st_size}|{safety.sha256_file(p)}")
    return hashlib.sha256("\n".join(sorted(lines)).encode("utf-8")).hexdigest()


def _tree_bytes(folder: Path) -> int:
    return sum(capture.file_size(folder / rel) for rel in capture.walk(folder))         if capture.long_path(folder).is_dir() else 0


def _move_aside(root: Path, folder: Path, entry_id: str) -> None:
    aside = root / STAGING / f"replaced-{entry_id}"
    n = 2
    while aside.exists():
        aside = root / STAGING / f"replaced-{entry_id}-{n}"
        n += 1
    aside.parent.mkdir(parents=True, exist_ok=True)
    _rename_retrying(folder, aside)


def _remove_empty(folder: Path) -> None:
    try:
        folder.rmdir()
    except OSError:
        pass                                  # it holds folders moved aside: they stay


def _check(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise RollbackError("cancelled", "Undoing the upgrade was cancelled; finish it or keep "
                                         "the upgrade.")


def _fault(hook: Callable[[str], None] | None, point: str) -> None:
    if hook is not None:
        hook(point)
