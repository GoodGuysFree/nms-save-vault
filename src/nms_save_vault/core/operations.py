"""High-level, safety-wrapped operations on save folders.

Every operation that writes into a live folder: (1) refuses if the game is running
(unless explicitly overridden), (2) auto-snapshots the live save state first, (3) stages
+ validates + writes atomically, and (4) appends an op-log record enabling undo.

Re-slotting copies the save *data* verbatim and only re-keys the small meta; promoting a
member only edits the meta timestamp. The data bytes are never recompressed.
"""
from __future__ import annotations

import logging
import os
import shutil
import warnings as pywarnings
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

from . import formats, lz4_block, meta, msstore, safety, savedir
from .catalog import (
    KIND_EXTRACT,
    KIND_FULL,
    KIND_IMPORTED,
    KIND_INPLACE,
    KIND_SNAPSHOT,
    CatalogEntry,
    SlotSummary,
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
    """A save file changed (``vanished`` False) or disappeared (True) while it was being
    copied into the vault: most likely the game or the Xbox app was writing meanwhile."""

    def __init__(self, name: str, vanished: bool):
        super().__init__(
            f"The save file {name} {'disappeared' if vanished else 'changed'} while it was "
            "being copied into the vault, so nothing was saved. Close the game (or let the "
            "Xbox app finish syncing) and try again."
        )
        self.name = name
        self.vanished = vanished


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


# --- file-set helpers --------------------------------------------------------


def _hg_files(folder: Path) -> list[Path]:
    """All save-state files (slot saves + account), data and meta."""
    return sorted(p for p in folder.glob("*.hg") if p.is_file())


def _member_paths(folder: Path, file_no: int) -> tuple[Path, Path]:
    ref = SaveFileRef(file_no)
    return folder / ref.data_name, folder / ref.meta_name


def _guard_game(allow_game_running: bool, warnings: list[str]) -> None:
    running = safety.is_game_running()
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


def _copy_save_state(src: Path, dst: Path, files: list[Path]) -> None:
    """Copy the live save state (``files`` = :func:`_save_state_files`) into ``dst``. Xbox
    copies the whole tree, empty folders included: a mirror restore from this copy deletes
    whatever it lacks."""
    if _is_xbox(src):
        shutil.copytree(src, dst, dirs_exist_ok=True)
    else:
        for p in files:
            shutil.copy2(p, dst / p.name)


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
    fill: Callable[[Path], None],
    label: str,
    source: str,
    slots: list[int] | None = None,
    live: bool = False,
) -> CatalogEntry:
    """Copy into a new managed vault folder and catalogue it -- or leave nothing behind.

    The folder is always a fresh one (never a leftover of an earlier failure, whose contents
    are unknown); ``fill`` copies ``files`` into it, each to its place relative to
    ``source_root``, and every copy is then checked by sha256 against its source. If the
    copy, the check, the scan or the catalog write fails, the half-written folder is removed
    and the error re-raised: a partial, corrupt or uncatalogued copy is not a backup and must
    never be mistaken for one. ``live``: the source is a live save folder, so a mismatch most
    likely means the game or the Xbox app changed it mid-copy, and the error says so.
    """
    vault.ensure()
    try:
        needed = sum(p.stat().st_size for p in files)
    except FileNotFoundError as exc:
        raise SourceChangedError(Path(exc.filename or "").name, vanished=True) from exc
    _require_free_space(parent, needed)
    entry_id = vault.new_id(kind, tag=tag, folder=parent)
    dest = parent / entry_id
    dest.mkdir()
    try:
        fill(dest)
        _verify_copy(source_root, dest, files, live)
        entry = vault.make_entry_from_dir(
            entry_id, kind, label, dest, managed=True, source=source, slots=slots
        )
        vault.upsert(entry)
    except BaseException as exc:
        _remove_partial(dest)
        gone = next((f for f in files if not f.exists()), None)
        if isinstance(exc, OSError) and gone is not None:
            raise SourceChangedError(gone.name, vanished=True) from exc
        raise
    return entry


def snapshot_live(vault: Vault, live_dir: Path, reason: str) -> CatalogEntry:
    """Copy the live save state into the vault as an auto-snapshot entry."""
    live_dir = Path(live_dir)
    files = _save_state_files(live_dir)
    return _new_managed_entry(
        vault, KIND_SNAPSHOT, vault.snapshots_dir, tag=savedir.platform_of(live_dir),
        source_root=live_dir, files=files, fill=lambda dest: _copy_save_state(live_dir, dest, files),
        label=f"auto: {reason}", source=live_dir.name, live=True,
    )


# --- feature 1: full backup / restore ---------------------------------------


def create_full_backup(
    vault: Vault, live_dir: Path, label: str = "", include_cache: bool = True,
    warnings: list[str] | None = None,
) -> CatalogEntry:
    """Copy the live folder into the vault. ``warnings`` collects anything the caller should
    show (today: an op-log record that could not be written)."""
    live_dir = Path(live_dir)
    files = _all_files(live_dir) if include_cache else _save_state_files(live_dir)

    def fill(dest: Path) -> None:
        if include_cache:
            shutil.copytree(live_dir, dest, dirs_exist_ok=True)
        else:
            _copy_save_state(live_dir, dest, files)

    entry = _new_managed_entry(
        vault, KIND_FULL, vault.backups_dir, tag=savedir.platform_of(live_dir),
        source_root=live_dir, files=files, fill=fill, label=label, source=live_dir.name, live=True,
    )
    _log_op(vault, _oplog("full_backup", entry_id=entry.id), warnings)
    return entry


def _verify_copy(src: Path, dst: Path, files: list[Path], live: bool) -> None:
    for p in files:
        rel = p.relative_to(src)
        d = dst / rel
        if not d.is_file() or safety.sha256_file(p) != safety.sha256_file(d):
            if live:
                raise SourceChangedError(str(rel), vanished=False)
            raise ValidationError(f"the copy of {rel} in the vault does not match the original")


def restore_entry(
    vault: Vault, entry: CatalogEntry, live_dir: Path, allow_game_running: bool = False
) -> OpResult:
    """Put a catalog entry back into the live folder, the right way for its kind.

    A single-slot **extract** is not a picture of a whole save folder -- it is one slot
    lifted aside. Restoring it means putting that slot back where it came from and leaving
    every other slot alone. Running it through :func:`restore_full` instead would mirror
    the live folder onto a one-slot backup and delete every other save in it, which is
    exactly the accident this function exists to prevent.
    """
    if entry.kind != KIND_EXTRACT:
        return restore_full(vault, entry, live_dir, allow_game_running=allow_game_running)

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


def restore_full(
    vault: Vault, entry: CatalogEntry, live_dir: Path, mirror: bool = True, allow_game_running: bool = False,
    *, undo_of: str | None = None,
) -> OpResult:
    """Put a whole backup back into the live folder. ``undo_of`` (used by :func:`undo_last`)
    records the restore as the undo of the op whose snapshot that was."""
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
        return _restore_full_xbox(vault, entry, src, live_dir, warnings, undo_of)
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
    src_hg = {p.name for p in _hg_files(src)}
    for p in _hg_files(src):
        safety.atomic_copy(p, live_dir / p.name, preserve_mtime=keep_mtime)
        changed.append(p.name)
    vdf = src / formats.STEAM_AUTOCLOUD
    if vdf.is_file():
        safety.atomic_copy(vdf, live_dir / vdf.name, preserve_mtime=keep_mtime)

    if mirror:
        for p in _hg_files(live_dir):
            if p.name not in src_hg:
                p.unlink()
                changed.append(f"-{p.name}")

    _validate_dir_metas(live_dir, warnings)
    _log_op(vault, _oplog(op, entry_id=entry.id, snapshot_id=snap.id, changed=changed, target=live_dir, undo_of=undo_of), warnings)
    return OpResult(True, op, f"restored {entry.id}", snapshot_id=snap.id, changed=changed, warnings=warnings)


# --- feature 2: extract a slot, repopulate a slot, promote a member ----------


def extract_slot(
    vault: Vault, source_dir: Path, slot: int, label: str = "", warnings: list[str] | None = None
) -> CatalogEntry:
    """Copy one slot into the vault. ``warnings`` as in :func:`create_full_backup`."""
    source_dir = Path(source_dir)
    _check_slot(slot)
    if _is_xbox(source_dir):
        return _extract_slot_xbox(vault, source_dir, slot, label, warnings)
    files: list[Path] = []
    for fno in slot_file_numbers(slot):
        data, meta_file = _member_paths(source_dir, fno)
        if data.is_file() and meta_file.is_file():
            files += [data, meta_file]
    if not files:
        raise OperationError(f"slot {slot} has no saves to extract in {source_dir}")
    entry = _new_managed_entry(
        vault, KIND_EXTRACT, vault.extracts_dir, tag=savedir.platform_of(source_dir),
        source_root=source_dir, files=files, fill=lambda dest: _copy_save_state(source_dir, dest, files),
        label=label or f"slot {slot} from {source_dir.name}", source=source_dir.name, slots=[slot],
    )
    _log_op(vault, _oplog("extract_slot", entry_id=entry.id, detail=f"slot {slot}"), warnings)
    return entry


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
        planned.append((dst_data, data_bytes, dst_meta, new_meta, src_data.stat().st_mtime))

    if not planned:
        raise OperationError(f"source slot {source_slot} has no saves in {source_dir}")

    snap = snapshot_live(vault, live_dir, reason=f"pre-repopulate slot {dest_slot}")
    # See restore_full: under Steam Cloud the source mtime would make the cloud copy look
    # newer than what we just wrote, and Steam would put its copy back.
    keep_mtime = not _steam_cloud_on(live_dir)
    changed: list[str] = []
    for dst_data, data_bytes, dst_meta, new_meta, mtime in planned:
        safety.atomic_write_bytes(dst_data, data_bytes)
        safety.atomic_write_bytes(dst_meta, new_meta)
        if keep_mtime:
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
    return result


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


def _already_newest(target, sibling, own_time: float, sibling_time: float) -> bool:
    """True when ``target`` already wins on every signal: the meta save time (wherever the
    sibling has one) and the file or index time."""
    meta_ok = not sibling.timestamp or target.timestamp > sibling.timestamp
    return meta_ok and own_time > sibling_time


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
    if _already_newest(target, sibling, own, sibling_time):
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
    if _already_newest(target, sibling, target.data_mtime, sibling.data_mtime if sibling.exists else 0.0):
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
    vault: Vault, source_dir: Path, slot: int, label: str, warnings: list[str] | None
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

    def fill(dest: Path) -> None:
        for c in picked:
            shutil.copytree(c.directory, dest / c.dir_guid)  # the GUID blob folder, verbatim
        # Rebuild the index from just the picked records; build uses their dir_guid (not
        # absolute paths), so the copied folders resolve under dest.
        subset = replace(info, trailer=b"")      # the source's trailing bytes are not ours
        safety.atomic_write_bytes(dest / "containers.index", msstore.build_containers_index(subset, picked))

    entry = _new_managed_entry(
        vault, KIND_EXTRACT, vault.extracts_dir, tag="xbox", source_root=source_dir, files=files, fill=fill,
        label=label or f"slot {slot} from {source_dir.name}", source=source_dir.name, slots=[slot],
    )
    # extract_slot -> here -> _log_op: one frame deeper than the other public operations.
    _log_op(vault, _oplog("extract_slot", entry_id=entry.id, detail=f"xbox slot {slot}"), warnings, stacklevel=4)
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
    floor = max((m.xbox.last_write for m in dest_sv.members if m.xbox is not None), default=0.0)
    newest = max(_now(), floor + len(planned))
    planned.sort(key=lambda p: p[0] is src_sv.newest)       # the source's newer member last
    times = [newest - (len(planned) - 1 - i) for i in range(len(planned))]

    snap = snapshot_live(vault, live_dir, reason=f"pre-repopulate slot {dest_slot}")
    changed: list[str] = []
    for (_sm, dest_ident, data_bytes, meta_bytes), when in zip(planned, times):
        msstore.write_save(live_dir, dest_ident, data_bytes, meta_bytes, when, create_if_missing=True)
        changed.append(dest_ident)

    # Logged before the read-back check, so a failed check still leaves undo its snapshot.
    _log_op(vault, _oplog("repopulate_slot", snapshot_id=snap.id, detail=f"xbox {Path(source_dir).name} slot {source_slot} -> slot {dest_slot}", changed=changed, target=live_dir), warnings)
    result = OpResult(True, "repopulate_slot", f"slot {source_slot} -> {dest_slot}", snapshot_id=snap.id, changed=changed, warnings=warnings)
    _validate_xbox_slot(live_dir, dest_slot, result)
    return result


def _restore_full_xbox(
    vault: Vault, entry: CatalogEntry, src: Path, live_dir: Path, warnings: list[str], undo_of: str | None
) -> OpResult:
    op = "undo" if undo_of else "restore_full"
    _require_wgs(live_dir)
    if not (src / "containers.index").is_file():
        raise OperationError(f"'{entry.id}' is not an Xbox/wgs backup")
    if src.resolve() == live_dir.resolve():
        # Restoring a folder onto itself is a no-op; the mirror below would otherwise wipe it.
        return OpResult(True, "restore_full", f"'{entry.id}' is the live folder; nothing to do", warnings=warnings)

    snap = snapshot_live(vault, live_dir, reason=f"pre-restore of {entry.id}")
    # Mirror the backup into the live folder (captured on this same account/machine, so its
    # containers.index identity is valid here). Reconcile rather than clear-then-copy, so a
    # mid-operation failure can't leave a half-emptied tree.
    src_names = {c.name for c in src.iterdir()}
    changed: list[str] = []
    for child in list(live_dir.iterdir()):          # 1) drop live entries not in the backup
        if child.name not in src_names:
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink()
            changed.append(f"-{child.name}")
    for child in src.iterdir():                      # 2) copy/overwrite the backup entries
        dst = live_dir / child.name
        if child.is_dir():
            if dst.exists():
                shutil.rmtree(dst, ignore_errors=True)
            shutil.copytree(child, dst)
        else:
            shutil.copy2(child, dst)
        changed.append(child.name)

    msstore.mark_modified(live_dir)  # so the Xbox app re-syncs the restored tree
    changed += msstore.mirror_xgs(live_dir)
    _validate_xbox_dir(live_dir, warnings)
    _log_op(vault, _oplog(op, entry_id=entry.id, snapshot_id=snap.id, changed=changed, target=live_dir, undo_of=undo_of), warnings)
    return OpResult(True, op, f"restored {entry.id}", snapshot_id=snap.id, changed=changed, warnings=warnings)


# --- feature 2b: clear a slot ------------------------------------------------
#
# Emptying a live slot is the one operation whose whole point is to destroy a save, so it
# is planned before it is done: plan_clear_slot() reports what the slot holds, the most
# recent copy of it in the vault, and how much play time the live save has gained since --
# i.e. exactly what would be lost. The front ends turn a non-empty ClearPlan.warning into
# an "are you sure?".


@dataclass
class ClearPlan:
    """What clearing one live slot would destroy, measured against the vault."""

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

    @property
    def backed_up(self) -> bool:
        return self.entry_id is not None

    @property
    def progress_at_risk(self) -> int:
        """Play-time seconds the live save has that the newest vault copy does not."""
        if not self.backed_up:
            return self.live_play_time
        return max(0, self.live_play_time - self.backup_play_time)

    @property
    def warning(self) -> str:
        """One sentence on what would be lost, or '' when the vault already has this save.

        An unbacked slot warns even at zero play time: "nothing in the vault" is the fact
        that matters there, not how far the save had got.
        """
        if not self.occupied:
            return ""
        if not self.backed_up:
            return f"No copy of slot {self.slot} was found in the vault."
        if self.progress_at_risk > 0:
            return (
                f"The live save has {format_duration(self.progress_at_risk)} more play time "
                f"than the newest copy in the vault ('{self.entry_id}'), so that progress "
                "would be lost."
            )
        return ""

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


def _slot_play_time(summary: SlotSummary) -> int:
    return max((m.play_time for m in summary.members if m.present), default=0)


def _latest_vault_copy(
    vault: Vault, live_dir: Path, slot: int
) -> tuple[CatalogEntry, SlotSummary] | None:
    """The most recently created catalog entry holding ``slot``, with that slot's summary.

    The live folder itself is skipped even when it is catalogued: an in-place import of
    the live folder would otherwise always answer "backed up" by pointing at the very save
    about to be deleted. Entries whose folder has since gone away are skipped too.
    """
    live_resolved = Path(live_dir).resolve()
    candidates: list[tuple[str, CatalogEntry, SlotSummary]] = []
    for entry in vault.entries:
        folder = Path(entry.path)
        if not folder.is_dir() or folder.resolve() == live_resolved:
            continue
        summary = next((s for s in entry.slots if s.slot == slot and s.occupied), None)
        if summary is not None:
            candidates.append((entry.created, entry, summary))
    if not candidates:
        return None
    _created, entry, summary = max(candidates, key=lambda c: c[0])
    return entry, summary


def plan_clear_slot(vault: Vault, live_dir: Path, slot: int) -> ClearPlan:
    """Describe, without changing anything, what clearing ``slot`` would cost."""
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
        live_play_time=max(
            (m.info.total_play_time for m in sv.present_members if m.info is not None), default=0
        ),
    )
    found = _latest_vault_copy(vault, live_dir, slot)
    if found is not None:
        entry, summary = found
        plan.entry_id = entry.id
        plan.entry_label = entry.label
        plan.entry_kind = entry.kind
        plan.entry_created = entry.created
        plan.backup_name = summary.name
        plan.backup_play_time = _slot_play_time(summary)
    return plan


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
    if copy_into_vault:
        files = _all_files(path)
        entry = _new_managed_entry(
            vault, KIND_IMPORTED, vault.backups_dir, tag=plat, source_root=path, files=files,
            fill=lambda dest: shutil.copytree(path, dest, dirs_exist_ok=True),
            label=label or path.name, source=path.name,
        )
    else:
        iid = vault.new_id(KIND_INPLACE, tag=plat)
        entry = vault.make_entry_from_dir(iid, KIND_INPLACE, label or path.name, path, managed=False, source=path.name)
        vault.upsert(entry)
    _log_op(vault, _oplog("import", entry_id=entry.id, detail=str(path)), warnings)
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


def undo_last(
    vault: Vault, live_dir: Path, allow_game_running: bool = False, acknowledge_damage: bool = False,
    other_live_dirs: Iterable[Path] = (),
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
    user accept that (``acknowledge_damage``).
    """
    live_dir = Path(live_dir)
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
        record, line = records[index], records.lines[index]
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
        if not acknowledge_damage and any(damaged > line for damaged in records.damaged_lines):
            # A damaged line after this record may be a later change to this folder: undoing
            # this one would put back an older state than the user expects.
            raise OperationError(
                "The record of a later operation is damaged, so undo is unsafe: it could undo "
                "an older operation instead. Restore from a backup instead, or confirm that "
                "the damaged lines may be ignored." + note
            )
        snap = entries.get(sid)
        if snap is None or not Path(snap.path).is_dir():
            # Never fall through to an older operation: that would skip this one silently.
            why = "not in the catalog" if snap is None else "its folder is gone"
            raise OperationError(
                f"The auto-snapshot for the last change to this folder ({sid}) can no longer "
                f"be found ({why}), so it cannot be undone." + note
            )
        result = restore_full(
            vault, snap, live_dir, mirror=True, allow_game_running=allow_game_running, undo_of=sid
        )
        result.detail = f"undid {record.get('op')} (restored {sid})"
        result.warnings += [_passed_over_note(r, "A newer operation") for r in passed_over]
        if acknowledge_damage and records.warning:
            result.warnings.append(records.warning)
        return result

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
    if any(could_be(d) and not _same_folder(d, live_dir) for d in others):
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


def _log_op(vault: Vault, record: dict, warnings: list[str] | None, stacklevel: int = 3) -> None:
    """Append ``record`` to the op log. The operation has already happened, so a failure
    here does not make it a failed one: it is logged and, where the caller reports
    warnings, reported -- Undo cannot reach an operation the log does not hold.
    ``stacklevel`` makes a ``warnings.warn`` point at the public function's caller: 3 when
    that function calls this directly."""
    try:
        vault.append_oplog(record)
    except OSError as exc:
        log.warning("the op-log record of '%s' could not be written: %s", record.get("op"), exc)
        message = f"Done, but missing from the history: the undo log could not be written ({exc})."
        if warnings is not None:
            warnings.append(message)
        else:  # backup, extract or import called without a collector
            pywarnings.warn(OplogNotWrittenWarning(message), stacklevel=stacklevel)


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
