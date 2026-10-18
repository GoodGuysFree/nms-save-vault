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
    PRE_UPGRADE_COPY,
    CatalogEntry,
    Vault,
    looks_like_vault_dir,
)
from .slotmap import SaveFileRef, slot_file_numbers

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
    """A capability this path deliberately does not offer; the UI shows the message as
    information, not as an error/failure."""


# Shown when a write would transfer a save between Steam and Xbox (either direction).
# Same-platform writes (Steam->Steam, Xbox->Xbox) run here; a cross-platform move goes through
# the Organizer's plan, which has the checks and the confirmation the classic path lacks.
CROSS_PLATFORM_MSG = ("To move a save between Steam and Xbox, use the Organizer tab "
                      "(Move to live) or `nmsvault place`.")


@dataclass
class OpResult:
    ok: bool
    op: str
    detail: str
    snapshot_id: str | None = None
    entry_id: str | None = None
    changed: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    op_id: str | None = None          # a live operation's id (the undo and history key)


LIVE_OP_BUSY = (
    "Another NMS Save Vault window is changing live saves. Try again when it has finished."
)


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
    """Cross-platform (Steam<->Xbox) is not done here (use the Organizer); same-platform is."""
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
        with _store().guarded_write(vault.root):
            vault.upsert(entry)
    except BaseException as exc:
        _remove_partial(dest)
        gone = next((f for f in files if not f.exists()), None)
        if isinstance(exc, OSError) and gone is not None:
            raise SourceChangedError(gone.name, vanished=True, live=live) from exc
        raise
    return entry


def _store():
    """The store's classic-operation helpers (D3 2.10), imported late: the store imports this
    module for its errors."""
    from .store import classic

    return classic


def snapshot_live(vault: Vault, live_dir: Path, reason: str) -> CatalogEntry:
    """Copy the live save state into the vault as an auto-snapshot entry (in store mode, a
    store entry; refused while the vault is being upgraded)."""
    live_dir = Path(live_dir)
    store = _store().store_of(vault)
    if store is not None:
        return _store().keep_live(store, live_dir, kind=KIND_SNAPSHOT, label=f"auto: {reason}",
                                  scope=_store().CaptureScope(), reason=reason)
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

    store = _store().store_of(vault)
    if store is not None:
        entry = _store().keep_live(
            store, live_dir, kind=KIND_FULL, label=label,
            scope=_store().CaptureScope(cache="include" if include_cache else "omit"))
    else:
        entry = _new_managed_entry(
            vault, KIND_FULL, vault.backups_dir, tag=savedir.platform_of(live_dir),
            source_root=live_dir, files=files, fill=fill, label=label, source=live_dir.name,
            live=True,
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
    exactly the accident this function exists to prevent. A version store's ``preserve``
    entry (one slot kept before a live write) is restored the same way (D3 2.9 rule 5).
    """
    if entry.kind not in _store().SLOT_KINDS:
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
    with _store().entry_folder(vault, entry, slots=[slot]) as folder:
        result = repopulate_slot(
            vault, folder, slot, live_dir, slot, allow_game_running=allow_game_running
        )
    return replace(
        result,
        op="restore_extract",
        detail=f"put slot {slot} back into live slot {slot} from {entry.id}",
    )


ACCOUNT_DATA_KEPT = (
    "The backup's account data (unlocks, settings) was not restored: the live folder kept its "
    "own. Restoring it as well would roll those back."
)


# --- feature 2: extract a slot, repopulate a slot, promote a member ----------


def extract_slot(
    vault: Vault, source_dir: Path, slot: int, label: str = "", warnings: list[str] | None = None,
    live_dirs: Iterable[Path] = (),
) -> CatalogEntry:
    """Copy one slot into the vault. ``warnings`` as in :func:`create_full_backup`.
    ``live_dirs``: the caller's live folders, so a problem copying from one of them is
    explained as the game or the Xbox app writing, not as a missing drive."""
    if not str(source_dir).strip() or not Path(source_dir).is_absolute():
        raise OperationError("That source has no folder of its own to read from; a relative or "
                             "empty path is never used. Nothing was changed.")
    source_dir = Path(source_dir)
    _check_slot(slot)
    collected: list[str] = []
    live = any(safety.same_path(source_dir, d) for d in live_dirs)
    store = _store().store_of(vault)
    if store is not None:
        entry = _extract_into_store(vault, store, source_dir, slot, label, live, collected)
    elif _is_xbox(source_dir):
        entry = _extract_slot_xbox(vault, source_dir, slot, label, collected, live)
    else:
        entry = _extract_slot_steam(vault, source_dir, slot, label, collected, live)
    _hand_over(collected, warnings)
    return entry


def _extract_into_store(vault: Vault, store, source_dir: Path, slot: int, label: str,
                        live: bool, collected: list[str]) -> CatalogEntry:
    """Store mode: the slot becomes a store entry (an Xbox slot with an index of its own)."""
    classic = _store()
    label = label or f"slot {slot} from {source_dir.name}"
    scope = classic.CaptureScope(slots=(slot,))
    if live:
        entry = classic.keep_live(store, source_dir, kind=KIND_EXTRACT, label=label, scope=scope)
    else:
        entry = classic.keep_folder(store, source_dir, kind=KIND_EXTRACT, label=label,
                                    scope=scope)
    platform = "xbox " if _is_xbox(source_dir) else ""
    _log_op(vault, _oplog("extract_slot", entry_id=entry.id, detail=f"{platform}slot {slot}"),
            collected)
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


def repopulate_slot(
    vault: Vault,
    source_dir: Path,
    source_slot: int,
    live_dir: Path,
    dest_slot: int,
    allow_game_running: bool = False,
) -> OpResult:
    """Write the two saves of ``source_slot`` (from ``source_dir``) into ``dest_slot`` of
    the live folder: one live operation (D5 4.1). Both positions are written, what the slot
    held is preserved in the vault first, and the write is checked and journaled."""
    from .liveops import classic

    return classic.repopulate(vault, source_dir, source_slot, live_dir, dest_slot,
                              allow_game_running)


def promote_member(vault: Vault, live_dir: Path, slot: int, member: int, allow_game_running: bool = False) -> OpResult:
    """Make a slot member the one the game loads: one live operation (D5 4.1)."""
    from .liveops import classic

    return classic.promote(vault, live_dir, slot, member, allow_game_running)


# --- Xbox / Game Pass (wgs) extract ------------------------------------------
#
# Same-platform only here: data + meta blobs are copied verbatim and the slot identity lives in
# containers.index (no XXTEA, no re-keying). Steam<->Xbox goes through the Organizer
# (see _guard_same_platform).


def _ms_identifier(slot: int, member: int) -> str:
    return f"Slot{slot}{'Auto' if member == 0 else 'Manual'}"


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


# --- play time text ------------------------------------------------------------


def format_duration(seconds: int) -> str:
    """Play time as 'Nh MMm' (or 'Nm' under an hour)."""
    h, m = divmod(max(0, int(seconds)) // 60, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m"


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
    store = _store().store_of(vault) if copy_into_vault else None
    if store is not None:
        entry = _store().keep_folder(store, path, kind=KIND_IMPORTED, label=label or path.name,
                                     scope=_store().CaptureScope(cache="include"))
    elif copy_into_vault:
        files = _all_files(path)
        entry = _new_managed_entry(
            vault, KIND_IMPORTED, vault.backups_dir, tag=plat, source_root=path, files=files,
            fill=lambda dest, copy: shutil.copytree(path, dest, dirs_exist_ok=True, copy_function=copy),
            label=label or path.name, source=path.name,
        )
    else:
        iid = vault.new_id(KIND_INPLACE, tag=plat)
        entry = vault.make_entry_from_dir(iid, KIND_INPLACE, label or path.name, path, managed=False, source=path.name)
        with _store().guarded_write(vault.root):
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
    if (source_dir / PRE_UPGRADE_COPY).is_file():
        raise OperationError("This is the pre-upgrade copy of a vault; use Undo the vault upgrade.")
    if _store().is_store_vault(source_dir):
        # Its entries live in a version store, not in folders: merging two stores is phase 5.
        raise OperationError("Importing an upgraded vault comes with a later version.")
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
    store = _store().store_of(vault) if copy_into_vault else None
    if store is None:
        vault.ensure()
    src = Vault(source_dir).load()

    added: list[str] = []
    warnings: list[str] = []
    have = {e.id for e in vault.entries}
    if store is not None:
        have |= {e.id for e in store.entries()}
    for e in src.entries:
        if e.id in have:
            continue
        if store is not None:
            new_id = _fold_imported(vault, store, src, e, warnings)
            if new_id is not None:
                have.add(e.id)
                added.append(new_id)
            continue
        new_entry = _relocate_entry(vault, src, e, copy_into_vault, warnings)
        if new_entry is None:
            continue
        with _store().guarded_write(vault.root):
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


def _fold_imported(vault: Vault, store, src: Vault, entry: CatalogEntry,
                   warnings: list[str]) -> str | None:
    """Store mode, copying: an entry of another (old-layout) vault becomes a store entry of
    this one under its own id, deduplicated and verified (D3 4.8). The caller skips ids this
    vault already has, so importing the same vault again adds nothing."""
    folder = _entry_folder(src, entry) if entry.managed and entry.kind != KIND_INPLACE \
        else Path(entry.path)
    if not folder.is_dir():
        warnings.append(f"{entry.id}: its folder is missing ({folder.name}); not imported")
        return None
    classic = _store()
    kind = entry.kind if entry.kind in (KIND_FULL, KIND_SNAPSHOT, KIND_EXTRACT) else KIND_IMPORTED
    kept = classic.keep_folder(store, folder, kind=kind, label=entry.label, entry_id=entry.id,
                               created_local=entry.created,
                               scope=classic.CaptureScope(cache="include"),
                               origin={"imported_entry": entry.id})
    return kept.id


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
        with _store().guarded_write(vault.root):
            vault.append_oplog(record)
    except (OSError, OperationError) as exc:     # OperationError: the vault lock was busy
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


# --- the whole-folder restore and undo: the panic restore (D5 4.3) -------------------------


def restore_full(
    vault: Vault, entry: CatalogEntry, live_dir: Path, mirror: bool = True, allow_game_running: bool = False,
    *, undo_of: str | None = None, restore_accountdata: bool = False,
) -> OpResult:
    """Put a whole backup or snapshot back into the live folder, slot by slot, as one live
    operation (the panic restore). The account data stays the live account's own unless
    ``restore_accountdata``; ``steam_autocloud.vdf`` is never copied, an Xbox backup of
    another account is refused, and nothing is deleted (``mirror`` is kept for callers)."""
    from .liveops import classic

    return classic.restore_full(vault, entry, live_dir, mirror, allow_game_running,
                                undo_of=undo_of, restore_accountdata=restore_accountdata)


def plan_undo(vault: Vault, live_dir: Path, other_live_dirs: Iterable[Path] = ()) -> UndoPlan:
    """What :func:`undo_last` would restore now, changing nothing."""
    from .liveops import classic

    return classic.undo_plan(vault, live_dir)


def undo_last(
    vault: Vault, live_dir: Path, allow_game_running: bool = False, acknowledge_damage: bool = False,
    other_live_dirs: Iterable[Path] = (), restore_accountdata: bool = False,
    expected: str | None = None,
) -> OpResult:
    """Restore the newest snapshot taken of ``live_dir`` (the panic restore). Slot-scoped undo
    of a live operation is the organizer's (``liveops.plan_undo``)."""
    from .liveops import classic

    return classic.undo_last(vault, live_dir, allow_game_running, restore_accountdata, expected)
