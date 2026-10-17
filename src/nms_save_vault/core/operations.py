"""High-level, safety-wrapped operations on save folders.

Every operation that writes into a live folder: (1) refuses if the game is running
(unless explicitly overridden), (2) auto-snapshots the live save state first, (3) stages
+ validates + writes atomically, and (4) appends an op-log record enabling undo.

Re-slotting copies the save *data* verbatim and only re-keys the small meta; promoting a
member only edits the meta timestamp. The data bytes are never recompressed.
"""
from __future__ import annotations

import functools
import inspect
import logging
import os
import threading
import shutil
import struct
import warnings as pywarnings
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

from . import formats, lz4_block, meta, msstore, playtime, safety, savedir
from .catalog import (
    KIND_EXTRACT,
    KIND_FULL,
    KIND_IMPORTED,
    KIND_INPLACE,
    KIND_SNAPSHOT,
    CatalogEntry,
    OplogRead,
    Vault,
    looks_like_vault_dir,
)
from .slotmap import SaveFileRef, slot_file_numbers, storage_ordinal

log = logging.getLogger(__name__)

# Free space a new vault copy must leave on the vault drive beyond its own size, so a copy
# never fills the drive the catalog still has to be written to.
SPACE_MARGIN = 100 * 1024 * 1024


class OperationError(Exception):
    pass


class GameRunningError(OperationError):
    pass


class ValidationError(OperationError):
    pass


class WrittenButInvalidError(ValidationError):
    """The live folder was written, but the result failed its check. Carries the operation's
    result (``ok`` False) so the caller still has its snapshot id and warnings for undo."""

    def __init__(self, message: str, result: "OpResult"):
        super().__init__(message)
        self.result = result


class SourceChangedError(OperationError):
    """A file changed (``vanished`` False) or disappeared (True) while it was being copied
    into the vault. From a live folder (``live``) the game or the Xbox app was most likely
    writing; from anywhere else the folder changed or became unavailable (a removed drive)."""

    def __init__(self, name: str, vanished: bool, live: bool):
        what = "disappeared" if vanished else "changed"
        if live:
            hint = "Close the game (or let the Xbox app finish syncing) and try again."
        else:
            hint = "Check that the source folder is still there and unchanged (a drive removed?) and try again."
        super().__init__(
            f"The file {name} {what} while it was being copied into the vault, so nothing was "
            f"saved. {hint}"
        )
        self.name = name
        self.vanished = vanished
        self.live = live


class ForeignAccountError(OperationError):
    """A backup of another account was refused: restoring it would write that account's
    identity into this one. Nothing was changed."""


class OplogNotWrittenWarning(UserWarning):
    """An operation that returns no OpResult (backup, extract, import) finished, but its
    op-log record could not be written. Issued with ``warnings.warn`` only when the caller
    passed no ``warnings`` list to collect it in."""


class FeatureNotYetAvailableError(OperationError):
    """A deliberately-gated capability the UI should present as 'coming soon',
    not as an error/failure."""


# Shown when a write would transfer a save between Steam and Xbox (either direction).
# Same-platform writes (Steam->Steam, Xbox->Xbox) are supported; cross-platform is gated.
CROSS_PLATFORM_MSG = "Transferring saves between Steam and Xbox / Game Pass is coming soon."


@dataclass
class OpResult:
    ok: bool
    op: str
    detail: str
    snapshot_id: str | None = None
    entry_id: str | None = None
    changed: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


LIVE_OP_BUSY = (
    "Another NMS Save Vault window is changing live saves. Try again when it has finished."
)

# The game check of the write in progress on this thread (_guard_game sets it): the hook
# journals the live slots only when the game is known not to be running.
_game_check = threading.local()


def _recorded(kind: str, slot_arg: str | None = None):
    """The organizer's phase-1 hook (D1 2.6.7) around a live write: the write runs under the
    live-op lock, so two windows never write one live folder at once, and is journaled as an
    operation on the slot ``slot_arg`` names (or on every slot a whole-folder restore
    changed). Recording never fails the write; only a busy lock refuses it."""
    def wrap(fn):
        signature = inspect.signature(fn)

        @functools.wraps(fn)
        def run(*args, **kwargs):
            from .organizer import recorder   # the organizer reads core modules, not this one

            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            arguments = bound.arguments
            vault = arguments["vault"]
            try:
                with recorder.classic_write(vault.root, Path(arguments["live_dir"]), kind) as hook:
                    if hook is None:                  # part of an outer write
                        return fn(*args, **kwargs)
                    _game_check.state = None          # unknown until the write checks
                    record = functools.partial(
                        hook.done, slot=arguments[slot_arg] if slot_arg else None,
                        source=lambda: _op_source(vault, arguments))
                    try:
                        result = fn(*args, **kwargs)
                    except BaseException as exc:
                        # A write that failed after changing the folder is still recorded.
                        record(getattr(exc, "result", None), game_running=_game_check.state,
                               error=exc)
                        raise
                    record(result, game_running=_game_check.state)
                    return result
            except recorder.LiveOpBusy as exc:
                raise OperationError(LIVE_OP_BUSY) from exc

        return run

    return wrap


def _op_source(vault: Vault, arguments: dict) -> dict | None:
    """Where a write took its saves from, by entry id or folder name (never a path)."""
    if isinstance(arguments.get("entry"), CatalogEntry):
        return {"kind": "entry", "entry": arguments["entry"].id}
    source_dir = arguments.get("source_dir")
    if source_dir is None:
        return None
    slot = arguments.get("source_slot")
    try:
        here = Path(source_dir).resolve()
        for entry in vault.entries:
            if Path(entry.path).resolve() == here:
                return {"kind": "entry", "entry": entry.id, "slot": slot}
    except OSError:
        pass
    return {"kind": "folder", "name": Path(source_dir).name, "slot": slot}


# --- file-set helpers --------------------------------------------------------


def _hg_files(folder: Path) -> list[Path]:
    """All save-state files (slot saves + account), data and meta."""
    return sorted(p for p in folder.glob("*.hg") if p.is_file())


def _member_paths(folder: Path, file_no: int) -> tuple[Path, Path]:
    ref = SaveFileRef(file_no)
    return folder / ref.data_name, folder / ref.meta_name


def _guard_game(allow_game_running: bool, warnings: list[str]) -> None:
    running = safety.is_game_running()
    _game_check.state = running    # what the organizer hook may journal depends on it
    if running is True and not allow_game_running:
        raise GameRunningError("No Man's Sky appears to be running. Close the game first.")
    if running is None:
        warnings.append("Could not determine whether the game is running.")


def _is_xbox(path: Path) -> bool:
    return savedir.platform_of(path) == "xbox"


def _steam_cloud_on(folder: Path) -> bool:
    """True when Steam Cloud is syncing this folder (it carries steam_autocloud.vdf)."""
    return (Path(folder) / formats.STEAM_AUTOCLOUD).is_file()


def _guard_same_platform(src: Path, dst: Path) -> None:
    """Cross-platform (Steam<->Xbox) transfer is gated; same-platform is supported."""
    if savedir.platform_of(src) != savedir.platform_of(dst):
        raise FeatureNotYetAvailableError(CROSS_PLATFORM_MSG)


def _now() -> float:
    return datetime.now().timestamp()


def _check_slot(slot: int) -> None:
    if not 1 <= slot <= formats.MAX_SAVE_SLOTS:
        raise OperationError(f"slot {slot} is out of range (1-{formats.MAX_SAVE_SLOTS})")


# --- snapshots & copying -----------------------------------------------------


def _save_state_files(src: Path) -> list[Path]:
    """The files that make up the live save state. Steam: all *.hg (+ steam_autocloud.vdf).
    Xbox/wgs: every file of the account folder (containers.index + every blob dir)."""
    if _is_xbox(src):
        return _all_files(src)
    vdf = src / formats.STEAM_AUTOCLOUD
    return _hg_files(src) + ([vdf] if vdf.is_file() else [])


def _all_files(folder: Path) -> list[Path]:
    return [p for p in folder.rglob("*") if p.is_file()]


class _Copier:
    """Copies files reading each source once, keeping the sha256 of what it read (to check
    the copy against) and noting a source whose size or time moved meanwhile. Usable as
    ``shutil.copytree``'s ``copy_function``."""

    def __init__(self) -> None:
        self.hashes: dict[str, str] = {}
        self.moved: set[str] = set()
        self.stats: dict[str, tuple[int, int]] = {}   # (size, mtime_ns) when it was copied

    def __call__(self, src, dst, *args, **kwargs):
        before = os.stat(src)
        digest = safety.copy_and_hash(src, dst)
        after = os.stat(src)
        key = _path_key(src)
        self.hashes[key] = digest
        self.stats[key] = (after.st_size, after.st_mtime_ns)
        if (before.st_size, before.st_mtime_ns) != self.stats[key]:
            self.moved.add(key)
        return dst


def _path_key(path) -> str:
    return os.path.normcase(os.path.abspath(path))


def _copy_save_state(src: Path, dst: Path, files: list[Path], copy: _Copier) -> None:
    """Copy the live save state (``files`` = :func:`_save_state_files`) into ``dst``. Xbox
    copies the whole tree, empty folders included: a mirror restore from this copy deletes
    whatever it lacks."""
    if _is_xbox(src):
        shutil.copytree(src, dst, dirs_exist_ok=True, copy_function=copy)
    else:
        for p in files:
            copy(p, dst / p.name)


def _require_free_space(folder: Path, needed: int) -> None:
    free = shutil.disk_usage(folder).free
    if free < needed + SPACE_MARGIN:
        mb = 1024 * 1024
        raise OperationError(
            f"Not enough free space in the vault: this needs about {(needed + SPACE_MARGIN) // mb} MB "
            f"({needed // mb} MB of saves plus {SPACE_MARGIN // mb} MB to spare), and the vault "
            f"drive has {free // mb} MB free. Nothing was changed."
        )


def _remove_partial(folder: Path) -> None:
    """Delete a half-written vault folder. A failure is logged, not raised, so the caller
    still sees the error that made the copy fail."""
    try:
        shutil.rmtree(folder)
    except OSError as exc:
        log.warning("could not remove the incomplete vault folder %s: %s", folder, exc)


def _new_managed_entry(
    vault: Vault,
    kind: str,
    parent: Path,
    *,
    tag: str,
    source_root: Path,
    files: list[Path],
    fill: Callable[[Path, _Copier], None],
    label: str,
    source: str,
    slots: list[int] | None = None,
    live: bool = False,
) -> CatalogEntry:
    """Copy into a new managed vault folder and catalogue it -- or leave nothing behind.

    The folder is always a fresh one (never a leftover of an earlier failure, whose contents
    are unknown); ``fill`` copies ``files`` into it through the given copier, each to its
    place relative to ``source_root``. The copier reads each source once and keeps its
    sha256; every copy is then checked against it, and a source that changed meanwhile is
    caught too. If the copy, the check, the scan or the catalog write fails, the
    half-written folder is removed and the error re-raised: a partial, corrupt or
    uncatalogued copy is not a backup and must never be mistaken for one. ``live``: the
    source is a live save folder (it decides what a changed-source error advises).
    """
    vault.ensure()
    try:
        needed = sum(p.stat().st_size for p in files)
    except FileNotFoundError as exc:
        raise SourceChangedError(Path(exc.filename or "").name, vanished=True, live=live) from exc
    _require_free_space(parent, needed)
    entry_id = vault.new_id(kind, tag=tag, folder=parent)
    dest = parent / entry_id
    dest.mkdir()
    try:
        copier = _Copier()
        fill(dest, copier)
        _verify_copy(source_root, dest, files, copier, live)
        entry = vault.make_entry_from_dir(
            entry_id, kind, label, dest, managed=True, source=source, slots=slots
        )
        vault.upsert(entry)
    except BaseException as exc:
        _remove_partial(dest)
        gone = next((f for f in files if not f.exists()), None)
        if isinstance(exc, OSError) and gone is not None:
            raise SourceChangedError(gone.name, vanished=True, live=live) from exc
        raise
    return entry


def snapshot_live(vault: Vault, live_dir: Path, reason: str) -> CatalogEntry:
    """Copy the live save state into the vault as an auto-snapshot entry."""
    live_dir = Path(live_dir)
    files = _save_state_files(live_dir)
    return _new_managed_entry(
        vault, KIND_SNAPSHOT, vault.snapshots_dir, tag=savedir.platform_of(live_dir),
        source_root=live_dir, files=files,
        fill=lambda dest, copy: _copy_save_state(live_dir, dest, files, copy),
        label=f"auto: {reason}", source=live_dir.name, live=True,
    )


# --- feature 1: full backup / restore ---------------------------------------


def create_full_backup(
    vault: Vault, live_dir: Path, label: str = "", include_cache: bool = True,
    warnings: list[str] | None = None,
) -> CatalogEntry:
    """Copy the live folder into the vault. ``warnings`` collects anything the caller should
    show (today: an op-log record that could not be written); without it such a message is
    issued as an :class:`OplogNotWrittenWarning`."""
    live_dir = Path(live_dir)
    files = _all_files(live_dir) if include_cache else _save_state_files(live_dir)
    collected: list[str] = []

    def fill(dest: Path, copy: _Copier) -> None:
        if include_cache:
            shutil.copytree(live_dir, dest, dirs_exist_ok=True, copy_function=copy)
        else:
            _copy_save_state(live_dir, dest, files, copy)

    entry = _new_managed_entry(
        vault, KIND_FULL, vault.backups_dir, tag=savedir.platform_of(live_dir),
        source_root=live_dir, files=files, fill=fill, label=label, source=live_dir.name, live=True,
    )
    _log_op(vault, _oplog("full_backup", entry_id=entry.id), collected)
    _hand_over(collected, warnings)
    from .organizer import recorder

    recorder.observe_folder(vault.root, live_dir)   # a look at the folder narrows the journal's gaps
    return entry


def _hand_over(collected: list[str], warnings: list[str] | None) -> None:
    """Give a backup's, extract's or import's messages to the caller's list, or -- with no
    list to return them in -- issue them as :class:`OplogNotWrittenWarning`. Called by the
    public function itself, so the warning points at that function's caller."""
    if warnings is not None:
        warnings += collected
        return
    for message in collected:
        pywarnings.warn(OplogNotWrittenWarning(message), stacklevel=3)


def _verify_copy(src: Path, dst: Path, files: list[Path], copier: _Copier, live: bool) -> None:
    """Every file was copied, no source moved while it was read or afterwards (until the
    last file was copied -- so a data / meta pair cannot be split across a game write), and
    each copy hashes to what was read. Sources are only stat'ed again, never re-read."""
    for p in files:
        key = _path_key(p)
        try:
            now = p.stat()
        except FileNotFoundError:
            raise SourceChangedError(str(p.relative_to(src)), vanished=True, live=live) from None
        if key in copier.stats and (now.st_size, now.st_mtime_ns) != copier.stats[key]:
            copier.moved.add(key)
    for p in files:
        rel = p.relative_to(src)
        key = _path_key(p)
        if key in copier.moved:
            raise SourceChangedError(str(rel), vanished=False, live=live)
        d = dst / rel
        if key not in copier.hashes or not d.is_file() or safety.sha256_file(d) != copier.hashes[key]:
            raise ValidationError(f"the copy of {rel} in the vault does not match the original")


def restore_entry(
    vault: Vault, entry: CatalogEntry, live_dir: Path, allow_game_running: bool = False,
    restore_accountdata: bool = False,
) -> OpResult:
    """Put a catalog entry back into the live folder, the right way for its kind.

    A single-slot **extract** is not a picture of a whole save folder -- it is one slot
    lifted aside. Restoring it means putting that slot back where it came from and leaving
    every other slot alone. Running it through :func:`restore_full` instead would mirror
    the live folder onto a one-slot backup and delete every other save in it, which is
    exactly the accident this function exists to prevent.
    """
    if entry.kind != KIND_EXTRACT:
        return restore_full(
            vault, entry, live_dir, allow_game_running=allow_game_running,
            restore_accountdata=restore_accountdata,
        )

    slots = [s.slot for s in entry.occupied_slots]
    if not slots:
        raise OperationError(f"'{entry.id}' contains no saves to restore")
    if len(slots) > 1:
        raise OperationError(
            f"'{entry.id}' is an extract covering slots {slots}; restore them one at a time "
            "with repopulate, choosing the destination slot for each."
        )
    slot = slots[0]
    result = repopulate_slot(
        vault, Path(entry.path), slot, live_dir, slot, allow_game_running=allow_game_running
    )
    return replace(
        result,
        op="restore_extract",
        detail=f"put slot {slot} back into live slot {slot} from {entry.id}",
    )


@_recorded("restore_full")
def restore_full(
    vault: Vault, entry: CatalogEntry, live_dir: Path, mirror: bool = True, allow_game_running: bool = False,
    *, undo_of: str | None = None, restore_accountdata: bool = False,
) -> OpResult:
    """Put a whole backup back into the live folder.

    What belongs to the live folder's own account stays: ``steam_autocloud.vdf`` (it holds
    the Steam account id) is never copied, an Xbox backup of another account is refused,
    and the account data (``accountdata.hg``; Xbox: the AccountData record) is kept unless
    ``restore_accountdata`` -- rolling it back rolls back unlocks and settings too.
    ``undo_of`` (used by :func:`undo_last`) records the restore as the undo of the op whose
    snapshot that was. A failure part-way is reported (``WrittenButInvalidError``), never
    hidden: the folder is then only partly restored, and undo puts it back.
    """
    warnings: list[str] = []
    op = "undo" if undo_of else "restore_full"
    _guard_game(allow_game_running, warnings)
    live_dir = Path(live_dir)
    src = Path(entry.path)
    if not src.is_dir():
        raise OperationError(f"backup folder not found: {src}")
    if mirror and entry.kind == KIND_EXTRACT:
        # Refused structurally, not just in the UI: mirroring the live folder onto a
        # single-slot extract deletes every other save (and the account data) in it.
        raise OperationError(
            f"'{entry.id}' is a single-slot extract, not a full backup. Restoring it as one "
            "would delete every other save in the live folder. Use restore_entry(), which "
            "puts the slot back where it came from."
        )
    _guard_same_platform(src, live_dir)
    if _is_xbox(live_dir):
        return _restore_full_xbox(vault, entry, src, live_dir, warnings, undo_of, restore_accountdata)
    if not _hg_files(src):
        raise OperationError(
            f"'{entry.id}' has no Steam-format save*.hg files, so it cannot be restored into a "
            "Steam folder."
        )

    snap = snapshot_live(vault, live_dir, reason=f"pre-restore of {entry.id}")
    changed: list[str] = []

    # Steam Cloud compares each local file against its own manifest, so a restored file
    # stamped with the backup's original mtime looks OLDER than the cloud copy and Steam
    # can overwrite the restore on the next launch. Under Cloud the write time is the
    # honest one; elsewhere the save keeps the age it had.
    keep_mtime = not _steam_cloud_on(live_dir)
    names = (formats.ACCOUNT_DATA_NAME, formats.ACCOUNT_META_NAME)
    # The live folder keeps its own account data -- when it has some to keep.
    keep_account = not restore_accountdata and (live_dir / formats.ACCOUNT_DATA_NAME).is_file()
    account = set(names) if keep_account else set()
    if keep_account and any(
        (src / n).is_file() and not _same_bytes(src / n, live_dir / n) for n in names
    ):
        warnings.append(ACCOUNT_DATA_KEPT)
    src_hg = {p.name for p in _hg_files(src)}
    failures: list[str] = []
    for p in _hg_files(src):
        if p.name in account:
            continue
        try:
            safety.atomic_copy(p, live_dir / p.name, preserve_mtime=keep_mtime)
            changed.append(p.name)
        except OSError as exc:
            _note_failure(failures, p.name, exc)

    if mirror:
        for p in _hg_files(live_dir):
            # The account data is never deleted: a backup without it is no reason to lose it.
            if p.name not in src_hg and p.name not in (formats.ACCOUNT_DATA_NAME, formats.ACCOUNT_META_NAME):
                try:
                    p.unlink()
                    changed.append(f"-{p.name}")
                except OSError as exc:
                    _note_failure(failures, p.name, exc)

    _validate_dir_metas(live_dir, warnings)
    _log_op(vault, _restore_record(op, entry, snap, changed, live_dir, undo_of, restore_accountdata), warnings)
    result = OpResult(True, op, f"restored {entry.id}", snapshot_id=snap.id, changed=changed, warnings=warnings)
    if failures:
        raise _partly_restored(failures, result)
    return result


ACCOUNT_DATA_KEPT = (
    "The backup's account data (unlocks, settings) was not restored: the live folder kept its "
    "own. Restoring it as well would roll those back."
)


def _same_bytes(a: Path, b: Path) -> bool:
    return a.is_file() and b.is_file() and a.read_bytes() == b.read_bytes()


def _restore_record(op, entry, snap, changed, live_dir, undo_of, restore_accountdata) -> dict:
    record = _oplog(op, entry_id=entry.id, snapshot_id=snap.id, changed=changed, target=live_dir, undo_of=undo_of)
    if restore_accountdata:
        record["with_accountdata"] = True   # so undoing this restore puts the account data back too
    return record


def _same_blobs(a, b) -> bool:
    """Whether two Xbox records hold the same data and meta bytes."""
    try:
        return all(
            x is not None and y is not None and x.read_bytes() == y.read_bytes()
            for x, y in ((a.data_file, b.data_file), (a.meta_file, b.meta_file))
        )
    except OSError:
        return False


def _note_failure(failures: list[str], name: str, exc: OSError) -> None:
    log.error("restore: could not replace %s in the live folder: %s", name, exc)
    failures.append(f"{name}: {exc}")


def _partly_restored(failures: list[str], result: OpResult) -> WrittenButInvalidError:
    shown = "; ".join(failures[:3]) + (f" and {len(failures) - 3} more" if len(failures) > 3 else "")
    return _written_but_invalid(
        f"The restore could not replace everything in the live folder ({shown}), so the folder "
        "is only partly restored.",
        result,
    )


# --- feature 2: extract a slot, repopulate a slot, promote a member ----------


def extract_slot(
    vault: Vault, source_dir: Path, slot: int, label: str = "", warnings: list[str] | None = None,
    live_dirs: Iterable[Path] = (),
) -> CatalogEntry:
    """Copy one slot into the vault. ``warnings`` as in :func:`create_full_backup`.
    ``live_dirs``: the caller's live folders, so a problem copying from one of them is
    explained as the game or the Xbox app writing, not as a missing drive."""
    source_dir = Path(source_dir)
    _check_slot(slot)
    collected: list[str] = []
    live = any(_same_folder(source_dir, Path(d)) for d in live_dirs)
    if _is_xbox(source_dir):
        entry = _extract_slot_xbox(vault, source_dir, slot, label, collected, live)
    else:
        entry = _extract_slot_steam(vault, source_dir, slot, label, collected, live)
    _hand_over(collected, warnings)
    return entry


def _extract_slot_steam(
    vault: Vault, source_dir: Path, slot: int, label: str, collected: list[str], live: bool
) -> CatalogEntry:
    files: list[Path] = []
    for fno in slot_file_numbers(slot):
        data, meta_file = _member_paths(source_dir, fno)
        if data.is_file() and meta_file.is_file():
            files += [data, meta_file]
    if not files:
        raise OperationError(f"slot {slot} has no saves to extract in {source_dir}")
    entry = _new_managed_entry(
        vault, KIND_EXTRACT, vault.extracts_dir, tag=savedir.platform_of(source_dir),
        source_root=source_dir, files=files,
        fill=lambda dest, copy: _copy_save_state(source_dir, dest, files, copy),
        label=label or f"slot {slot} from {source_dir.name}", source=source_dir.name, slots=[slot],
        live=live,
    )
    _log_op(vault, _oplog("extract_slot", entry_id=entry.id, detail=f"slot {slot}"), collected)
    return entry


@_recorded("repopulate", "dest_slot")
def repopulate_slot(
    vault: Vault,
    source_dir: Path,
    source_slot: int,
    live_dir: Path,
    dest_slot: int,
    allow_game_running: bool = False,
) -> OpResult:
    """Write the two saves of ``source_slot`` (from ``source_dir``) into ``dest_slot`` of
    the live folder, re-keying each meta when the slot number differs."""
    warnings: list[str] = []
    source_dir = Path(source_dir)
    live_dir = Path(live_dir)
    _check_slot(source_slot)
    _check_slot(dest_slot)
    _guard_same_platform(source_dir, live_dir)
    if _is_xbox(live_dir):
        return _repopulate_slot_xbox(vault, source_dir, source_slot, live_dir, dest_slot, allow_game_running)
    _guard_game(allow_game_running, warnings)

    # Pre-flight: build the (data_bytes, new_meta_bytes, dst paths, mtime) for each member.
    planned = []
    for member in (0, 1):
        src_f = slot_file_numbers(source_slot)[member]
        dst_f = slot_file_numbers(dest_slot)[member]
        src_data, src_meta = _member_paths(source_dir, src_f)
        if not (src_data.is_file() and src_meta.is_file()):
            warnings.append(f"source slot {source_slot}{'AB'[member]} missing; skipped")
            continue
        data_bytes = src_data.read_bytes()
        meta_bytes = src_meta.read_bytes()
        dst_ord = storage_ordinal(dst_f)
        used_ord, _plain = meta.decrypt_autodetect(meta_bytes, storage_ordinal(src_f))
        new_meta = meta_bytes if used_ord == dst_ord else meta.re_key(meta_bytes, used_ord, dst_ord)
        _validate_pair(new_meta, dst_ord, data_bytes)
        dst_data, dst_meta = _member_paths(live_dir, dst_f)
        planned.append((member, dst_data, data_bytes, dst_meta, new_meta, src_data.stat().st_mtime))

    if not planned:
        raise OperationError(f"source slot {source_slot} has no saves in {source_dir}")
    src_sv = savedir.scan_slot(source_dir, source_slot)
    dest_sv = savedir.scan_slot(live_dir, dest_slot)
    written = [p[0] for p in planned]
    _refuse_mixed_slot(source_slot, dest_slot, written, dest_sv, src_sv, savedir.platform_of(live_dir))
    expected = _expected_newest(src_sv, written)
    planned.sort(key=lambda p: "AB"[p[0]] == expected)         # the newer member last

    snap = snapshot_live(vault, live_dir, reason=f"pre-repopulate slot {dest_slot}")
    # See restore_full: under Steam Cloud the source mtime would make the cloud copy look
    # newer than what we just wrote, and Steam would put its copy back.
    keep_mtime = not _steam_cloud_on(live_dir)
    now = _now()
    changed: list[str] = []
    for member, dst_data, data_bytes, dst_meta, new_meta, mtime in planned:
        safety.atomic_write_bytes(dst_data, data_bytes)
        safety.atomic_write_bytes(dst_meta, new_meta)
        if not keep_mtime:
            # Under Cloud: the write time, the member that must load a second ahead -- the
            # file times then order the pair whatever order the writes landed in.
            mtime = now if "AB"[member] == expected else now - 1
        safety.set_file_mtime(dst_data, mtime)
        safety.set_file_mtime(dst_meta, mtime)
        changed += [dst_data.name, dst_meta.name]

    # Logged before the read-back check, so a failed check still leaves undo its snapshot.
    _log_op(
        vault,
        _oplog("repopulate_slot", snapshot_id=snap.id, detail=f"{source_dir.name} slot {source_slot} -> slot {dest_slot}", changed=changed, target=live_dir),
        warnings,
    )
    result = OpResult(True, "repopulate_slot", f"slot {source_slot} -> {dest_slot}", snapshot_id=snap.id, changed=changed, warnings=warnings)
    try:
        _validate_slot(live_dir, dest_slot, warnings)
    except ValidationError as exc:
        raise _written_but_invalid(f"{exc}.", result) from exc
    _require_newest(savedir.scan_slot(live_dir, dest_slot), expected, result)
    return result


def _refuse_mixed_slot(
    source_slot: int, dest_slot: int, written: list[int], dest_sv: savedir.SlotView,
    src_sv: savedir.SlotView, platform: str,
) -> None:
    """A one-member source must not go into a slot whose other member is a different -- or
    unidentifiable -- playthrough: that save would stay beside the copy, and the game could
    load it instead (P6). The same playthrough may stay; the copied member is then checked
    to be the one that loads."""
    if len(written) != 1:
        return
    leftover = dest_sv.members[1 - written[0]]
    if not leftover.exists:
        return
    try:
        stay_key, stay_known = playtime.identity(leftover, platform)
        new_key, new_known = playtime.identity(src_sv.members[written[0]], platform)
    except _READ_ERRORS:
        stay_known = new_known = False
    if stay_known and new_known and stay_key == new_key:
        return
    labels = formats.SAVE_TYPE_LABELS
    have, other = labels[written[0]], labels[1 - written[0]]
    raise OperationError(
        f"Source slot {source_slot} holds only its {have}, but live slot {dest_slot} also holds "
        f"a {other} of a different (or unidentifiable) playthrough that this copy would leave "
        "behind -- the game could then load that save instead of the copied one. Clear live "
        f"slot {dest_slot} first (it is snapshotted, so undo can bring it back), then copy "
        "again. Nothing was changed."
    )


def _expected_newest(src_sv: savedir.SlotView, written: list[int]) -> str:
    """The member label that must load after the copy: the source's newest of those written."""
    newest = src_sv.newest
    if newest is not None and "AB".index(newest.label) in written:
        return newest.label
    return "AB"[written[-1]]


def _require_newest(after: savedir.SlotView, expected: str, result: OpResult) -> None:
    """After a copy, the game must load the member the source would have loaded."""
    newest = after.newest
    if newest is None or newest.label != expected:
        raise _written_but_invalid(
            f"After the copy the game would load slot {after.slot}"
            f"{newest.label if newest else '?'} instead of the copied slot {after.slot}{expected}.",
            result,
        )


@_recorded("promote", "slot")
def promote_member(vault: Vault, live_dir: Path, slot: int, member: int, allow_game_running: bool = False) -> OpResult:
    """Force a slot member to be the one the game treats as newest (timestamp bump).

    The result is checked after the op-log record is written, so a failed check still
    leaves ``undo`` pointing at the pre-promote snapshot.
    """
    live_dir = Path(live_dir)
    _check_slot(slot)
    promote = _promote_member_xbox if _is_xbox(live_dir) else _promote_member_steam
    result, after = promote(vault, live_dir, slot, member, allow_game_running)
    if result.snapshot_id is None:              # already the newest: nothing was written
        return result
    newest = after.newest
    if newest is None or newest.label != "AB"[member]:
        raise _written_but_invalid(
            f"Slot {slot}{'AB'[member]} is still not the newest after promoting it.", result
        )
    return result


def _written_but_invalid(problem: str, result: OpResult) -> WrittenButInvalidError:
    return WrittenButInvalidError(
        f"{problem} The live folder was changed: undo restores it from snapshot "
        f"{result.snapshot_id}.",
        replace(result, ok=False),
    )


def _already_newest(sv: savedir.SlotView, member: int, own_time: float, sibling_time: float) -> bool:
    """True when the member already wins on every signal, so a promote has nothing to do:
    it is :attr:`SlotView.newest` and strictly ahead on :meth:`SlotView.order_time` (the
    ordering everything else uses), ahead on the meta save time wherever the sibling has
    one, and ahead on the file / index time (``own_time`` / ``sibling_time``)."""
    target, sibling = sv.members[member], sv.members[1 - member]
    if sv.newest is not target:
        return False
    if sibling.exists and not sv.order_time(target) > sv.order_time(sibling):
        return False                                   # a tie has no defined winner
    if sibling.timestamp and not target.timestamp > sibling.timestamp:
        return False
    return own_time > sibling_time


def _nothing_to_promote(slot: int, member: int, warnings: list[str]) -> OpResult:
    return OpResult(
        True, "promote_member",
        f"slot {slot}{'AB'[member]} is already the newest; nothing was changed", warnings=warnings,
    )


def _newest_file_time(*paths: Path) -> float:
    """The latest mtime among ``paths`` that exist; 0.0 when none does."""
    return max((p.stat().st_mtime for p in paths if p.is_file()), default=0.0)


def _promote_member_steam(
    vault: Vault, live_dir: Path, slot: int, member: int, allow_game_running: bool
) -> tuple[OpResult, savedir.SlotView]:
    warnings: list[str] = []
    _guard_game(allow_game_running, warnings)
    view = savedir.scan(live_dir)
    sv = view.slots[slot]
    target = sv.members[member]
    sibling = sv.members[1 - member]
    if not target.exists or not target.valid:
        raise OperationError(f"slot {slot}{'AB'[member]} is missing or unreadable")

    base = target.timestamp
    if sibling.exists and sibling.info is not None:
        base = max(base, sibling.timestamp)

    # A sibling without a meta time makes the file times order the pair (SlotView.order_time),
    # so the target's files must end up later than the sibling's as well -- and they never
    # move back in time. Under Steam Cloud they carry the write time: Cloud judges by file
    # time, and an older one would let its copy win on the next launch.
    own = _newest_file_time(target.data_path, target.meta_path)
    sibling_time = _newest_file_time(sibling.data_path, sibling.meta_path)  # even half-present
    if _already_newest(sv, member, own, sibling_time):
        return _nothing_to_promote(slot, member, warnings), sv
    mtime = max(own, sibling_time + 1 if sibling_time else 0.0)
    new_meta = None
    if base:
        new_ts = base + 1
        ordinal = target.ordinal_used or target.mapped_ordinal
        plain = meta.decrypt(target.meta_path.read_bytes(), ordinal)
        new_meta = meta.encrypt(meta.set_timestamp(plain, new_ts), ordinal)
        if not meta.is_valid_for(new_meta, ordinal):
            raise ValidationError("promote: re-encrypted meta failed validation")
        mtime = max(mtime, float(new_ts))
        moved = f"ts->{new_ts}"
    else:
        # Neither member has a meta save time, so the file times alone order the pair and
        # the meta is left as it is (a save time of 1 would read as 1970).
        moved = "file time only"
    if _steam_cloud_on(live_dir):
        mtime = max(mtime, _now())

    snap = snapshot_live(vault, live_dir, reason=f"pre-promote slot {slot}{'AB'[member]}")
    if new_meta is not None:
        safety.atomic_write_bytes(target.meta_path, new_meta)
    safety.set_file_mtime(target.meta_path, mtime)
    if target.data_path.is_file():
        safety.set_file_mtime(target.data_path, mtime)
    changed = [target.meta_path.name, target.data_path.name]
    _log_op(vault, _oplog("promote_member", snapshot_id=snap.id, detail=f"slot {slot}{'AB'[member]} {moved}", changed=changed, target=live_dir), warnings)
    result = OpResult(True, "promote_member", f"slot {slot}{'AB'[member]} promoted", snapshot_id=snap.id, changed=changed, warnings=warnings)
    return result, savedir.scan_slot(live_dir, slot)


# --- Xbox / Game Pass (wgs) variants -----------------------------------------
#
# Same-platform only: data + meta blobs are copied verbatim and the slot identity lives in
# containers.index (no XXTEA, no re-keying). All three snapshot the live folder first, so
# `undo` (which routes back through restore_full -> _restore_full_xbox) recovers the prior
# state. Cross-platform Steam<->Xbox transfer stays gated (see _guard_same_platform).


def _ms_identifier(slot: int, member: int) -> str:
    return f"Slot{slot}{'Auto' if member == 0 else 'Manual'}"


def _require_wgs(live_dir: Path) -> None:
    # An xgs-only account has no containers.index to keep the Xbox app's sync state in, so
    # writing it would be invisible to (or undone by) the cloud. Read-only until understood.
    if not msstore.is_microsoft_save_dir(live_dir):
        raise OperationError(f"{live_dir} is an xgs-only Xbox folder; the vault can read it but not write it")


def _validate_xbox_slot(live_dir: Path, slot: int, result: OpResult) -> savedir.SaveDirView:
    """Check the slot reads back after a write; returns the view it scanned. The write has
    happened, so a failure carries ``result`` (snapshot id, warnings) for undo."""
    view = savedir.scan_any(live_dir)
    present = view.slots[slot].present_members if slot in view.slots else []
    if not present:
        raise _written_but_invalid(f"Slot {slot} has no readable save after the write.", result)
    for m in present:
        if m.info is None:
            raise _written_but_invalid(
                f"Slot {slot}{m.label} is unreadable after the write ({m.note}).", result
            )
    return view


def _validate_xbox_dir(live_dir: Path, warnings: list[str]) -> None:
    view = savedir.scan_any(live_dir)
    for sv in view.slots.values():
        for m in sv.present_members:
            if m.info is None:
                warnings.append(f"slot {sv.slot}{m.label}: {m.note}")


def _promote_member_xbox(
    vault: Vault, live_dir: Path, slot: int, member: int, allow_game_running: bool
) -> tuple[OpResult, savedir.SlotView]:
    warnings: list[str] = []
    _require_wgs(live_dir)
    _guard_game(allow_game_running, warnings)
    view = savedir.scan_any(live_dir)
    sv = view.slots[slot]
    target, sibling = sv.members[member], sv.members[1 - member]
    if not target.exists or target.xbox is None:
        raise OperationError(f"slot {slot}{'AB'[member]} is missing or not an Xbox save")
    if not target.valid:
        raise OperationError(f"slot {slot}{'AB'[member]} is unreadable")
    if _already_newest(sv, member, target.data_mtime, sibling.data_mtime if sibling.exists else 0.0):
        return _nothing_to_promote(slot, member, warnings), sv
    data_bytes = Path(target.data_path).read_bytes()
    meta_bytes = Path(target.meta_path).read_bytes()
    # The index FILETIME is the record's local write time, so: now (R8 Q8) -- but never back
    # from its own time, and past the sibling's, so whichever clock orders the pair
    # (SlotView.order_time) the target wins.
    when = max(_now(), target.data_mtime + 1, sibling.data_mtime + 1 if sibling.exists else 0.0)
    if target.timestamp:
        # The meta save time moves too; a meta without one (280 bytes) is left as it is.
        new_ts = max(target.timestamp, sibling.timestamp if sibling.exists else 0) + 1
        try:
            meta_bytes = msstore.set_ms_meta_timestamp(meta_bytes, new_ts)
        except ValueError as exc:
            raise OperationError(
                f"slot {slot}{'AB'[member]} has a {len(meta_bytes)}-byte meta, a layout this "
                "version cannot write a save time into. Nothing was changed."
            ) from exc
        when = max(when, float(new_ts))
    else:
        new_ts = int(when)
    snap = snapshot_live(vault, live_dir, reason=f"pre-promote slot {slot}{'AB'[member]}")
    msstore.write_save(live_dir, target.xbox.identifier, data_bytes, meta_bytes, when)
    changed = [target.xbox.identifier]
    _log_op(vault, _oplog("promote_member", snapshot_id=snap.id, detail=f"xbox slot {slot}{'AB'[member]} ts->{new_ts}", changed=changed, target=live_dir), warnings)
    result = OpResult(True, "promote_member", f"slot {slot}{'AB'[member]} promoted", snapshot_id=snap.id, changed=changed, warnings=warnings)
    after = _validate_xbox_slot(live_dir, slot, result)
    return result, after.slots[slot]


def _extract_slot_xbox(
    vault: Vault, source_dir: Path, slot: int, label: str, collected: list[str], live: bool
) -> CatalogEntry:
    """Extract one wgs slot into the vault as a minimal, self-contained wgs folder: its own
    ``containers.index`` (just this slot's records) plus the slot's blob directories copied
    verbatim. That way it scans, summarises and repopulates exactly like any Xbox source."""
    info, containers = msstore.parse_containers_index(source_dir)
    wanted = {_ms_identifier(slot, 0), _ms_identifier(slot, 1)}
    picked = [c for c in containers if c.identifier in wanted and c.data_file and c.data_file.is_file()]
    if not picked:
        raise OperationError(f"slot {slot} has no saves to extract in {source_dir}")

    files = [p for c in picked for p in _all_files(c.directory)]

    def fill(dest: Path, copy: _Copier) -> None:
        for c in picked:                          # each GUID blob folder, verbatim
            shutil.copytree(c.directory, dest / c.dir_guid, copy_function=copy)
        # Rebuild the index from just the picked records; build uses their dir_guid (not
        # absolute paths), so the copied folders resolve under dest.
        subset = replace(info, trailer=b"")      # the source's trailing bytes are not ours
        safety.atomic_write_bytes(dest / "containers.index", msstore.build_containers_index(subset, picked))

    entry = _new_managed_entry(
        vault, KIND_EXTRACT, vault.extracts_dir, tag="xbox", source_root=source_dir, files=files, fill=fill,
        label=label or f"slot {slot} from {source_dir.name}", source=source_dir.name, slots=[slot],
        live=live,
    )
    _log_op(vault, _oplog("extract_slot", entry_id=entry.id, detail=f"xbox slot {slot}"), collected)
    return entry


def _repopulate_slot_xbox(vault: Vault, source_dir: Path, source_slot: int, live_dir: Path, dest_slot: int, allow_game_running: bool) -> OpResult:
    warnings: list[str] = []
    _require_wgs(live_dir)
    _guard_game(allow_game_running, warnings)
    src_view = savedir.scan_any(source_dir)
    src_sv = src_view.slots.get(source_slot)

    planned = []  # (source member, dest_identifier, data_bytes, meta_bytes)
    for member in (0, 1):
        sm = src_sv.members[member] if src_sv else None
        if sm is None or not sm.exists or sm.xbox is None or sm.info is None:
            warnings.append(f"source slot {source_slot}{'AB'[member]} missing or unreadable; skipped")
            continue
        data_bytes = Path(sm.data_path).read_bytes()
        meta_bytes = Path(sm.meta_path).read_bytes()
        planned.append((sm, _ms_identifier(dest_slot, member), data_bytes, meta_bytes))

    if not planned:
        raise OperationError(f"source slot {source_slot} has no readable saves in {source_dir}")

    # Index FILETIMEs are local write times: now (R8 Q8), never back from what either record of
    # the destination slot holds, and one second apart in the source's order, so the pair
    # keeps the order it had whichever clock orders it (SlotView.order_time).
    dest_sv = savedir.scan_any(live_dir).slots[dest_slot]
    written = [m for m in (0, 1) if any(p[0] is src_sv.members[m] for p in planned)]
    _refuse_mixed_slot(source_slot, dest_slot, written, dest_sv, src_sv, "xbox")
    expected = _expected_newest(src_sv, written)
    floor = max((m.xbox.last_write for m in dest_sv.members if m.xbox is not None), default=0.0)
    newest = max(_now(), floor + len(planned))
    planned.sort(key=lambda p: p[0].label == expected)      # the newer member last
    times = [newest - (len(planned) - 1 - i) for i in range(len(planned))]

    snap = snapshot_live(vault, live_dir, reason=f"pre-repopulate slot {dest_slot}")
    changed: list[str] = []
    for (_sm, dest_ident, data_bytes, meta_bytes), when in zip(planned, times):
        msstore.write_save(live_dir, dest_ident, data_bytes, meta_bytes, when, create_if_missing=True)
        changed.append(dest_ident)

    # Logged before the read-back check, so a failed check still leaves undo its snapshot.
    _log_op(vault, _oplog("repopulate_slot", snapshot_id=snap.id, detail=f"xbox {Path(source_dir).name} slot {source_slot} -> slot {dest_slot}", changed=changed, target=live_dir), warnings)
    result = OpResult(True, "repopulate_slot", f"slot {source_slot} -> {dest_slot}", snapshot_id=snap.id, changed=changed, warnings=warnings)
    after = _validate_xbox_slot(live_dir, dest_slot, result)
    _require_newest(after.slots[dest_slot], expected, result)
    return result


def _restore_full_xbox(
    vault: Vault, entry: CatalogEntry, src: Path, live_dir: Path, warnings: list[str], undo_of: str | None,
    restore_accountdata: bool,
) -> OpResult:
    op = "undo" if undo_of else "restore_full"
    _require_wgs(live_dir)
    if not (src / "containers.index").is_file():
        raise OperationError(f"'{entry.id}' is not an Xbox/wgs backup")
    if src.resolve() == live_dir.resolve():
        # Restoring a folder onto itself is a no-op; the mirror below would otherwise wipe it.
        return OpResult(True, "restore_full", f"'{entry.id}' is the live folder; nothing to do", warnings=warnings)
    try:
        src_info, src_records = msstore.parse_containers_index(src)
    except ValueError as exc:
        raise OperationError(f"'{entry.id}' has a damaged save index ({exc}), so it cannot be restored.") from exc
    try:
        live_info, live_records = msstore.parse_containers_index(live_dir)
    except ValueError as exc:
        # A restore is how a damaged folder gets repaired, so it must not be blocked here.
        # The account is then known only by the folder name the backup was taken of.
        if entry.source != live_dir.name:
            raise OperationError(
                f"The live save index is damaged ({exc}), and '{entry.id}' was not taken of "
                "this account's folder, so it cannot be confirmed to be the same account. "
                "Nothing was changed."
            ) from exc
        warnings.append(
            f"The live save index was damaged ({exc}); the backup's replaced it. The live "
            "account data could not be identified, so it could not be kept or checked, and "
            "live folders the backup does not hold were left in place."
        )
        live_info, live_records = None, []
    if live_info is not None and src_info.account_id != live_info.account_id:
        # The backup's containers.index carries its own account's identity and sync state.
        raise ForeignAccountError(
            f"'{entry.id}' was taken from another Xbox account. Restoring it would write that "
            "account's save index into this one, so it is refused. Nothing was changed."
        )
    live_account = next(
        (c for c in live_records if c.identifier == "AccountData" and c.data_file and c.data_file.is_file()),
        None,
    )
    src_account = next((c for c in src_records if c.identifier == "AccountData"), None)
    # The live account data stays -- when there is some to keep.
    keep_account = not restore_accountdata and live_account is not None
    # Folder names compare case-insensitively: a GUID may be spelt in either case on disk.
    keep: set[str] = set()           # live entries left exactly as they are
    if keep_account:
        keep = {c.dir_guid.casefold() for c in (live_account, src_account) if c is not None}
        if src_account is not None and not _same_blobs(src_account, live_account):
            warnings.append(ACCOUNT_DATA_KEPT)
    floors = {c.identifier: c.filetime for c in live_records}
    index_name = "containers.index"

    snap = snapshot_live(vault, live_dir, reason=f"pre-restore of {entry.id}")
    # Mirror the backup into the live folder (same account, so its containers.index identity
    # is valid here). Reconcile rather than clear-then-copy, so a failure part-way cannot
    # leave a half-emptied tree; every failure is collected and reported. The index is
    # written last and once, so it never names a folder that is not in place.
    src_names = {c.name.casefold() for c in src.iterdir()}
    changed: list[str] = []
    failures: list[str] = []
    if live_info is not None:                        # 1) drop live entries not in the backup
        # (with a damaged index nothing live can be identified, so nothing is deleted)
        for child in list(live_dir.iterdir()):
            name = child.name.casefold()
            if name not in src_names and name not in keep:
                try:
                    if child.is_dir():
                        shutil.rmtree(child)
                    else:
                        child.unlink()
                    changed.append(f"-{child.name}")
                except OSError as exc:
                    _note_failure(failures, child.name, exc)
    for child in src.iterdir():                      # 2) copy/overwrite the backup entries
        if child.name.casefold() in keep or child.name == index_name:
            continue
        dst = live_dir / child.name
        try:
            if child.is_dir():
                if dst.exists():
                    shutil.rmtree(dst)
                shutil.copytree(child, dst)
            else:
                shutil.copy2(child, dst)
            changed.append(child.name)
        except OSError as exc:
            _note_failure(failures, child.name, exc)

    position = src_records.index(src_account) if src_account is not None else 0
    try:                                             # 3) the index, once, then xgs
        index = msstore.restored_index(
            (src / index_name).read_bytes(), _now(), floors, live_info.filetime if live_info else 0,
            keep=("AccountData", live_account.raw) if keep_account else None, keep_position=position,
        )
        safety.atomic_write_bytes(live_dir / index_name, index)
        changed.append(index_name)
        changed += msstore.mirror_xgs(live_dir)
        _validate_xbox_dir(live_dir, warnings)
    except (OSError, ValueError) as exc:
        _note_failure(failures, f"{index_name} / xgs", exc)
    _log_op(vault, _restore_record(op, entry, snap, changed, live_dir, undo_of, restore_accountdata), warnings)
    result = OpResult(True, op, f"restored {entry.id}", snapshot_id=snap.id, changed=changed, warnings=warnings)
    if failures:
        raise _partly_restored(failures, result)
    return result


# --- feature 2b: clear a slot ------------------------------------------------
#
# Emptying a live slot is the one operation whose whole point is to destroy a save, so it
# is planned before it is done: plan_clear_slot() reports what the slot holds, the most
# recent copy of it in the vault, and how much play time the live save has gained since --
# i.e. exactly what would be lost. The front ends turn a non-empty ClearPlan.warning into
# an "are you sure?".


@dataclass
class ClearPlan:
    """What clearing one live slot would destroy, measured against the vault.

    Until versions have a lineage, only a byte-identical copy proves the save the game loads
    is safe: a copy of the same playthrough with more (or less) play time may be an earlier
    point or another branch. Such a copy is shown as information, never as proof.
    """

    slot: int
    occupied: bool
    live_name: str = ""
    live_play_time: int = 0
    entry_id: str | None = None
    entry_label: str = ""
    entry_kind: str = ""
    entry_created: str = ""
    backup_name: str = ""
    backup_play_time: int = 0
    steam_cloud: bool = False
    identical: bool = False          # the vault holds the very save the game loads (same bytes)
    unverifiable: bool = False       # no playthrough id and no identical copy: cannot tell
    notes: list[str] = field(default_factory=list)   # vault files that could not be checked

    @property
    def backed_up(self) -> bool:
        """The vault holds this very save (byte-identical)."""
        return self.identical

    @property
    def progress_at_risk(self) -> int:
        """Play-time seconds that could be lost: none with an identical copy, else all of it
        (a copy that is not identical may be another branch)."""
        return 0 if self.backed_up else self.live_play_time

    @property
    def warning(self) -> str:
        """One sentence on what would be lost, or '' when the vault already has this save.

        An unbacked slot warns even at zero play time: "nothing in the vault" is the fact
        that matters there, not how far the save had got.
        """
        if not self.occupied:
            return ""
        if self.backed_up:
            return ""
        if self.unverifiable:
            return (
                f"The save in slot {self.slot} ('{self.live_name}') is too old to carry a "
                "playthrough id, and no identical copy is in the vault, so a copy of it "
                "cannot be verified."
            )
        if self.entry_id is None:
            return f"No copy of the save in slot {self.slot} ('{self.live_name}') was found in the vault."
        diff = self.live_play_time - self.backup_play_time
        if diff > 0:
            relation = f"{format_duration(diff)} less play time"
        elif diff < 0:
            relation = f"{format_duration(-diff)} more play time"
        else:
            relation = "the same play time"
        return (
            f"The vault holds no identical copy of the save in slot {self.slot} "
            f"('{self.live_name}'). Its closest copy of this playthrough ('{self.entry_id}') has "
            f"{relation}: it may be an earlier point or a different branch, so it does not "
            "prove this save is safe."
        )

    @property
    def cloud_warning(self) -> str:
        """Why a Steam Cloud delete does not stick, and the procedure that makes it.

        Steam restores a file that is merely MISSING locally -- its manifest still lists
        it -- so deleting saves under Cloud is undone on the next launch. Overwrites are
        not affected: a changed file is seen as a change.
        """
        if not self.steam_cloud:
            return ""
        return (
            "Steam Cloud is syncing this folder, and Steam puts back files that are only "
            "missing locally, so this slot can reappear the next time you launch the game. "
            "To make the deletion stick: in Steam, No Man's Sky > Properties > General, "
            "turn off 'Keep game saves in the Steam Cloud'; clear the slot; launch and quit "
            "the game once; then turn Cloud back on and keep the local copy if Steam asks."
        )


def format_duration(seconds: int) -> str:
    """Play time as 'Nh MMm' (or 'Nm' under an hour), for the clear-slot messages."""
    h, m = divmod(max(0, int(seconds)) // 60, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m"


@dataclass
class _VaultCopy:
    entry: CatalogEntry
    member: savedir.MemberView
    identical: bool


# What reading a save can raise: I/O, and the decoders' ValueError / IndexError /
# struct.error on damaged bytes. Anything else is a bug and is let through.
_READ_ERRORS = (OSError, ValueError, IndexError, struct.error)


def _best_vault_copy(
    vault: Vault, live_dir: Path, sv: savedir.SlotView, notes: list[str], other_live_dirs: list[Path]
) -> tuple[_VaultCopy | None, bool]:
    """The vault's best copy of the save the game loads from live slot ``sv`` (its newest
    member), and whether that save carries a playthrough id.

    Best: a copy of these very bytes, else the copy of the same playthrough whose play time
    is closest (then the newest entry) -- shown only as information. Matched by the game's
    own playthrough id, wherever it sits in the vault -- never by slot number, which in
    another folder may hold a different save. A save too old to carry an id matches only a
    byte-identical copy. Live folders and their xgs twins never count (the live folder
    itself would answer "backed up" by pointing at the save about to be deleted; its xgs
    mirror holds the same bytes; another live folder is no backup either), nor do entries
    whose folder has gone. A file that cannot be read is noted in ``notes`` and skipped.
    """
    target = sv.newest
    platform = savedir.platform_of(live_dir)
    cache = playtime.IdCache(vault.root / playtime.CACHE_NAME)
    try:
        key, identified = playtime.identity(target, platform, cache)
    except _READ_ERRORS as exc:
        _note(notes, f"the live file {Path(target.data_path).name}", exc)
        key, identified = "", False
    live_play = target.info.total_play_time if target.info else 0
    try:
        target_hash = safety.sha256_file(target.data_path)
    except OSError as exc:
        _note(notes, f"the live file {Path(target.data_path).name}", exc)
        target_hash = None

    def same_bytes(m: savedir.MemberView) -> bool:
        return (
            target_hash is not None and m.data_size == target.data_size
            and safety.sha256_file(m.data_path) == target_hash
        )

    lives = msstore.with_xgs_twins([live_dir, *other_live_dirs])
    copies: list[_VaultCopy] = []
    for entry in vault.entries:
        folder = Path(entry.path)
        try:
            if not folder.is_dir() or any(_same_folder(folder, d) for d in lives):
                continue
            view = savedir.scan_any(folder)
            entry_platform = savedir.platform_of(folder)
        except _READ_ERRORS as exc:
            _note(notes, entry.id, exc)
            continue
        for other in view.occupied_slots:
            for m in other.present_members:
                try:
                    if m.info is None:
                        continue
                    same = same_bytes(m)
                    if same or (identified and playtime.identity(m, entry_platform, cache)[0] == key):
                        copies.append(_VaultCopy(entry, m, same))
                except _READ_ERRORS as exc:
                    _note(notes, f"{entry.id} slot {other.slot}{m.label}", exc)
    cache.flush()
    if not copies:
        return None, identified
    best = max(copies, key=lambda c: (
        c.identical, -abs(c.member.info.total_play_time - live_play), c.entry.created,
    ))
    return best, identified


def _note(notes: list[str], what: str, exc: Exception) -> None:
    log.warning("clear plan: could not check %s: %s", what, exc)
    notes.append(f"{what} could not be checked ({exc}).")


def plan_clear_slot(
    vault: Vault, live_dir: Path, slot: int, other_live_dirs: Iterable[Path] = ()
) -> ClearPlan:
    """Describe what clearing ``slot`` would cost. Changes nothing but playtime's
    playthrough-id cache in the vault. ``other_live_dirs``: the caller's other live folders,
    which are never counted as copies even when catalogued."""
    _check_slot(slot)
    live_dir = Path(live_dir)
    view = savedir.scan_any(live_dir)
    sv = view.slots.get(slot)
    if sv is None or not sv.occupied:
        return ClearPlan(slot=slot, occupied=False, steam_cloud=view.steam_cloud)

    plan = ClearPlan(
        slot=slot,
        occupied=True,
        steam_cloud=view.steam_cloud,
        live_name=sv.display_name,
        live_play_time=sv.newest.info.total_play_time if sv.newest.info else 0,   # what loads
    )
    found, identified = _best_vault_copy(vault, live_dir, sv, plan.notes, [Path(d) for d in other_live_dirs])
    plan.unverifiable = found is None and not identified
    if found is not None:
        plan.entry_id = found.entry.id
        plan.entry_label = found.entry.label
        plan.entry_kind = found.entry.kind
        plan.entry_created = found.entry.created
        plan.backup_name = found.member.save_name
        plan.backup_play_time = found.member.info.total_play_time
        plan.identical = found.identical
    return plan


@_recorded("clear", "slot")
def clear_slot(
    vault: Vault, live_dir: Path, slot: int, allow_game_running: bool = False
) -> OpResult:
    """Empty a live slot: delete both of its saves after snapshotting the live folder.

    Destructive by design, so it is snapshot-backed like every other write and reversible
    with ``undo``. Callers are expected to have shown :func:`plan_clear_slot` first; this
    function does not ask, it only refuses to run on a slot that is already empty.
    """
    warnings: list[str] = []
    live_dir = Path(live_dir)
    _check_slot(slot)
    if _is_xbox(live_dir):
        return _clear_slot_xbox(vault, live_dir, slot, allow_game_running)
    _guard_game(allow_game_running, warnings)

    # Driven by what is on disk rather than by SlotView.occupied, so a half-present slot
    # (a meta whose data file is gone) is cleared out too instead of being called empty.
    doomed = [p for fno in slot_file_numbers(slot) for p in _member_paths(live_dir, fno) if p.is_file()]
    if not doomed:
        raise OperationError(f"slot {slot} is already empty")

    snap = snapshot_live(vault, live_dir, reason=f"pre-clear slot {slot}")
    changed: list[str] = []
    for p in doomed:
        p.unlink()
        changed.append(f"-{p.name}")

    _log_op(vault, _oplog("clear_slot", snapshot_id=snap.id, detail=f"slot {slot}", changed=changed, target=live_dir), warnings)
    result = OpResult(
        True, "clear_slot", f"cleared slot {slot}", snapshot_id=snap.id, changed=changed, warnings=warnings
    )
    if savedir.scan(live_dir).slots[slot].occupied:
        raise _written_but_invalid(f"Slot {slot} still holds a save after clearing it.", result)
    return result


def _clear_slot_xbox(vault: Vault, live_dir: Path, slot: int, allow_game_running: bool) -> OpResult:
    warnings: list[str] = []
    _require_wgs(live_dir)
    _guard_game(allow_game_running, warnings)
    sv = savedir.scan_any(live_dir).slots.get(slot)
    if sv is None or not sv.occupied:
        raise OperationError(f"slot {slot} is already empty")

    snap = snapshot_live(vault, live_dir, reason=f"pre-clear slot {slot}")
    when = _now()
    changed: list[str] = []
    for member in (0, 1):
        identifier = _ms_identifier(slot, member)
        if msstore.delete_save(live_dir, identifier, when):
            changed.append(f"-{identifier}")

    _log_op(vault, _oplog("clear_slot", snapshot_id=snap.id, detail=f"xbox slot {slot}", changed=changed, target=live_dir), warnings)
    result = OpResult(
        True, "clear_slot", f"cleared slot {slot}", snapshot_id=snap.id, changed=changed, warnings=warnings
    )
    if savedir.scan_any(live_dir).slots[slot].occupied:
        raise _written_but_invalid(f"Slot {slot} still holds a save after clearing it.", result)
    return result


# --- feature 3: import -------------------------------------------------------


def import_backup(
    vault: Vault, path: Path, label: str = "", copy_into_vault: bool = False,
    warnings: list[str] | None = None,
) -> CatalogEntry:
    """Catalogue a backup folder, in place or copied in. ``warnings`` as in
    :func:`create_full_backup`."""
    path = Path(path)
    if not savedir.looks_like_save_dir(path):
        raise OperationError(f"not an NMS save folder: {path}")
    plat = savedir.platform_of(path)
    collected: list[str] = []
    if copy_into_vault:
        files = _all_files(path)
        entry = _new_managed_entry(
            vault, KIND_IMPORTED, vault.backups_dir, tag=plat, source_root=path, files=files,
            fill=lambda dest, copy: shutil.copytree(path, dest, dirs_exist_ok=True, copy_function=copy),
            label=label or path.name, source=path.name,
        )
    else:
        iid = vault.new_id(KIND_INPLACE, tag=plat)
        entry = vault.make_entry_from_dir(iid, KIND_INPLACE, label or path.name, path, managed=False, source=path.name)
        vault.upsert(entry)
    _log_op(vault, _oplog("import", entry_id=entry.id, detail=str(path)), collected)
    _hand_over(collected, warnings)
    return entry


# --- feature 4: import a whole vault ----------------------------------------
#
# Recognise a copied vault directory (its own catalog.json + backups/snapshots/extracts) and
# fold the entries this vault does not already have into it -- either copying each entry's
# files in (self-contained) or cataloguing them in place (referencing the source folder).
# Keyed on entry id, so re-importing the same vault is a no-op (idempotent).


def preview_vault_import(vault: Vault, source_dir: Path) -> dict:
    """Compare a source vault against ``vault`` without changing anything. Returns
    ``{"total", "new", "existing"}`` where new/existing are lists of the source's
    ``CatalogEntry``. Raises ``OperationError`` if ``source_dir`` is not a vault or is this
    same vault."""
    source_dir = Path(source_dir)
    if not looks_like_vault_dir(source_dir):
        raise OperationError(f"not an NMS Save Vault folder (no catalog.json): {source_dir}")
    if source_dir.resolve() == vault.root.resolve():
        raise OperationError("that is the current vault; nothing to import")
    src_entries = Vault(source_dir).load().entries
    have = {e.id for e in vault.entries}
    return {
        "total": len(src_entries),
        "new": [e for e in src_entries if e.id not in have],
        "existing": [e for e in src_entries if e.id in have],
    }


def import_vault(vault: Vault, source_dir: Path, copy_into_vault: bool) -> OpResult:
    """Import the entries of another vault at ``source_dir`` into ``vault``. With
    ``copy_into_vault`` each managed entry's files are copied into this vault (self-contained);
    otherwise they are catalogued in place, referencing the source folder. Idempotent: an
    entry whose id this vault already has is skipped, and an existing target folder is never
    overwritten."""
    source_dir = Path(source_dir)
    total = preview_vault_import(vault, source_dir)["total"]  # also validates source_dir
    vault.ensure()
    src = Vault(source_dir).load()

    added: list[str] = []
    warnings: list[str] = []
    have = {e.id for e in vault.entries}
    for e in src.entries:
        if e.id in have:
            continue
        new_entry = _relocate_entry(vault, src, e, copy_into_vault, warnings)
        if new_entry is None:
            continue
        vault.upsert(new_entry)
        have.add(e.id)
        added.append(e.id)

    skipped = total - len(added)
    mode = "copied into vault" if copy_into_vault else "indexed in place"
    detail = (
        f"imported {len(added)} new entr{'y' if len(added) == 1 else 'ies'} ({mode}); "
        f"{skipped} already present"
    )
    _log_op(vault, _oplog("import_vault", detail=f"{source_dir} [{mode}] +{len(added)}"), warnings)
    return OpResult(True, "import_vault", detail, changed=added, warnings=warnings)


def _entry_folder(vault: Vault, entry: CatalogEntry) -> Path:
    """Where a managed entry's files live within ``vault`` (subfolder by kind, named by id)."""
    subdir = {KIND_SNAPSHOT: vault.snapshots_dir, KIND_EXTRACT: vault.extracts_dir}.get(
        entry.kind, vault.backups_dir
    )
    return subdir / entry.id


def _relocate_entry(
    vault: Vault, src: Vault, entry: CatalogEntry, copy_into_vault: bool, warnings: list[str]
) -> CatalogEntry | None:
    """Build a current-vault entry from a source-vault entry: copy its files in (copy mode) or
    point at the source folder (in place). Returns None to skip (source data missing)."""
    if not entry.managed or entry.kind == KIND_INPLACE:
        # External reference: the data was never inside the source vault. Re-catalogue as-is;
        # the recorded path still points at the external save folder.
        if not Path(entry.path).is_dir():
            warnings.append(f"{entry.id}: referenced folder is missing ({entry.path})")
        return replace(entry)
    src_folder = _entry_folder(src, entry)
    if not src_folder.is_dir():
        warnings.append(f"{entry.id}: data folder not found in source vault ({src_folder})")
        return None
    if copy_into_vault:
        dst_folder = _entry_folder(vault, entry)
        if not dst_folder.exists():  # idempotent: never re-copy over an existing folder
            shutil.copytree(src_folder, dst_folder)
        return replace(entry, path=str(dst_folder), managed=True)
    # In place: reference the source vault's folder, but do NOT manage it -- the files
    # belong to the other vault.
    return replace(entry, path=str(src_folder), managed=False)


# --- undo --------------------------------------------------------------------


@dataclass
class UndoPlan:
    """What :func:`undo_last` would undo in a folder now (see :func:`plan_undo`)."""

    op: str
    ts: str
    snapshot_id: str
    token: str = ""                  # the record's identity: pass to undo_last(expected=...)
    detail: str = ""
    damaged_after: bool = False      # a damaged log line is newer: undo needs acknowledge_damage
    log_warning: str = ""            # what the damaged lines are, when there are any
    with_accountdata: bool = False   # the change was a restore that put the account data back


@dataclass
class _UndoTarget:
    records: OplogRead
    entries: dict[str, CatalogEntry]
    note: str
    index: int
    passed_over: list[dict]

    @property
    def record(self) -> dict:
        return self.records[self.index]

    @property
    def token(self) -> str:
        """Which record this is (its line and snapshot), and the log it was found in (how
        many lines, which of them damaged): damage that appears after the user was asked
        changes it too."""
        damaged = ",".join(str(n) for n in self.records.damaged_lines)
        size = len(self.records.lines) + len(self.records.damaged_lines)
        return f"{self.records.lines[self.index]}:{self.record.get('snapshot_id')}:{size}:{damaged}"

    @property
    def damaged_after(self) -> bool:
        line = self.records.lines[self.index]
        return any(damaged > line for damaged in self.records.damaged_lines)

    def snapshot(self) -> CatalogEntry:
        sid = self.record.get("snapshot_id")
        snap = self.entries.get(sid)
        if snap is None or not Path(snap.path).is_dir():
            # Never fall through to an older operation: that would skip this one silently.
            why = "not in the catalog" if snap is None else "its folder is gone"
            raise OperationError(
                f"The auto-snapshot for the last change to this folder ({sid}) can no longer "
                f"be found ({why}), so it cannot be undone." + self.note
            )
        return snap


def _find_undo(vault: Vault, live_dir: Path, other_live_dirs: Iterable[Path]) -> _UndoTarget:
    """The newest not-yet-undone operation on ``live_dir`` (see :func:`undo_last`), or
    :class:`OperationError` when there is none or the log cannot settle it."""
    records = vault.read_oplog()
    note = f" {records.warning}" if records.warning else ""
    if not records:
        if note:
            raise OperationError("No operation in the undo log can be read, so nothing can be undone." + note)
        raise OperationError("Nothing has been done yet, so there is nothing to undo.")

    entries = {e.id: e for e in vault.entries}
    others = [Path(d) for d in other_live_dirs]
    undone, part_of_undo = _already_undone(records)
    passed_over: list[dict] = []
    last_here: dict | None = None             # the newest record of a change to this folder
    for index in range(len(records) - 1, -1, -1):
        record = records[index]
        sid = record.get("snapshot_id")
        if not sid:
            continue
        mine = _changed_folder(entries, record, live_dir, others)
        if mine and last_here is None:
            last_here = record
        if record.get("op") == "undo" or sid in undone or index in part_of_undo or mine is False:
            continue
        if mine is None and sid not in entries:
            # An old record that names no folder and whose snapshot is gone could not be
            # undone anyway; it must not block undo for every folder.
            passed_over.append(record)
            continue
        if mine is None:
            raise OperationError(
                f"A record in the undo log ('{record.get('op')}' at {record.get('ts')}) does not "
                "say which save folder it changed, and its snapshot does not settle it either, "
                "so undo cannot tell whether it belongs to this folder. Restore from a backup "
                "instead." + note
            )
        return _UndoTarget(records, entries, note, index, passed_over)

    if last_here is None:
        what = "the undo log records no change to it"
    else:
        what = (
            f"the last recorded change to it was '{last_here.get('op')}' at {last_here.get('ts')}, "
            "and changes already undone are not undone twice"
        )
    raise OperationError(
        f"There is nothing to undo in this folder: {what}. Backing up, extracting and "
        "importing only add to the vault -- they never change your live saves."
        + "".join(f" {_passed_over_note(r, 'An operation')}" for r in passed_over) + note
    )


def plan_undo(vault: Vault, live_dir: Path, other_live_dirs: Iterable[Path] = ()) -> UndoPlan:
    """What :func:`undo_last` would undo in ``live_dir`` now, changing nothing -- so the
    caller can name it before asking. Raises :class:`OperationError` where undo_last would
    refuse, except for a damaged log line newer than the operation: that is reported
    (``damaged_after``) for the caller to put to the user (``acknowledge_damage``)."""
    target = _find_undo(vault, Path(live_dir), other_live_dirs)
    target.snapshot()
    record = target.record
    return UndoPlan(
        op=record.get("op") or "",
        ts=record.get("ts") or "",
        snapshot_id=record.get("snapshot_id") or "",
        token=target.token,
        detail=record.get("detail") or record.get("entry_id") or "",
        damaged_after=target.damaged_after,
        log_warning=target.records.warning or "",
        with_accountdata=bool(record.get("with_accountdata")),
    )


@_recorded("undo")
def undo_last(
    vault: Vault, live_dir: Path, allow_game_running: bool = False, acknowledge_damage: bool = False,
    other_live_dirs: Iterable[Path] = (), restore_accountdata: bool = False,
    expected: str | None = None,
) -> OpResult:
    """Undo the newest not-yet-undone operation on ``live_dir``, by restoring the snapshot
    taken just before it. Repeated calls step further back through that folder's history.

    Only operations that *write into* the live folder take a snapshot. Backing up,
    extracting and importing merely add to the vault, so there is nothing to put back --
    and the message says so, rather than implying the op log is broken. Operations on other
    folders are passed over; an old record that cannot be placed stops the undo rather than
    being guessed at. Old records are placed by folder name, so ``other_live_dirs`` (the
    caller's other known live folders) makes a name they share ambiguous rather than a match.
    A damaged log line newer than the operation stops it too, unless the caller has had the
    user accept that (``acknowledge_damage``). The account data is kept, as in
    :func:`restore_full`, unless the caller asks (``restore_accountdata``) or the operation
    being undone was a restore that rolled the account data back itself.

    ``expected`` (:attr:`UndoPlan.token`) holds the undo to the operation the user was
    shown: if the log now points at another one, nothing is done. ``acknowledge_damage``
    then covers that operation only.
    """
    live_dir = Path(live_dir)
    target = _find_undo(vault, live_dir, other_live_dirs)
    if expected is not None and target.token != expected:
        raise OperationError(
            f"The undo history changed after you were asked (the last change to this folder is "
            f"now '{target.record.get('op')}' at {target.record.get('ts')}, or new lines or "
            "damage were added). Nothing was changed; press Undo again to see what it would "
            "undo now."
        )
    if not acknowledge_damage and target.damaged_after:
        # A damaged line after this record may be a later change to this folder: undoing
        # this one would put back an older state than the user expects.
        raise OperationError(
            "The record of a later operation is damaged, so undo is unsafe: it could undo "
            "an older operation instead. Restore from a backup instead, or confirm that "
            "the damaged lines may be ignored." + target.note
        )
    snap = target.snapshot()
    record = target.record
    sid = record.get("snapshot_id")
    result = restore_full(
        vault, snap, live_dir, mirror=True, allow_game_running=allow_game_running, undo_of=sid,
        restore_accountdata=restore_accountdata or bool(record.get("with_accountdata")),
    )
    result.detail = f"undid {record.get('op')} (restored {sid})"
    result.warnings += [_passed_over_note(r, "A newer operation") for r in target.passed_over]
    if acknowledge_damage and target.records.warning:
        result.warnings.append(target.records.warning)
    return result


def _passed_over_note(record: dict, which: str) -> str:
    return (
        f"{which} ('{record.get('op')}' at {record.get('ts')}) does not say which save folder "
        "it changed and its snapshot is gone, so it was passed over."
    )


def _already_undone(records: list[dict]) -> tuple[set[str], set[int]]:
    """Snapshot ids of operations already undone, and the positions of the extra records an
    undo wrote before this version. Such an undo logged ``restore_full`` (of the snapshot)
    and then ``undo`` with that snapshot's id; it had no ``undo_of``."""
    undone: set[str] = set()
    part_of_undo: set[int] = set()
    for i, record in enumerate(records):
        if record.get("op") != "undo":
            continue
        if "undo_of" in record:
            undone.add(record["undo_of"])
            continue
        restored = record.get("snapshot_id")
        undone.add(restored)
        before = records[i - 1] if i else {}
        if before.get("op") == "restore_full" and before.get("entry_id") == restored:
            part_of_undo.add(i - 1)
    return undone, part_of_undo


def _changed_folder(
    entries: dict[str, CatalogEntry], record: dict, live_dir: Path, others: list[Path]
) -> bool | None:
    """Whether ``record`` changed ``live_dir``: True, False, or None when it cannot tell.

    New records name their target. Older ones are placed by their snapshot: the folder
    name it was taken of and, where its id carries one, the platform. A name that another
    known live folder of that platform shares cannot place it."""
    target = record.get("target")
    if target:
        return _same_folder(Path(target), live_dir)
    snap = entries.get(record.get("snapshot_id"))
    if snap is None or not snap.source:
        return None
    tag = Vault.tag_of(snap.id)
    platform = tag if tag in ("steam", "xbox") else None

    def could_be(folder: Path) -> bool:
        return folder.name == snap.source and (platform is None or platform == savedir.platform_of(folder))

    if not could_be(live_dir):
        return False
    this = msstore.with_xgs_twins([live_dir])          # its xgs mirror is the same save
    if any(could_be(d) and not any(_same_folder(d, t) for t in this) for d in others):
        return None
    return True


def _same_folder(a: Path, b: Path) -> bool:
    return os.path.normcase(str(a.resolve())) == os.path.normcase(str(b.resolve()))


# --- validation helpers ------------------------------------------------------


def _validate_pair(meta_bytes: bytes, ordinal: int, data_bytes: bytes) -> None:
    """Confirm a (data, meta) pair is internally consistent for the given slot ordinal.

    size_disk is intentionally NOT checked: it can be stale on editor-touched saves
    (e.g. save6.hg). The reliable tie is size_decompressed vs the chunk structure.
    """
    if not meta.is_valid_for(meta_bytes, ordinal):
        raise ValidationError(f"meta does not validate for ordinal {ordinal}")
    info = meta.parse(meta.decrypt(meta_bytes, ordinal), ordinal)
    st = lz4_block.stats(data_bytes)
    if st.chunk_count == 0:
        raise ValidationError("save data has no valid chunks")
    if info.size_decompressed != st.total_decompressed:
        raise ValidationError(
            f"size_decompressed {info.size_decompressed} != chunk total {st.total_decompressed}"
        )


def _validate_slot(live_dir: Path, slot: int, warnings: list[str]) -> None:
    view = savedir.scan(live_dir)
    for m in view.slots[slot].present_members:
        if not m.valid:
            raise ValidationError(f"post-write validation failed for slot {slot}{m.label}: {m.note}")


def _validate_dir_metas(folder: Path, warnings: list[str]) -> None:
    view = savedir.scan(folder)
    for sv in view.slots.values():
        for m in sv.present_members:
            if not m.valid:
                warnings.append(f"slot {sv.slot}{m.label}: {m.note}")


def _log_op(vault: Vault, record: dict, warnings: list[str]) -> None:
    """Append ``record`` to the op log. The operation has already happened, so a failure
    here does not make it a failed one: it is logged and reported in ``warnings`` -- Undo
    cannot reach an operation the log does not hold."""
    try:
        vault.append_oplog(record)
    except OSError as exc:
        log.warning("the op-log record of '%s' could not be written: %s", record.get("op"), exc)
        warnings.append(f"Done, but missing from the history: the undo log could not be written ({exc}).")


def _oplog(
    op: str, *, entry_id=None, snapshot_id=None, detail="", changed=None,
    target: Path | None = None, undo_of: str | None = None,
) -> dict:
    """One op-log record. ``target`` (the live folder written) and ``undo_of`` (the snapshot
    id of the record an undo reversed) are additive keys; older readers ignore them."""
    record = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "op": op,
        "entry_id": entry_id,
        "snapshot_id": snapshot_id,
        "detail": detail,
        "changed": changed or [],
    }
    if target is not None:
        record["target"] = str(Path(target).resolve())
    if undo_of is not None:
        record["undo_of"] = undo_of
    return record
