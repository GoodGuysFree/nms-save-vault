"""The vault upgrade to the version store (D3 4.1): a verified copy of the whole vault is made
next to it first, and only then is the store created.

Nothing in the vault changes before the copy is verified, except ``store/state/`` (the upgrade's
lock and its ``upgrade.json``). A failure removes the upgrade's own unfinished copy (the
``.partial`` folder) and ``store/``. Nothing here runs by itself: the GUI or ``nmsvault store
upgrade`` starts it, and only for the vault the owner chose.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
import time
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .. import catalog, discover, safety
from .errors import UpgradeError
from .versionstore import (
    FORMAT_NAME,
    INLINE_MAX,
    SPACE_MARGIN,
    STATE,
    STORE_DIR,
    UPGRADE_JSON,
    create_store,
    iso_utc,
    store_root,
    utc_now,
    vault_lock,
    vault_mode,
)

log = logging.getLogger(__name__)

UPGRADE_LOCK = ".upgrade.lock"
MIGRATION_JSON = "migration.json"
PARTIAL = ".partial"
COPY_INFIX = ".pre-store-"
GIB = 1 << 30
BLOCK = 1 << 20
MAX_ROUNDS = 3
MAX_PATH = 260              # Windows' limit; the copy must fit it wherever the vault moves
MANAGED_DIRS = ("backups", "snapshots", "extracts")
VAULT_CHANGING = ("The upgrade stopped: the vault kept changing while it was copied. Close other "
                  "NMS Save Vault windows and try again.")


@dataclass(frozen=True)
class UpgradePlan:
    vault_root: Path
    files: int
    bytes: int                        # the whole vault tree, store/ excluded
    fingerprint: str                  # sha256 of the sorted "relpath|size|mtime_ns" lines
    dest: Path                        # where the copy goes (next to the vault by default)
    dest_free: int
    needed: int                       # bytes + 1 GiB
    longest_dest_path: int
    copy: bool                        # False: a vault with no entries is upgraded without one
    problems: tuple[tuple[str, str], ...]   # (UpgradeError code, ASCII text); empty = can run
    live_roots: tuple[Path, ...] = ()       # the live folders (and xgs twins) checked against


@dataclass(frozen=True)
class UpgradeProgress:
    stage: str                        # "copy", "verify", "commit"
    files_done: int
    files_total: int
    bytes_done: int
    bytes_total: int


@dataclass(frozen=True)
class UpgradeResult:
    copy_aside: str | None            # the copy's folder name (never a path)
    files: int
    bytes: int
    fold_waiting_for_space: int       # bytes still needed on the vault drive, 0 if none
    message: str                      # ASCII, for the owner


def is_pre_upgrade_copy(folder: str | Path) -> bool:
    return (Path(folder) / catalog.PRE_UPGRADE_COPY).is_file()


def upgrade_needed(vault_root: str | Path) -> bool:
    """For the front ends (a seam; nothing upgrades by itself): the folder is a vault in the
    old layout that the upgrade dialog may offer. Reads only."""
    root = Path(vault_root)
    return (catalog.looks_like_vault_dir(root) and vault_mode(root) == "legacy"
            and not is_pre_upgrade_copy(root))


def tree(vault_root: Path) -> dict[str, tuple[int, int]]:
    """relpath -> (size, mtime_ns) of every file of the vault, ``store/`` and the organizer's
    lock files (no data, possibly held) excluded."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(vault_root):
        here = Path(dirpath)
        if here == vault_root and STORE_DIR in dirnames:
            dirnames.remove(STORE_DIR)
        for name in filenames:
            p = here / name
            rel = p.relative_to(vault_root).as_posix()
            if rel.startswith("organizer/") and name.endswith(".lock"):
                continue
            st = p.stat()
            out[rel] = (st.st_size, st.st_mtime_ns)
    return out


def _fingerprint(files: dict[str, tuple[int, int]]) -> str:
    lines = sorted(f"{rel}|{size}|{mtime}" for rel, (size, mtime) in files.items())
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class _Assessment:
    files: dict[str, tuple[int, int]]
    total: int
    fingerprint: str
    free: int
    needed: int
    deepest: int
    copy: bool
    problems: tuple[tuple[str, str], ...]


def plan_upgrade(vault_root: str | Path, *, dest_parent: str | Path | None = None,
                 live_roots: Sequence[Path] | None = None,
                 now: Callable[[], datetime] = utc_now) -> UpgradePlan:
    """What the upgrade would do, and every reason it cannot run now. Reads only."""
    root = Path(vault_root)
    stamp = now().astimezone(timezone.utc)
    parent = Path(dest_parent) if dest_parent is not None else root.parent
    dest = parent / f"{root.name}{COPY_INFIX}{stamp:%Y%m%dT%H%M%SZ}"
    live = tuple(discover.live_folders(live_roots if live_roots is not None
                                       else discover.all_live_paths(None)))
    a = _assess(root, dest, live)
    problems = list(a.problems)
    if _upgrade_lock_held(root):
        problems.append(("in-progress", "The vault is being upgraded by another NMS Save Vault "
                                        "window."))
    return UpgradePlan(root, len(a.files), a.total, a.fingerprint, dest, a.free, a.needed,
                       a.deepest, a.copy, tuple(problems), live)


def _assess(root: Path, dest: Path, live: Sequence[Path]) -> _Assessment:
    """The facts a plan rests on, checked again by the run under its lock. Reads only."""
    if not root.is_dir() or not catalog.looks_like_vault_dir(root):
        return _Assessment({}, 0, _fingerprint({}), 0, 0, 0, False,
                           (("not-a-vault", "That folder is not a vault."),))
    problems: list[tuple[str, str]] = []
    mode = vault_mode(root)
    if mode == "store":
        problems.append(("already-upgraded", "The vault is already upgraded."))
    elif mode == "upgrading":
        problems.append(("in-progress", "The vault is being upgraded by another NMS Save Vault "
                                        "window, or the last upgrade was interrupted."))
    elif store_root(root).exists():
        problems.append(("damaged", "store folder present without FORMAT - run fsck"))
    if is_pre_upgrade_copy(root):
        problems.append(("bad-destination", "This is the pre-upgrade copy of a vault; use Undo "
                                            "the vault upgrade."))
    files = tree(root)
    total = sum(size for size, _ in files.values())
    copy = bool(catalog.Vault(root, read_only=True).load().entries)
    parent = dest.parent
    try:
        free = shutil.disk_usage(parent if parent.exists() else parent.parent).free
    except OSError:
        free = 0
    needed = total + GIB
    deepest = len(str(dest)) + len(PARTIAL) + 1 + max((len(r) for r in files), default=0)
    if copy:
        if free < needed:
            problems.append(("space", f"The upgrade needs {_gb(needed)} free on the drive of the "
                                      f"copy for a copy of the vault; there are {_gb(free)}. "
                                      "Free some space or choose another folder for the copy."))
        if deepest >= MAX_PATH:
            problems.append(("path-too-long", "The copy would need paths longer than Windows "
                                              "allows. Choose a folder with a shorter path."))
        partial = dest.with_name(dest.name + PARTIAL)
        if _inside(dest, root) or discover.is_inside(dest, live) or dest.exists() \
                or partial.exists() or not parent.is_dir():
            problems.append(("bad-destination", "Choose a folder outside the vault and outside "
                                                "the game's save folders for the copy."))
    return _Assessment(files, total, _fingerprint(files), free, needed, deepest, copy,
                       tuple(problems))


def run_upgrade(plan: UpgradePlan, *,
                vault_lock_factory: Callable[[], AbstractContextManager] | None = None,
                progress: Callable[[UpgradeProgress], None] | None = None,
                cancel: threading.Event | None = None,
                now: Callable[[], datetime] = utc_now) -> UpgradeResult:
    """Copy the vault aside, verify the copy, then turn store mode on (D3 4.1 steps 1-7).
    Under the upgrade lock the plan is checked again (it must still hold, and the vault must
    still be what was planned). Raises UpgradeError; the vault is then as it was: only what
    this run made (its copy, its ``store/``) is removed, before the lock is released."""
    if plan.problems:
        code, text = plan.problems[0]
        raise UpgradeError(code, text)
    root = plan.vault_root
    lock = vault_lock_factory or vault_lock(root)
    partial = plan.dest.with_name(plan.dest.name + PARTIAL)
    held = safety.exclusive_lock(upgrade_lock_path(root), timeout=0)
    try:
        held.__enter__()
    except (safety.LockTimeout, RuntimeError) as exc:
        raise UpgradeError("in-progress", "The vault is being upgraded by another NMS Save Vault "
                                          "window.") from exc
    owned = committed = False
    try:
        fresh = _assess(root, plan.dest, plan.live_roots)
        if fresh.problems:
            raise UpgradeError(*fresh.problems[0])
        if (fresh.fingerprint, fresh.copy) != (plan.fingerprint, plan.copy):
            raise UpgradeError("vault-changing", "The vault changed after the upgrade was "
                                                 "planned. Nothing was changed; start it again.")
        store_root(root).mkdir()                 # the vault exists: never parents=True here
        owned = True
        (store_root(root) / STATE).mkdir()
        with lock():
            _write_json(store_root(root) / STATE / UPGRADE_JSON, {
                "state": "copying", "partial": str(partial), "dest": str(plan.dest),
                "store_created": True, "started_utc": iso_utc(now()),
                "fingerprint": plan.fingerprint})
        result = _Run(plan, partial, lock, progress, cancel, now).copy_and_commit()
        committed = True
        return result
    except UpgradeError:
        raise
    except OSError as exc:
        raise UpgradeError("copy-failed", f"The upgrade stopped: could not copy "
                                          f"{getattr(exc, 'filename', '') or 'a file'} "
                                          f"({exc.strerror or exc}). Your vault was not changed.") from exc
    finally:
        try:
            if not committed:
                _remove_unfinished(root, partial, plan.dest, store_owned=owned)
        finally:
            held.__exit__(None, None, None)


class _Run:
    def __init__(self, plan: UpgradePlan, partial: Path, lock, progress, cancel, now) -> None:
        self.plan = plan
        self.root = plan.vault_root
        self.partial = partial
        self.lock = lock
        self.progress = progress
        self.cancel = cancel
        self.now = now
        self.copied: dict[str, tuple[int, int, str]] = {}    # rel -> (size, mtime_ns, sha256)

    def copy_and_commit(self) -> UpgradeResult:
        if not self.plan.copy:
            with self.lock():
                self._commit(None, 0)
            return UpgradeResult(None, 0, 0, 0, "Vault upgraded.")
        self.partial.mkdir()                     # its parent was checked; never parents=True
        files = tree(self.root)
        self._sync(files, stage="copy")
        for _round in range(MAX_ROUNDS + 1):
            now = tree(self.root)
            if self._settled(now):
                waiting = self._fold_shortfall()
                with self.lock():
                    if self._settled(tree(self.root)):
                        info = self._commit_copy(waiting)
                        return UpgradeResult(info["name"], info["files"], info["bytes"], waiting,
                                             _done_text(info, waiting))
                continue
            if _round == MAX_ROUNDS:
                break
            self._sync(now, stage="copy")
        raise UpgradeError("vault-changing", VAULT_CHANGING)

    def _settled(self, now: dict[str, tuple[int, int]]) -> bool:
        return now == {rel: (size, mtime) for rel, (size, mtime, _sha) in self.copied.items()}

    def _sync(self, files: dict[str, tuple[int, int]], *, stage: str) -> None:
        """Copy what is new or changed since the last round (all of it the first time), drop
        what is gone, then verify every copied file by reading it back."""
        todo = [rel for rel, stat in sorted(files.items())
                if rel not in self.copied or self.copied[rel][:2] != stat]
        for rel in [r for r in self.copied if r not in files]:
            (self.partial / rel).unlink()
            del self.copied[rel]
        total = sum(files[rel][0] for rel in todo)
        done_bytes = 0
        for n, rel in enumerate(todo, start=1):
            if self.cancel is not None and self.cancel.is_set():
                raise UpgradeError("cancelled", "Upgrade cancelled. Your vault was not changed.")
            self.copied[rel] = self._copy(rel)
            done_bytes += self.copied[rel][0]
            self._report(stage, n, len(todo), done_bytes, total)
        for n, rel in enumerate(todo, start=1):
            size, _mtime, sha = self.copied[rel]
            got = self.partial / rel
            if got.stat().st_size != size or safety.sha256_file(got) != sha:
                raise UpgradeError("copy-mismatch", f"The upgrade stopped: the copy did not match "
                                                    f"the vault ({rel}). Your vault was not "
                                                    "changed. Check the drive.")
            self._report("verify", n, len(todo), 0, 0)

    def _copy(self, rel: str) -> tuple[int, int, str]:
        src, dst = self.root / rel, self.partial / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        before = os.stat(src)
        h = hashlib.sha256()
        size = 0
        with open(src, "rb") as fi, open(dst, "wb") as fo:
            for block in iter(lambda: fi.read(BLOCK), b""):
                h.update(block)
                fo.write(block)
                size += len(block)
            fo.flush()
            os.fsync(fo.fileno())
        os.utime(dst, ns=(before.st_atime_ns, before.st_mtime_ns))
        return size, before.st_mtime_ns, h.hexdigest()

    def _fold_shortfall(self) -> int:
        """What the fold will add to the vault drive (distinct contents of object-sized files
        in the managed folders, plus a margin) beyond its free space; 0 when it fits."""
        distinct = {}
        for rel, (size, _mtime, sha) in self.copied.items():
            if rel.split("/", 1)[0] in MANAGED_DIRS and size > INLINE_MAX:
                distinct[sha] = size
        needed = sum(distinct.values()) + SPACE_MARGIN
        try:
            free = shutil.disk_usage(self.root).free
        except OSError:
            return needed
        return max(0, needed - free)

    def _commit_copy(self, waiting: int) -> dict:
        """Under the vault lock: the copy gets its final name and its note, the migration
        record is written, and store mode goes on (FORMAT last)."""
        dest = self.plan.dest
        _rename_retrying(self.partial, dest)
        lines = sorted(f"{rel}|{size}|{sha}" for rel, (size, _m, sha) in self.copied.items())
        info = {"name": dest.name, "sibling": dest.parent == self.root.parent,
                "files": len(self.copied), "bytes": sum(s for s, _m, _h in self.copied.values()),
                "tree_sha256": hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest(),
                "verified_utc": iso_utc(self.now())}
        (dest / catalog.PRE_UPGRADE_COPY).write_bytes(_note_text(self.root.name, info).encode("ascii"))
        self._commit(info, waiting)
        return info

    def _commit(self, copy_aside: dict | None, waiting: int) -> None:
        state_dir = store_root(self.root) / STATE
        fold = {"state": "waiting-for-space" if waiting else "pending",
                "waiting_for_space_bytes": waiting, "entries": {}}
        _write_json(state_dir / MIGRATION_JSON, {"copy_aside": copy_aside, "fold": fold})
        create_store(self.root, clock=self.now)                  # COMMIT POINT: store mode on
        (state_dir / UPGRADE_JSON).unlink()
        safety.fsync_dir(state_dir)

    def _report(self, stage: str, done: int, total: int, done_bytes: int, total_bytes: int) -> None:
        if self.progress is not None:
            self.progress(UpgradeProgress(stage, done, total, done_bytes, total_bytes))


def cleanup_interrupted_upgrade(vault_root: str | Path) -> UpgradeResult | None:
    """After a crash or power loss during an upgrade (the front ends call it when they open a
    vault): one that had reached its commit point is finished; one that had not is cleaned up -
    only what it made itself, as ``upgrade.json`` records it (its ``.partial`` copy or the
    renamed copy, and a ``store/`` it created), never vault data. None when there is nothing
    to do, or another window is upgrading."""
    root = Path(vault_root)
    marker = store_root(root) / STATE / UPGRADE_JSON
    if not marker.is_file():
        return None
    try:
        with safety.exclusive_lock(upgrade_lock_path(root), timeout=0):
            try:
                record = json.loads(marker.read_bytes().decode("utf-8"))
            except (OSError, ValueError):
                record = {}
            if record.get("state", "copying") != "copying":
                return None                                      # a rollback's (D3 4.4)
            if (store_root(root) / FORMAT_NAME).is_file():
                marker.unlink()                                  # it had committed
                return UpgradeResult(None, 0, 0, 0, "The last upgrade had finished.")
            if not record.get("store_created"):
                return UpgradeResult(None, 0, 0, 0, "store folder present without FORMAT - run "
                                                    "fsck. Nothing was removed.")
            partial, dest = _recorded_copy(record)
            _remove_unfinished(root, partial, dest, store_owned=True)
    except (safety.LockTimeout, RuntimeError):
        return None
    return UpgradeResult(None, 0, 0, 0, "The last upgrade was interrupted and has been cleaned "
                                        "up. Your vault was not changed.")


def startup_notice(vault_root: str | Path) -> str | None:
    """For the front ends when they open a vault: clean up after an interrupted upgrade and
    say so; None when there was nothing to do."""
    result = cleanup_interrupted_upgrade(vault_root)
    return result.message if result is not None else None


# --- helpers ----------------------------------------------------------------------------------


def upgrade_lock_path(vault_root: str | Path) -> Path:
    """The lock one upgrade (or one undo of it) holds for its whole run. Beside D1's locks in
    ``organizer/``, never inside ``store/``: a failed run removes ``store/`` while it still
    holds the lock."""
    return Path(vault_root) / "organizer" / UPGRADE_LOCK


def _recorded_copy(record: dict) -> tuple[Path | None, Path | None]:
    """The copy folders an upgrade.json names, when they look like ours: ``<dest>.partial`` and
    ``<vault>.pre-store-<UTC>``."""
    dest = Path(str(record.get("dest") or ""))
    partial = Path(str(record.get("partial") or ""))
    if dest.name.endswith(PARTIAL):              # an upgrade.json of the first version
        dest, partial = dest.with_name(dest.name[:-len(PARTIAL)]), dest
    ok_dest = COPY_INFIX in dest.name and partial == dest.with_name(dest.name + PARTIAL)
    return (partial if ok_dest else None), (dest if ok_dest else None)


def _remove_unfinished(root: Path, partial: Path | None, dest: Path | None, *,
                       store_owned: bool) -> None:
    """After a failed or interrupted upgrade, while store mode never came on: the run's own
    copy (unfinished, or renamed but not committed, note and all) and the ``store/`` it made."""
    if (store_root(root) / FORMAT_NAME).is_file():
        return
    folders = [f for f in (partial, dest) if f is not None]
    if store_owned:
        folders.append(store_root(root))
    for folder in folders:
        if folder.exists():
            try:
                shutil.rmtree(folder)
            except OSError as exc:
                log.warning("could not remove %s after the upgrade stopped: %s", folder.name, exc)


def _upgrade_lock_held(root: Path) -> bool:
    lock_file = upgrade_lock_path(root)
    if not lock_file.exists():
        return False
    try:
        with safety.exclusive_lock(lock_file, timeout=0):
            return False
    except (safety.LockTimeout, RuntimeError):
        return True


def _rename_retrying(src: Path, dest: Path) -> None:
    for attempt in range(5):
        try:
            os.rename(src, dest)
            return
        except PermissionError:         # a virus scanner or the indexer has a file open
            if attempt == 4:
                raise
            time.sleep(0.2)


def _write_json(path: Path, body: dict) -> None:
    raw = json.dumps(body, indent=1, sort_keys=True, ensure_ascii=True).encode("ascii")
    safety.atomic_write_bytes(path, raw, temp_prefix=f".{path.stem}.tmp_")


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _gb(n: int) -> str:
    return f"{n / GIB:.1f} GB"


def _note_text(vault_name: str, info: dict) -> str:
    return (
        "NMS Save Vault - copy of a vault made before its upgrade\r\n\r\n"
        f"This folder is a copy of the vault '{vault_name}' as it was before it was upgraded to\r\n"
        f"the version store, made and checked file by file at {info['verified_utc']}\r\n"
        f"({info['files']} files, {_gb(info['bytes'])}).\r\n\r\n"
        "It is not a vault to open and not a backup folder to import. Vault > Undo the vault\r\n"
        "upgrade uses it to put the vault back in the old layout. Keep it until you are happy\r\n"
        "with the upgraded vault, then delete it yourself.\r\n")


def _done_text(info: dict, waiting: int) -> str:
    text = (f"Vault upgraded. A copy of the vault as it was is in {info['name']} next to it "
            f"({_gb(info['bytes'])}). Keep it until you are happy with the new version and "
            "delete it yourself when you no longer need it; Vault > Undo the vault upgrade "
            "uses it.")
    if waiting:
        text += (f" Your old backups were not read into the store yet: it needs {_gb(waiting)} "
                 "more free space on the vault drive.")
    return text
