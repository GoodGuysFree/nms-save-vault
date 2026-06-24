"""High-level, safety-wrapped operations on save folders.

Every operation that writes into a live folder: (1) refuses if the game is running
(unless explicitly overridden), (2) auto-snapshots the live save state first, (3) stages
+ validates + writes atomically, and (4) appends an op-log record enabling undo.

Re-slotting copies the save *data* verbatim and only re-keys the small meta; promoting a
member only edits the meta timestamp. The data bytes are never recompressed.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import formats, lz4_block, meta, safety, savedir
from .catalog import (
    KIND_EXTRACT,
    KIND_FULL,
    KIND_IMPORTED,
    KIND_INPLACE,
    KIND_SNAPSHOT,
    CatalogEntry,
    Vault,
)
from .slotmap import SaveFileRef, slot_file_numbers, storage_ordinal

SNAPSHOT_RETENTION = 20


class OperationError(Exception):
    pass


class GameRunningError(OperationError):
    pass


class ValidationError(OperationError):
    pass


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


# --- snapshots & copying -----------------------------------------------------


def _copy_save_state(src: Path, dst: Path) -> list[str]:
    """Copy all *.hg (+ steam_autocloud.vdf) from src to dst. Returns copied names."""
    dst.mkdir(parents=True, exist_ok=True)
    copied = []
    for p in _hg_files(src):
        shutil.copy2(p, dst / p.name)
        copied.append(p.name)
    vdf = src / formats.STEAM_AUTOCLOUD
    if vdf.is_file():
        shutil.copy2(vdf, dst / vdf.name)
    return copied


def snapshot_live(vault: Vault, live_dir: Path, reason: str) -> CatalogEntry:
    """Copy the live save state into the vault as an auto-snapshot entry."""
    vault.ensure()
    sid = vault.new_id(KIND_SNAPSHOT)
    dest = vault.snapshots_dir / sid
    _copy_save_state(Path(live_dir), dest)
    entry = vault.make_entry_from_dir(
        sid, KIND_SNAPSHOT, f"auto: {reason}", dest, managed=True, source=Path(live_dir).name
    )
    vault.upsert(entry)
    _prune_snapshots(vault)
    return entry


def _prune_snapshots(vault: Vault) -> None:
    snaps = sorted((e for e in vault.entries if e.kind == KIND_SNAPSHOT), key=lambda e: e.id)
    for old in snaps[:-SNAPSHOT_RETENTION]:
        shutil.rmtree(old.path, ignore_errors=True)
        vault.remove(old.id)


# --- feature 1: full backup / restore ---------------------------------------


def create_full_backup(vault: Vault, live_dir: Path, label: str = "", include_cache: bool = True) -> CatalogEntry:
    vault.ensure()
    live_dir = Path(live_dir)
    bid = vault.new_id(KIND_FULL)
    dest = vault.backups_dir / bid
    if include_cache:
        shutil.copytree(live_dir, dest)
    else:
        _copy_save_state(live_dir, dest)
    _verify_copy(live_dir, dest, only_hg=not include_cache)
    entry = vault.make_entry_from_dir(bid, KIND_FULL, label, dest, managed=True, source=live_dir.name)
    vault.upsert(entry)
    vault.append_oplog(_oplog("full_backup", entry_id=bid))
    return entry


def _verify_copy(src: Path, dst: Path, only_hg: bool) -> None:
    files = _hg_files(src) if only_hg else [p for p in src.rglob("*") if p.is_file()]
    for p in files:
        rel = p.relative_to(src)
        d = dst / rel
        if not d.is_file() or safety.sha256_file(p) != safety.sha256_file(d):
            raise ValidationError(f"backup verification failed for {rel}")


def restore_full(
    vault: Vault, entry: CatalogEntry, live_dir: Path, mirror: bool = True, allow_game_running: bool = False
) -> OpResult:
    warnings: list[str] = []
    _guard_game(allow_game_running, warnings)
    live_dir = Path(live_dir)
    src = Path(entry.path)
    if not src.is_dir():
        raise OperationError(f"backup folder not found: {src}")
    if not _hg_files(src):
        raise OperationError(
            f"'{entry.id}' has no Steam-format save*.hg files, so it cannot be restored into a "
            "Steam folder. (Microsoft/Xbox Game Pass saves are read-only in this tool.)"
        )

    snap = snapshot_live(vault, live_dir, reason=f"pre-restore of {entry.id}")
    changed: list[str] = []

    src_hg = {p.name for p in _hg_files(src)}
    for p in _hg_files(src):
        safety.atomic_copy(p, live_dir / p.name)
        changed.append(p.name)
    vdf = src / formats.STEAM_AUTOCLOUD
    if vdf.is_file():
        safety.atomic_copy(vdf, live_dir / vdf.name)

    if mirror:
        for p in _hg_files(live_dir):
            if p.name not in src_hg:
                p.unlink()
                changed.append(f"-{p.name}")

    _validate_dir_metas(live_dir, warnings)
    vault.append_oplog(_oplog("restore_full", entry_id=entry.id, snapshot_id=snap.id, changed=changed))
    return OpResult(True, "restore_full", f"restored {entry.id}", snapshot_id=snap.id, changed=changed, warnings=warnings)


# --- feature 2: extract a slot, repopulate a slot, promote a member ----------


def extract_slot(vault: Vault, source_dir: Path, slot: int, label: str = "") -> CatalogEntry:
    vault.ensure()
    source_dir = Path(source_dir)
    eid = vault.new_id(KIND_EXTRACT)
    dest = vault.extracts_dir / eid
    dest.mkdir(parents=True, exist_ok=True)
    copied = 0
    for fno in slot_file_numbers(slot):
        d, m = _member_paths(source_dir, fno)
        if d.is_file() and m.is_file():
            shutil.copy2(d, dest / d.name)
            shutil.copy2(m, dest / m.name)
            copied += 1
    if copied == 0:
        shutil.rmtree(dest, ignore_errors=True)
        raise OperationError(f"slot {slot} has no saves to extract in {source_dir}")
    lbl = label or f"slot {slot} from {source_dir.name}"
    entry = vault.make_entry_from_dir(eid, KIND_EXTRACT, lbl, dest, managed=True, source=source_dir.name, slots=[slot])
    vault.upsert(entry)
    vault.append_oplog(_oplog("extract_slot", entry_id=eid, detail=f"slot {slot}"))
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
    _guard_game(allow_game_running, warnings)
    source_dir = Path(source_dir)
    live_dir = Path(live_dir)

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
    changed: list[str] = []
    for dst_data, data_bytes, dst_meta, new_meta, mtime in planned:
        safety.atomic_write_bytes(dst_data, data_bytes)
        safety.atomic_write_bytes(dst_meta, new_meta)
        safety.set_file_mtime(dst_data, mtime)
        safety.set_file_mtime(dst_meta, mtime)
        changed += [dst_data.name, dst_meta.name]

    _validate_slot(live_dir, dest_slot, warnings)
    vault.append_oplog(
        _oplog("repopulate_slot", snapshot_id=snap.id, detail=f"{source_dir.name} slot {source_slot} -> slot {dest_slot}", changed=changed)
    )
    return OpResult(True, "repopulate_slot", f"slot {source_slot} -> {dest_slot}", snapshot_id=snap.id, changed=changed, warnings=warnings)


def promote_member(vault: Vault, live_dir: Path, slot: int, member: int, allow_game_running: bool = False) -> OpResult:
    """Force a slot member to be the one the game treats as newest (timestamp bump)."""
    warnings: list[str] = []
    _guard_game(allow_game_running, warnings)
    live_dir = Path(live_dir)
    view = savedir.scan(live_dir)
    sv = view.slots[slot]
    target = sv.members[member]
    sibling = sv.members[1 - member]
    if not target.exists or not target.valid:
        raise OperationError(f"slot {slot}{'AB'[member]} is missing or unreadable")

    base = target.timestamp
    if sibling.exists and sibling.info is not None:
        base = max(base, sibling.timestamp)
    new_ts = base + 1

    ordinal = target.ordinal_used or target.mapped_ordinal
    plain = meta.decrypt(target.meta_path.read_bytes(), ordinal)
    new_meta = meta.encrypt(meta.set_timestamp(plain, new_ts), ordinal)
    if not meta.is_valid_for(new_meta, ordinal):
        raise ValidationError("promote: re-encrypted meta failed validation")

    snap = snapshot_live(vault, live_dir, reason=f"pre-promote slot {slot}{'AB'[member]}")
    safety.atomic_write_bytes(target.meta_path, new_meta)
    safety.set_file_mtime(target.meta_path, new_ts)
    if target.data_path.is_file():
        safety.set_file_mtime(target.data_path, new_ts)
    changed = [target.meta_path.name, target.data_path.name]
    vault.append_oplog(_oplog("promote_member", snapshot_id=snap.id, detail=f"slot {slot}{'AB'[member]} ts->{new_ts}", changed=changed))
    return OpResult(True, "promote_member", f"slot {slot}{'AB'[member]} promoted", snapshot_id=snap.id, changed=changed, warnings=warnings)


# --- feature 3: import -------------------------------------------------------


def import_backup(vault: Vault, path: Path, label: str = "", copy_into_vault: bool = False) -> CatalogEntry:
    path = Path(path)
    if not savedir.looks_like_save_dir(path):
        raise OperationError(f"not an NMS save folder: {path}")
    if copy_into_vault:
        iid = vault.new_id(KIND_IMPORTED)
        dest = vault.backups_dir / iid
        shutil.copytree(path, dest)
        entry = vault.make_entry_from_dir(iid, KIND_IMPORTED, label or path.name, dest, managed=True, source=path.name)
    else:
        iid = vault.new_id(KIND_INPLACE)
        entry = vault.make_entry_from_dir(iid, KIND_INPLACE, label or path.name, path, managed=False, source=path.name)
    vault.upsert(entry)
    vault.append_oplog(_oplog("import", entry_id=entry.id, detail=str(path)))
    return entry


# --- undo --------------------------------------------------------------------


def undo_last(vault: Vault, live_dir: Path, allow_game_running: bool = False) -> OpResult:
    for record in reversed(vault.read_oplog()):
        sid = record.get("snapshot_id")
        if not sid:
            continue
        snap = vault.get(sid)
        if snap is None:
            continue
        result = restore_full(vault, snap, live_dir, mirror=True, allow_game_running=allow_game_running)
        vault.append_oplog(_oplog("undo", snapshot_id=sid, detail=f"undid {record.get('op')}"))
        result.detail = f"undid {record.get('op')} (restored {sid})"
        return result
    raise OperationError("no undoable operation found in the op log")


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


def _oplog(op: str, *, entry_id=None, snapshot_id=None, detail="", changed=None) -> dict:
    return {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "op": op,
        "entry_id": entry_id,
        "snapshot_id": snapshot_id,
        "detail": detail,
        "changed": changed or [],
    }
