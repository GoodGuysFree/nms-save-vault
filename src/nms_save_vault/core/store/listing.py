"""One listing of catalog and store entries (D3 2.9), for D1's scanner, the classic Backups tab
and the CLI: none of them reads ``catalog.json`` or the store on its own, so no entry is ever
invisible and a folded folder is never listed twice.

Rules: every store entry is listed; every catalog entry is listed unless a store entry has its
id (fold and copy-in keep ids) or its folder is a live folder (P9). A store entry's slots are
built from its member records exactly as ``catalog.summarize`` builds them from a folder.
"""
from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from .. import catalog, discover, savedir, slotmap
from ..catalog import CatalogEntry, MemberSummary, SlotSummary
from ..formats import MAX_SAVE_SLOTS
from ..organizer.model import EntryRef, Platform, StoreRef
from ..state import AppState
from .damage import DamageRecord, read_damage
from .errors import StoreUnavailable
from .manifest import MemberRecord, StoreEntry
from .versionstore import STATE, VaultMode, store_root, vault_mode

if TYPE_CHECKING:
    from .versionstore import VersionStore

log = logging.getLogger(__name__)

VaultEntryKind = Literal["full", "snapshot", "extract", "imported", "preserve", "inplace"]
MIGRATION_JSON = "migration.json"


@dataclass(frozen=True)
class VaultEntry:
    id: str
    kind: VaultEntryKind
    ref: EntryRef | StoreRef           # where the bytes are
    origin: Literal["catalog", "store", "folded", "copied-in"]
    platform: Platform
    label: str
    created: str                       # catalog v1 form (local, naive, seconds)
    created_utc: int
    source: str | None                 # live or external folder name; redact for display
    managed: bool                      # False: a hand-made in-place folder or another vault's
    slots: tuple[SlotSummary, ...]     # the classic pane's row type
    op_id: str | None
    reason: str


def list_vault_entries(vault: catalog.Vault, store: "VersionStore | None", *,
                       live_roots: Sequence[Path] | None = None,
                       app_state: AppState | None = None) -> list[VaultEntry]:
    """Every entry of the vault, catalog and store, sorted by ``(created_utc, id)``.
    ``live_roots``: the live save folders; None means the store's, else the live sources of
    ``app_state`` plus the ones discovered on this machine."""
    out = [store_vault_entry(e) for e in store.entries()] if store is not None else []
    stored = {e.id for e in out}
    live = _live(live_roots, store, app_state)
    for entry in vault.entries:
        if entry.id not in stored and not _in_live(vault, entry, live):
            out.append(catalog_vault_entry(vault, entry))
    return sorted(out, key=lambda e: (e.created_utc, e.id))


def get_vault_entry(vault: catalog.Vault, store: "VersionStore | None", entry_id: str, *,
                    live_roots: Sequence[Path] | None = None,
                    app_state: AppState | None = None) -> VaultEntry | None:
    """One entry by id, by the listing's rules (a store entry wins over a catalog one)."""
    if store is not None and store.has_entry(entry_id):
        return store_vault_entry(store.entry(entry_id))
    entry = vault.get(entry_id)
    if entry is None or _in_live(vault, entry, _live(live_roots, store, app_state)):
        return None
    return catalog_vault_entry(vault, entry)


@contextmanager
def open_entry_folder(vault: catalog.Vault, store: "VersionStore | None", entry: VaultEntry, *,
                      slots: Iterable[int] | None = None, cache: bool = False) -> Iterator[Path]:
    """A folder holding the entry's files, for reading only: a catalog entry's own folder, or
    a verified temporary materialization of a store entry (removed on exit). The classic
    restore, repopulate and undo code runs unchanged on it."""
    if isinstance(entry.ref, EntryRef):
        catalog_entry = vault.get(entry.id)
        if catalog_entry is None:
            raise KeyError(f"the catalog has no entry {entry.id!r}")
        yield entry_folder(vault, catalog_entry)
        return
    if store is None:
        raise StoreUnavailable("The vault's version store is not open.")
    with store.materialized(entry.id, slots=slots, cache=cache) as folder:
        yield folder


def entry_folder(vault: catalog.Vault, entry: CatalogEntry) -> Path:
    """Where a catalog entry's files are: a managed entry's folder inside this vault (so a
    moved vault still finds them), else the recorded path."""
    if entry.managed and entry.kind != catalog.KIND_INPLACE:
        subdir = {catalog.KIND_SNAPSHOT: vault.snapshots_dir,
                  catalog.KIND_EXTRACT: vault.extracts_dir}.get(entry.kind, vault.backups_dir)
        if (subdir / entry.id).is_dir():
            return subdir / entry.id
    return Path(entry.path)


# --- one entry --------------------------------------------------------------------------------


def store_vault_entry(entry: StoreEntry) -> VaultEntry:
    origin = entry.origin or {}
    kind_of_origin = ("folded" if "legacy_entry" in origin
                      else "copied-in" if "inplace_entry" in origin else "store")
    source = entry.source.get("account_key") or entry.source.get("name")
    return VaultEntry(
        id=entry.id, kind=entry.kind, ref=StoreRef(entry.id), origin=kind_of_origin,
        platform=entry.platform, label=entry.label, created=entry.created_local,
        created_utc=epoch_utc(entry.created_utc), source=source, managed=True,
        slots=tuple(store_slot_summaries(entry)), op_id=entry.op_id, reason=entry.reason)


def catalog_vault_entry(vault: catalog.Vault, entry: CatalogEntry) -> VaultEntry:
    platform = catalog.Vault.tag_of(entry.id)
    if platform not in ("steam", "xbox"):
        folder = entry_folder(vault, entry)
        platform = savedir.platform_of(folder) if folder.is_dir() else "steam"
    return VaultEntry(
        id=entry.id, kind=entry.kind, ref=EntryRef(entry.id), origin="catalog",  # type: ignore[arg-type]
        platform=platform, label=entry.label, created=entry.created,  # type: ignore[arg-type]
        created_utc=_epoch_local(entry.created), source=entry.source, managed=entry.managed,
        slots=tuple(entry.slots), op_id=None, reason="")


def store_slot_summaries(entry: StoreEntry) -> list[SlotSummary]:
    """The slots of a store entry as ``catalog.summarize`` shows the same folder."""
    return [_slot_summary(entry, slot) for slot in entry_slots(entry)]


def entry_slots(entry: StoreEntry) -> list[int]:
    """The slots a store entry observes: every slot of a whole-folder entry, the scope's
    slots of a slot-scoped one (as a scan of the folder would)."""
    wanted = entry.scope.get("slots")
    return list(range(1, MAX_SAVE_SLOTS + 1)) if not isinstance(wanted, list) else sorted(wanted)


def newest_record(entry: StoreEntry, slot: int) -> MemberRecord | None:
    """The member the game loads, by ``savedir.SlotView.newest``'s rule on the records."""
    present = [r for r in (entry.member(slot, label) for label in slotmap.MEMBER_LABELS)
               if r is not None]
    candidates = [r for r in present if has_info(entry, r)] or present
    if not candidates:
        return None
    if all(_timestamp(entry, r) for r in candidates):
        return max(candidates, key=lambda r: _timestamp(entry, r) or file_time(r))
    return max(candidates, key=file_time)


def _slot_summary(entry: StoreEntry, slot: int) -> SlotSummary:
    records = [entry.member(slot, label) for label in slotmap.MEMBER_LABELS]
    present = [r for r in records if r is not None]
    newest = newest_record(entry, slot)
    name = newest.name if newest is not None else ""
    return SlotSummary(
        slot=slot, occupied=bool(present),
        name=name or ("<unnamed>" if present else "<empty>"),
        newest_label=newest.member if newest is not None else None,
        members=[_member_summary(entry, slot, label, r)
                 for label, r in zip(slotmap.MEMBER_LABELS, records)])


def _member_summary(entry: StoreEntry, slot: int, label: str,
                    r: MemberRecord | None) -> MemberSummary:
    if r is None:
        return MemberSummary(label=label, present=False, note=entry.absent.get(f"{slot}{label}", ""))
    ms = MemberSummary(label=label, present=True, data_size=entry.file(r.data).size,
                       valid=has_info(entry, r), moved=_moved(entry, r), note=r.note)
    if has_info(entry, r):
        ms.name, ms.summary = r.name, r.summary
        ms.game_mode, ms.season = r.game_mode, r.season
        ms.play_time = r.play_time
        ms.timestamp = r.meta_ts or int(file_time(r))
        ms.base_version, ms.difficulty = r.base_version, r.difficulty
    return ms


def has_info(entry: StoreEntry, r: MemberRecord) -> bool:
    """savedir decoded the meta (its ``info`` is set, and so ``valid``): always for Xbox, for
    Steam when an ordinal opened it."""
    return entry.platform == "xbox" or r.ordinal is not None


def _timestamp(entry: StoreEntry, r: MemberRecord) -> int:
    return r.meta_ts if has_info(entry, r) else 0


def file_time(r: MemberRecord) -> float:
    return r.file_time_ns / 1_000_000_000


def _moved(entry: StoreEntry, r: MemberRecord) -> bool:
    if entry.platform != "steam" or r.ordinal is None:
        return False
    fno = slotmap.file_no(r.slot, slotmap.member_index(r.member))
    return r.ordinal != slotmap.storage_ordinal(fno)


# --- helpers ----------------------------------------------------------------------------------


def _live(live_roots: Sequence[Path] | None, store: "VersionStore | None",
          app_state: AppState | None) -> discover.LiveFolders:
    if live_roots is not None:
        return discover.live_folders(live_roots)
    if store is not None:
        return store.live_folders()
    return discover.live_folders(discover.all_live_paths(app_state))


def _in_live(vault: catalog.Vault, entry: CatalogEntry, live: discover.LiveFolders) -> bool:
    return discover.is_inside(entry_folder(vault, entry), live)


def epoch_utc(iso: str) -> int:
    try:
        return int(datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ")
                   .replace(tzinfo=timezone.utc).timestamp())
    except ValueError:
        return 0


def _epoch_local(text: str) -> int:
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return 0
    return int((when if when.tzinfo else when.astimezone()).timestamp())


# --- store status (D3 2.9) --------------------------------------------------------------------


@dataclass(frozen=True)
class CopyAsideInfo:
    name: str
    sibling: bool
    files: int
    bytes: int
    made_utc: int
    found: bool                     # present next to the vault with the recorded file count


@dataclass(frozen=True)
class StoreStatus:
    mode: VaultMode
    copy_aside: CopyAsideInfo | None
    fold_total: int
    fold_done: int
    fold_failed: tuple[tuple[str, str], ...]
    fold_waiting_for_space: int
    inplace_not_copied: tuple[str, ...]
    freeable_bytes: int
    damage: tuple[DamageRecord, ...]
    entries: int
    store_bytes: int


def store_status(vault_root: Path, store: "VersionStore | None", *,
                 live_roots: Sequence[Path] | None = None,
                 app_state: AppState | None = None) -> StoreStatus:
    """What the vault status shows: mode, the copy-aside, fold progress (from
    ``store/state/migration.json``, written by the upgrade and the fold), hand-made folders not
    copied in, the space freeing would give, damage, and the store's size."""
    vault_root = Path(vault_root)
    migration = read_migration(vault_root)
    fold = migration.get("fold") if isinstance(migration.get("fold"), dict) else {}
    states = fold.get("entries") if isinstance(fold.get("entries"), dict) else {}
    statuses = {k: v for k, v in states.items() if isinstance(v, dict)}
    stored = {e.id: e for e in store.entries()} if store is not None else {}
    vault = catalog.Vault(vault_root)
    live = _live(live_roots, store, app_state)
    inplace = tuple(sorted(
        e.id for e in vault.entries
        if e.kind == catalog.KIND_INPLACE and e.id not in stored and not _in_live(vault, e, live)))
    freeable = sum(_tree_bytes(entry_folder(vault, e)) for e in vault.entries
                   if e.id in stored and "legacy_entry" in (stored[e.id].origin or {})
                   and statuses.get(e.id, {}).get("status") == "verified")
    return StoreStatus(
        mode=vault_mode(vault_root), copy_aside=_copy_aside(vault_root, migration),
        fold_total=len(statuses),
        fold_done=sum(1 for v in statuses.values() if v.get("status") in ("folded", "verified")),
        fold_failed=tuple(sorted((k, str(v.get("reason", ""))) for k, v in statuses.items()
                                 if v.get("status") == "failed")),
        fold_waiting_for_space=_uint(fold.get("waiting_for_space_bytes")),
        inplace_not_copied=inplace, freeable_bytes=freeable,
        damage=tuple(read_damage(store_root(vault_root))), entries=len(stored),
        store_bytes=_store_bytes(store) if store is not None else 0)


def read_migration(vault_root: Path) -> dict:
    path = store_root(vault_root) / STATE / MIGRATION_JSON
    try:
        d = json.loads(path.read_bytes().decode("utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("the store's migration record cannot be read: %s", exc)
        return {}
    return d if isinstance(d, dict) else {}


def _copy_aside(vault_root: Path, migration: dict) -> CopyAsideInfo | None:
    rec = migration.get("copy_aside")
    if not isinstance(rec, dict) or not isinstance(rec.get("name"), str):
        return None
    files = _uint(rec.get("files"))
    sibling = bool(rec.get("sibling", True))
    where = vault_root.parent / rec["name"]
    found = sibling and where.is_dir() and _count_files(where) >= files
    return CopyAsideInfo(name=rec["name"], sibling=sibling, files=files,
                         bytes=_uint(rec.get("bytes")),
                         made_utc=epoch_utc(str(rec.get("verified_utc", ""))), found=found)


def _count_files(folder: Path) -> int:
    return sum(len(names) for _d, _s, names in os.walk(folder))


def _tree_bytes(folder: Path) -> int:
    total = 0
    for dirpath, _dirs, names in os.walk(folder):
        for name in names:
            try:
                total += os.stat(os.path.join(dirpath, name)).st_size
            except OSError:
                pass
    return total


def _store_bytes(store: "VersionStore") -> int:
    return sum(store.objects.size_of(d) or 0 for d in store.objects.digests()) + _tree_bytes(
        store.root / "entries")


def _uint(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0
