"""Version store B-9: one listing of catalog and store entries, open_entry_folder and the
store status (D3 T11)."""
from __future__ import annotations

import json
import shutil

import pytest

from nms_save_vault.core import catalog, operations, safety, savedir
from nms_save_vault.core import store as vstore
from nms_save_vault.core.catalog import Vault
from nms_save_vault.core.organizer.model import EntryRef, LiveTarget, StoreRef
from nms_save_vault.core.store import listing
from nms_save_vault.core.store.d1 import LiveMember

from .store_helpers import STEAM_ACCOUNT, open_vault_store, steam_live
from .test_store_capture_xbox import wgs_live

SNAP = vstore.CaptureScope()


def _sha(path):
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def mixed(tmp_path):
    """A vault with catalog entries made by the classic operations, a store, and store
    entries: one of them the fold of a catalog entry (same id)."""
    live = steam_live(tmp_path / "live")
    (live / "save5.hg").write_bytes((live / "save.hg").read_bytes())          # moved meta
    (live / "mf_save5.hg").write_bytes((live / "mf_save.hg").read_bytes())
    (live / "save7.hg").write_bytes(b"data without its meta")                 # unpaired
    root = tmp_path / "vault"
    vault = Vault(root)
    full = operations.create_full_backup(vault, live, label="classic full", warnings=[])
    snapshot = operations.snapshot_live(vault, live, "classic snapshot")
    handmade = tmp_path / "hand made copy"
    shutil.copytree(live, handmade)
    vault.upsert(vault.make_entry_from_dir("inplace-20260101-000000", catalog.KIND_INPLACE,
                                           "by hand", handmade, managed=False))
    vault.upsert(vault.make_entry_from_dir("inplace-20260102-000000", catalog.KIND_INPLACE,
                                           "the live folder", live, managed=False))
    vstore.create_store(root)
    store = open_vault_store(root, live_roots=[live])
    folded = store.add_from_folder(
        listing.entry_folder(vault, full), kind="full", label=full.label,
        source={"type": "folder", "name": full.source}, scope=vstore.CaptureScope(cache="include"),
        entry_id=full.id, created_local=full.created,
        origin={"legacy_entry": full.id, "legacy_subdir": "backups"})
    snap = store.add_from_live(live, platform="steam", account_key=STEAM_ACCOUNT,
                               kind="snapshot", label="auto: pre-place", scope=SNAP,
                               op_id="op-20261016T111500Z-ab12", reason="pre-place")
    extract = store.add_from_live(live, platform="steam", account_key=STEAM_ACCOUNT,
                                  kind="extract", label="slot 2", scope=vstore.CaptureScope(slots=(2,)))
    loc = vstore.StoreLocator(store, live_dir=lambda t: live)
    kept = loc.preserve([LiveMember(LiveTarget("steam", STEAM_ACCOUNT, 1), "A",
                                    _sha(live / "save.hg"))], op_id="op-2", reason="place")
    return dict(tmp_path=tmp_path, live=live, vault=vault, store=store, full=full,
                snapshot=snapshot, folded=folded, snap=snap, extract=extract,
                preserve=store.entry(kept[0].ref.key))


def test_listing(mixed):
    vault, store, live = mixed["vault"], mixed["store"], mixed["live"]
    entries = vstore.list_vault_entries(vault, store, live_roots=[live])
    by_id = {e.id: e for e in entries}
    assert sorted(by_id) == sorted([mixed["full"].id, mixed["snapshot"].id, mixed["snap"].id,
                                    mixed["extract"].id, mixed["preserve"].id,
                                    "inplace-20260101-000000"])
    assert len(entries) == len(by_id)                         # the folded one listed once
    folded = by_id[mixed["full"].id]
    assert (folded.ref, folded.origin, folded.managed) == (StoreRef(folded.id), "folded", True)
    assert folded.created == mixed["full"].created
    assert by_id[mixed["snapshot"].id].ref == EntryRef(mixed["snapshot"].id)
    assert by_id[mixed["snapshot"].id].origin == "catalog"
    snap = by_id[mixed["snap"].id]
    assert (snap.kind, snap.origin, snap.op_id, snap.reason, snap.source) == (
        "snapshot", "store", "op-20261016T111500Z-ab12", "pre-place", STEAM_ACCOUNT)
    assert by_id[mixed["preserve"].id].kind == "preserve"
    handmade = by_id["inplace-20260101-000000"]
    assert (handmade.managed, handmade.platform) == (False, "steam")
    assert entries == sorted(entries, key=lambda e: (e.created_utc, e.id))
    assert all(e.created_utc > 0 for e in entries)


def test_store_slots_read_as_the_folder_does(mixed):
    store, vault = mixed["store"], mixed["vault"]
    # The folded entry shows exactly what the catalog cached for its folder.
    assert list(listing.store_vault_entry(mixed["folded"]).slots) == mixed["full"].slots
    for key in ("snap", "extract", "preserve", "folded"):
        entry = mixed[key]
        wanted = entry.scope["slots"]
        with store.materialized(entry.id) as folder:
            expected = catalog.summarize(savedir.scan(folder),
                                         None if wanted == "all" else wanted)
        assert list(listing.store_vault_entry(entry).slots) == expected, key
    slot3 = listing.store_vault_entry(mixed["snap"]).slots[2]
    assert slot3.members[0].moved and slot3.members[0].note.startswith("meta keyed")
    slot4 = listing.store_vault_entry(mixed["snap"]).slots[3]
    assert slot4.members[0].note == "incomplete: missing meta file" and not slot4.occupied


def test_xbox_store_slots_read_as_the_folder_does(tmp_path, make_wgs_account):
    acct = wgs_live(make_wgs_account, tmp_path / "live")
    root = tmp_path / "vault"
    root.mkdir()
    vstore.create_store(root)
    store = open_vault_store(root)
    for scope in (SNAP, vstore.CaptureScope(slots=(1,))):
        entry = store.add_from_live(acct, platform="xbox", account_key=acct.name,
                                    kind="snapshot" if scope.slots is None else "extract",
                                    label="t", scope=scope)
        with store.materialized(entry.id) as folder:
            expected = catalog.summarize(savedir.scan_any(folder), scope.slots)
        assert list(listing.store_vault_entry(entry).slots) == expected


def test_get_vault_entry_follows_the_same_rules(mixed):
    vault, store, live = mixed["vault"], mixed["store"], mixed["live"]
    get = lambda i: vstore.get_vault_entry(vault, store, i, live_roots=[live])   # noqa: E731
    assert get(mixed["full"].id).ref == StoreRef(mixed["full"].id)
    assert get(mixed["snapshot"].id).ref == EntryRef(mixed["snapshot"].id)
    assert get("inplace-20260102-000000") is None                 # the live folder (P9)
    assert get("no-such-entry") is None
    assert vstore.get_vault_entry(vault, None, mixed["full"].id).ref == EntryRef(mixed["full"].id)


def test_open_entry_folder(mixed):
    vault, store, live = mixed["vault"], mixed["store"], mixed["live"]
    catalog_entry = vstore.get_vault_entry(vault, store, mixed["snapshot"].id, live_roots=[live])
    with vstore.open_entry_folder(vault, store, catalog_entry) as folder:
        assert folder == vault.snapshots_dir / mixed["snapshot"].id
    stored = vstore.get_vault_entry(vault, store, mixed["snap"].id, live_roots=[live])
    with vstore.open_entry_folder(vault, store, stored, slots=[2]) as folder:
        assert savedir.scan(folder).slots[2].occupied
        assert not savedir.scan(folder).slots[1].occupied
        assert safety.plain_path(folder.parent) == store.root / "tmp"
    assert not folder.exists()
    with pytest.raises(vstore.StoreUnavailable):
        with vstore.open_entry_folder(vault, None, stored):
            pass


def test_store_status(mixed):
    vault, store, live, tmp_path = mixed["vault"], mixed["store"], mixed["live"], mixed["tmp_path"]
    status = vstore.store_status(vault.root, store, live_roots=[live])
    assert (status.mode, status.copy_aside, status.fold_total, status.damage) == (
        "store", None, 0, ())
    assert status.entries == len(store.entries()) and status.store_bytes > 0
    assert status.inplace_not_copied == ("inplace-20260101-000000",)
    assert status.freeable_bytes == 0

    copy = tmp_path / "vault.pre-store-20261017T101500Z"
    copy.mkdir()
    (copy / "catalog.json").write_text("{}", "utf-8")
    migration = {"copy_aside": {"name": copy.name, "sibling": True, "files": 1, "bytes": 2,
                                "tree_sha256": "0" * 64, "verified_utc": "2026-10-17T10:15:00Z"},
                 "fold": {"state": "done", "waiting_for_space_bytes": 0, "entries": {
                     mixed["full"].id: {"status": "verified"},
                     "snapshot-steam-20260101-000000": {"status": "failed", "reason": "changed"}}}}
    (store.root / "state" / "migration.json").write_text(json.dumps(migration), "utf-8")
    store.record_damage(mixed["snap"].id, mixed["snap"].files[0], "corrupt", "test")
    status = vstore.store_status(vault.root, store, live_roots=[live])
    assert status.copy_aside == vstore.CopyAsideInfo(copy.name, True, 1, 2, 1792232100, True)
    assert (status.fold_total, status.fold_done) == (2, 1)
    assert status.fold_failed == (("snapshot-steam-20260101-000000", "changed"),)
    assert status.freeable_bytes == sum(
        p.stat().st_size for p in (vault.backups_dir / mixed["full"].id).rglob("*") if p.is_file())
    assert [d.entry_id for d in status.damage] == [mixed["snap"].id]
    shutil.rmtree(copy)
    assert not vstore.store_status(vault.root, store, live_roots=[live]).copy_aside.found


def test_a_legacy_vault_lists_its_catalog(tmp_path):
    live = steam_live(tmp_path / "live")
    vault = Vault(tmp_path / "vault")
    entry = operations.snapshot_live(vault, live, "x")
    assert [e.ref for e in vstore.list_vault_entries(vault, None, live_roots=[live])] == [
        EntryRef(entry.id)]
    assert vstore.store_status(vault.root, None, live_roots=[live]).mode == "legacy"
