"""The classic operations in store mode (D3 2.10): what ``operations`` needs from the version
store, kept here so the classic code changes only where an entry is made or read.

* every vault write checks the vault mode: refused while the vault is being upgraded; in store
  mode taken under D1's vault lock, the mode checked again inside it;
* backups, auto-snapshots, extracts and imports become store entries;
* a store entry is handed to the classic code as a catalog-shaped entry (no folder) and read
  through a verified temporary folder (:func:`entry_folder`), so restore, repopulate and undo
  run unchanged on it.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from weakref import WeakKeyDictionary

from .. import catalog, savedir
from ..catalog import CatalogEntry, Vault
from ..state import AppState
from .capture import CaptureScope
from .errors import StoreBusy, StoreError, StoreNotUpgraded
from .listing import list_vault_entries, store_slot_summaries
from .manifest import StoreEntry
from .merge import merge_store, open_source
from .versionstore import TMP, VersionStore, open_store, read_format, vault_lock, vault_mode

log = logging.getLogger(__name__)

PRESERVE = "preserve"
SLOT_KINDS = (catalog.KIND_EXTRACT, PRESERVE)     # restored into their slot, never mirrored


_opened: "WeakKeyDictionary[Vault, VersionStore]" = WeakKeyDictionary()


def store_of(vault: Vault, *, app_state: AppState | None = None,
             start_new: bool = True) -> VersionStore | None:
    """The vault's store in store mode, None in the old layout; refused (StoreNotUpgraded)
    while the vault is being upgraded. Opened once per :class:`Vault` object, so one operation
    (and one window) uses one store; its live folders include the configured live sources of
    ``app_state`` (the app's own configuration when not given). ``start_new`` (the writing
    operations): a vault with nothing in it yet is created now, in store mode (D3 4.1)."""
    mode = vault_mode(vault.root)
    if mode == "legacy" and start_new and not vault.read_only:
        try:
            vault.ensure()
            # a vault the app creates now (missing or empty) starts as a store
        except OSError as exc:
            log.warning("could not create the vault folders: %s", exc)
        mode = vault_mode(vault.root)
    if mode == "legacy":
        return None
    if mode == "upgrading":
        raise StoreNotUpgraded("The vault is being upgraded.")
    store = _opened.get(vault)
    if store is not None and read_format(store.root).store_id != store.store_id:
        store = None                    # undone and upgraded again since: another store
    if store is None:
        if app_state is None:
            from .. import state

            app_state = state.load()
        store = _opened[vault] = open_store(vault.root, app_state=app_state)
    return store


@contextmanager
def guarded_write(vault_root: str | Path) -> Iterator[None]:
    """Around one vault write (a catalog change, an op-log line): under D1's vault lock - the
    lock the upgrade holds while it settles and commits, so no write slips in between - with
    the mode checked inside it: refused while the vault is being upgraded."""
    from .. import safety

    if vault_mode(vault_root) == "upgrading":
        raise StoreNotUpgraded("The vault is being upgraded.")
    try:
        with vault_lock(vault_root)():
            if vault_mode(vault_root) == "upgrading":
                raise StoreNotUpgraded("The vault is being upgraded.")
            yield
    except safety.LockTimeout as exc:
        raise StoreBusy() from exc


def as_catalog_entry(entry: StoreEntry) -> CatalogEntry:
    """A store entry in the shape the classic code passes around. It has no folder (``path``
    is empty): read it through :func:`entry_folder`."""
    source = entry.source.get("account_key") or entry.source.get("name")
    return CatalogEntry(id=entry.id, kind=entry.kind, label=entry.label, path="",
                        created=entry.created_local, source=source, managed=True,
                        slots=store_slot_summaries(entry))


def find_entry(vault: Vault, entry_id: str) -> CatalogEntry | None:
    """An entry by id, catalog or store (a store entry wins: fold and copy-in keep ids)."""
    store = store_of(vault, start_new=False)
    if store is not None and store.has_entry(entry_id):
        return as_catalog_entry(store.entry(entry_id))
    return vault.get(entry_id)


def list_entries(vault: Vault, *, live_roots: Sequence[Path] | None = None
                 ) -> list[CatalogEntry]:
    """Every entry of the vault through the one listing (D3 2.9), catalog-shaped."""
    store = store_of(vault, start_new=False)
    out = []
    for v in list_vault_entries(vault, store, live_roots=live_roots):
        found = vault.get(v.id) if v.origin == "catalog" else None
        out.append(found if found is not None else as_catalog_entry(store.entry(v.id)))
    return out


def is_store_entry(vault_root: str | Path, entry: CatalogEntry) -> bool:
    return not entry.path and vault_mode(vault_root) == "store"


@contextmanager
def entry_folder(vault: Vault, entry: CatalogEntry, *, slots: Iterable[int] | None = None,
                 cache: bool = False) -> Iterator[Path]:
    """The folder holding an entry's files: a catalog entry's own, or a verified temporary
    copy of a store entry's (removed afterwards). For reading only."""
    if entry.path:
        yield Path(entry.path)
        return
    store = store_of(vault, start_new=False)
    if store is None or not store.has_entry(entry.id):
        raise StoreError(f"The entry {entry.id} is not in the vault.")
    with store.materialized(entry.id, slots=slots, cache=cache) as folder:
        yield folder


def keep_live(store: VersionStore, live_dir: Path, *, kind: str, label: str,
              scope: CaptureScope, reason: str = "") -> CatalogEntry:
    """A store entry of (part of) a live folder."""
    platform = savedir.platform_of(live_dir)
    entry = store.add_from_live(live_dir, platform=platform, account_key=live_dir.name,  # type: ignore[arg-type]
                                kind=kind, label=label, scope=scope, reason=reason)  # type: ignore[arg-type]
    return as_catalog_entry(entry)


def keep_folder(store: VersionStore, folder: Path, *, kind: str, label: str,
                scope: CaptureScope = CaptureScope(), entry_id: str | None = None,
                created_local: str | None = None, origin: dict[str, Any] | None = None
                ) -> CatalogEntry:
    """A store entry of a folder that is not live (another vault's entry, an import, an
    entry of this vault read through :func:`entry_folder`)."""
    entry = store.add_from_folder(folder, kind=kind, label=label, source=folder_source(store, folder),  # type: ignore[arg-type]
                                  scope=scope, entry_id=entry_id, created_local=created_local,
                                  origin=origin)
    return as_catalog_entry(entry)


def folder_source(store: VersionStore, folder: Path) -> dict[str, str]:
    """How an entry records the folder it was taken of: by name, never a path; a temporary
    copy of one of this vault's entries by that entry's id."""
    tmp = store.root / TMP
    if folder.parent == tmp and folder.name.startswith("mat-"):
        return {"type": "entry", "entry": folder.name[len("mat-"):-9]}    # mat-<id>-<8 hex>
    return {"type": "folder", "name": folder.name}


def is_store_vault(folder: str | Path) -> bool:
    """A vault in store mode (or being upgraded): it cannot be read as plain folders."""
    return vault_mode(folder) != "legacy"


def store_copies(store: VersionStore, target_vid: str | None, key: str, identified: bool
                 ) -> list[tuple[CatalogEntry, str, int, bool]]:
    """Copies of a live save in the store, for the clear-slot plan: (entry, save name, play
    time, identical) for each stored member with these very bytes, or - with a playthrough
    id - of the same playthrough."""
    out = []
    for entry in store.entries():
        shaped = None
        for m in entry.members:
            same = target_vid is not None and m.vid == target_vid
            if same or (identified and m.pid == key):
                shaped = shaped or as_catalog_entry(entry)
                out.append((shaped, m.name, m.play_time, same))
    return out
