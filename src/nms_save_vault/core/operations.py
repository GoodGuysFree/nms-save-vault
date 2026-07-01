"""High-level, safety-wrapped operations on save folders.

Every operation that writes into a live folder: (1) refuses if the game is running
(unless explicitly overridden), (2) auto-snapshots the live save state first, (3) stages
+ validates + writes atomically, and (4) appends an op-log record enabling undo.

Re-slotting copies the save *data* verbatim and only re-keys the small meta; promoting a
member only edits the meta timestamp. The data bytes are never recompressed.
"""
from __future__ import annotations

import shutil
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
    Vault,
    looks_like_vault_dir,
)
from .slotmap import SaveFileRef, slot_file_numbers, storage_ordinal

SNAPSHOT_RETENTION = 20


class OperationError(Exception):
    pass


class GameRunningError(OperationError):
    pass


class ValidationError(OperationError):
    pass


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


def _copy_save_state(src: Path, dst: Path) -> list[str]:
    """Copy the live save state from src to dst. Steam: all *.hg (+ steam_autocloud.vdf).
    Xbox/wgs: the whole account folder (containers.index + every blob dir). Returns the
    copied file names (relative to dst)."""
    src = Path(src)
    if _is_xbox(src):
        shutil.copytree(src, dst, dirs_exist_ok=True)
        return [str(p.relative_to(dst)) for p in dst.rglob("*") if p.is_file()]
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
    sid = vault.new_id(KIND_SNAPSHOT, tag=savedir.platform_of(live_dir))
    dest = vault.snapshots_dir / sid
    _copy_save_state(Path(live_dir), dest)
    entry = vault.make_entry_from_dir(
        sid, KIND_SNAPSHOT, f"auto: {reason}", dest, managed=True, source=Path(live_dir).name
    )
    vault.upsert(entry)
    _prune_snapshots(vault)
    return entry


def _prune_snapshots(vault: Vault) -> None:
    # Sort by creation time, not id: ids are "snapshot-<platform>-<stamp>", so sorting by id
    # groups by platform and would prune the wrong (not the oldest) snapshots when a user has
    # both Steam and Xbox live folders -- including a just-created one still referenced by the
    # oplog, which would silently defeat undo. Only prune snapshots this vault manages -- an
    # in-place-imported snapshot (managed=False) belongs to another vault; never delete it.
    snaps = sorted(
        (e for e in vault.entries if e.kind == KIND_SNAPSHOT and e.managed), key=lambda e: e.created
    )
    for old in snaps[:-SNAPSHOT_RETENTION]:
        shutil.rmtree(old.path, ignore_errors=True)
        vault.remove(old.id)


# --- feature 1: full backup / restore ---------------------------------------


def create_full_backup(vault: Vault, live_dir: Path, label: str = "", include_cache: bool = True) -> CatalogEntry:
    vault.ensure()
    live_dir = Path(live_dir)
    bid = vault.new_id(KIND_FULL, tag=savedir.platform_of(live_dir))
    dest = vault.backups_dir / bid
    if include_cache:
        shutil.copytree(live_dir, dest)
    else:
        _copy_save_state(live_dir, dest)
    # Xbox always copies the whole wgs tree (no *.hg), so verify the full tree, not just *.hg.
    _verify_copy(live_dir, dest, only_hg=not include_cache and not _is_xbox(live_dir))
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
    _guard_same_platform(src, live_dir)
    if _is_xbox(live_dir):
        return _restore_full_xbox(vault, entry, src, live_dir, warnings)
    if not _hg_files(src):
        raise OperationError(
            f"'{entry.id}' has no Steam-format save*.hg files, so it cannot be restored into a "
            "Steam folder."
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
    eid = vault.new_id(KIND_EXTRACT, tag=savedir.platform_of(source_dir))
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
    live_dir = Path(live_dir)
    _check_slot(slot)
    if _is_xbox(live_dir):
        return _promote_member_xbox(vault, live_dir, slot, member, allow_game_running)
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


# --- Xbox / Game Pass (wgs) variants -----------------------------------------
#
# Same-platform only: data + meta blobs are copied verbatim and the slot identity lives in
# containers.index (no XXTEA, no re-keying). All three snapshot the live folder first, so
# `undo` (which routes back through restore_full -> _restore_full_xbox) recovers the prior
# state. Cross-platform Steam<->Xbox transfer stays gated (see _guard_same_platform).


def _ms_identifier(slot: int, member: int) -> str:
    return f"Slot{slot}{'Auto' if member == 0 else 'Manual'}"


def _validate_xbox_slot(live_dir: Path, slot: int, warnings: list[str]) -> None:
    view = savedir.scan_any(live_dir)
    present = view.slots[slot].present_members if slot in view.slots else []
    if not present:
        raise ValidationError(f"slot {slot} has no readable save after the write")
    for m in present:
        if m.info is None:
            raise ValidationError(f"slot {slot}{m.label} is unreadable after the write: {m.note}")


def _validate_xbox_dir(live_dir: Path, warnings: list[str]) -> None:
    view = savedir.scan_any(live_dir)
    for sv in view.slots.values():
        for m in sv.present_members:
            if m.info is None:
                warnings.append(f"slot {sv.slot}{m.label}: {m.note}")


def _promote_member_xbox(vault: Vault, live_dir: Path, slot: int, member: int, allow_game_running: bool) -> OpResult:
    warnings: list[str] = []
    _guard_game(allow_game_running, warnings)
    view = savedir.scan_any(live_dir)
    sv = view.slots[slot]
    target, sibling = sv.members[member], sv.members[1 - member]
    if not target.exists or target.xbox is None:
        raise OperationError(f"slot {slot}{'AB'[member]} is missing or not an Xbox save")
    new_ts = target.effective_timestamp
    if sibling.exists:
        new_ts = max(new_ts, sibling.effective_timestamp)
    new_ts += 1

    # Bump BOTH the index FILETIME (write_save's `when`) and the meta timestamp field, so the
    # promoted member is newest regardless of which one the game keys off.
    data_bytes = Path(target.data_path).read_bytes()
    meta_bytes = msstore.set_ms_meta_timestamp(Path(target.meta_path).read_bytes(), new_ts)
    snap = snapshot_live(vault, live_dir, reason=f"pre-promote slot {slot}{'AB'[member]}")
    msstore.write_save(live_dir, target.xbox.identifier, data_bytes, meta_bytes, float(new_ts))
    _validate_xbox_slot(live_dir, slot, warnings)
    changed = [target.xbox.identifier]
    vault.append_oplog(_oplog("promote_member", snapshot_id=snap.id, detail=f"xbox slot {slot}{'AB'[member]} ts->{new_ts}", changed=changed))
    return OpResult(True, "promote_member", f"slot {slot}{'AB'[member]} promoted", snapshot_id=snap.id, changed=changed, warnings=warnings)


def _repopulate_slot_xbox(vault: Vault, source_dir: Path, source_slot: int, live_dir: Path, dest_slot: int, allow_game_running: bool) -> OpResult:
    warnings: list[str] = []
    _guard_game(allow_game_running, warnings)
    src_view = savedir.scan_any(source_dir)
    src_sv = src_view.slots.get(source_slot)

    planned = []  # (dest_identifier, data_bytes, meta_bytes, when)
    for member in (0, 1):
        sm = src_sv.members[member] if src_sv else None
        if sm is None or not sm.exists or sm.xbox is None or sm.info is None:
            warnings.append(f"source slot {source_slot}{'AB'[member]} missing or unreadable; skipped")
            continue
        data_bytes = Path(sm.data_path).read_bytes()
        meta_bytes = Path(sm.meta_path).read_bytes()
        ts = sm.effective_timestamp
        when = float(ts) if ts else _now()    # 0 == unknown time -> stamp now
        planned.append((_ms_identifier(dest_slot, member), data_bytes, meta_bytes, when))

    if not planned:
        raise OperationError(f"source slot {source_slot} has no readable saves in {source_dir}")

    snap = snapshot_live(vault, live_dir, reason=f"pre-repopulate slot {dest_slot}")
    changed: list[str] = []
    for dest_ident, data_bytes, meta_bytes, when in planned:
        msstore.write_save(live_dir, dest_ident, data_bytes, meta_bytes, when, create_if_missing=True)
        changed.append(dest_ident)

    _validate_xbox_slot(live_dir, dest_slot, warnings)
    vault.append_oplog(_oplog("repopulate_slot", snapshot_id=snap.id, detail=f"xbox {Path(source_dir).name} slot {source_slot} -> slot {dest_slot}", changed=changed))
    return OpResult(True, "repopulate_slot", f"slot {source_slot} -> {dest_slot}", snapshot_id=snap.id, changed=changed, warnings=warnings)


def _restore_full_xbox(vault: Vault, entry: CatalogEntry, src: Path, live_dir: Path, warnings: list[str]) -> OpResult:
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
    _validate_xbox_dir(live_dir, warnings)
    vault.append_oplog(_oplog("restore_full", entry_id=entry.id, snapshot_id=snap.id, changed=changed))
    return OpResult(True, "restore_full", f"restored {entry.id}", snapshot_id=snap.id, changed=changed, warnings=warnings)


# --- feature 3: import -------------------------------------------------------


def import_backup(vault: Vault, path: Path, label: str = "", copy_into_vault: bool = False) -> CatalogEntry:
    path = Path(path)
    if not savedir.looks_like_save_dir(path):
        raise OperationError(f"not an NMS save folder: {path}")
    plat = savedir.platform_of(path)
    if copy_into_vault:
        iid = vault.new_id(KIND_IMPORTED, tag=plat)
        dest = vault.backups_dir / iid
        shutil.copytree(path, dest)
        entry = vault.make_entry_from_dir(iid, KIND_IMPORTED, label or path.name, dest, managed=True, source=path.name)
    else:
        iid = vault.new_id(KIND_INPLACE, tag=plat)
        entry = vault.make_entry_from_dir(iid, KIND_INPLACE, label or path.name, path, managed=False, source=path.name)
    vault.upsert(entry)
    vault.append_oplog(_oplog("import", entry_id=entry.id, detail=str(path)))
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
    vault.append_oplog(_oplog("import_vault", detail=f"{source_dir} [{mode}] +{len(added)}"))
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
    # In place: reference the source vault's folder, but do NOT manage it, so this vault's
    # snapshot pruning never deletes files that belong to the other vault.
    return replace(entry, path=str(src_folder), managed=False)


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
