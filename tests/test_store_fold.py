"""Version store B-13: folding the old backups into the store after the upgrade - what is
folded and what is skipped, verification, resume after cancel, crash or lack of space
(D3 4.2, T17, T9 in part)."""
from __future__ import annotations

import hashlib
import json
import shutil
import threading

import pytest

from nms_save_vault import cli
from nms_save_vault.core import catalog, operations, safety
from nms_save_vault.core import state as appstate
from nms_save_vault.core import store as vstore
from nms_save_vault.core.catalog import Vault
from nms_save_vault.core.organizer.model import EntryRef, StoreRef
from nms_save_vault.core.store import migrate, versionstore

from .store_helpers import steam_live


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache-home"))
    monkeypatch.setattr(safety, "is_game_running", lambda: False)
    monkeypatch.setattr(versionstore.discover, "all_live_paths", lambda *a, **k: [])


@pytest.fixture
def upgraded(tmp_path):
    """An old-layout vault - full backup, snapshot, extract, an imported copy, a hand-made
    folder and the live folder catalogued in place - upgraded, not folded yet."""
    live = steam_live(tmp_path / "NMS")
    vault = Vault(tmp_path / "vault")
    full = operations.create_full_backup(vault, live, warnings=[])
    snap = operations.snapshot_live(vault, live, "test")
    extract = operations.extract_slot(vault, live, 2, warnings=[], live_dirs=[live])
    hand = tmp_path / "hand made"
    shutil.copytree(live, hand)
    imported = operations.import_backup(vault, hand, copy_into_vault=True, warnings=[])
    inplace = operations.import_backup(vault, hand, copy_into_vault=False, warnings=[])
    alias = operations.import_backup(vault, live, copy_into_vault=False, warnings=[])
    vstore.run_upgrade(vstore.plan_upgrade(vault.root, live_roots=[live]))
    store = vstore.open_store(vault.root, live_roots=[live])
    ids = dict(full=full.id, snap=snap.id, extract=extract.id, imported=imported.id,
               inplace=inplace.id, alias=alias.id)
    return vault, live, store, ids


def _tree(folder):
    return {p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(folder.rglob("*")) if p.is_file()}


def test_the_fold_reads_every_managed_folder_and_keeps_it(upgraded):
    vault, live, store, ids = upgraded
    assert vstore.fold_pending(vault.root)
    folders = {k: _tree(vault.backups_dir) for k in ("before",)}
    report = vstore.run_fold(store, live_roots=[live])
    assert (report.total, report.verified, report.failed, report.cancelled) == (4, 4, (), False)
    assert {k for k, _why in report.skipped} == {ids["inplace"], ids["alias"]}
    assert report.fsck_ok and "4 old backups are now in the store" in report.message
    assert _tree(vault.backups_dir) == folders["before"]          # nothing deleted or changed
    for key in ("full", "snap", "extract", "imported"):
        entry = store.entry(ids[key])
        assert entry.origin["legacy_entry"] == ids[key]
        assert entry.created_local == vault.get(ids[key]).created
    assert store.entry(ids["snap"]).origin["legacy_subdir"] == "snapshots"
    listed = vstore.list_vault_entries(vault, store, live_roots=[live])
    by_id = {e.id: e for e in listed}
    assert by_id[ids["full"]].ref == StoreRef(ids["full"]) and by_id[ids["full"]].origin == "folded"
    assert ids["alias"] not in by_id and ids["inplace"] in by_id
    assert list(by_id[ids["full"]].slots) == vault.get(ids["full"]).slots
    status = vstore.store_status(vault.root, store, live_roots=[live])
    assert (status.fold_total, status.fold_done, status.fold_failed) == (4, 4, ())
    assert status.inplace_not_copied == (ids["inplace"],)
    assert not vstore.fold_pending(vault.root) and store.fsck().ok


def test_fold_idempotent_resumable(upgraded, monkeypatch):
    vault, live, store, ids = upgraded
    stop = threading.Event()
    report = vstore.run_fold(store, live_roots=[live], cancel=stop,
                             progress=lambda p: stop.set() if p.entries_done == 1 else None)
    assert report.cancelled and report.verified == 2 and vstore.fold_pending(vault.root)
    real = migrate._fold_one
    seen = []

    def crash_on_the_second(store_, entry, folder):
        seen.append(entry.id)
        if len(seen) == 2:
            raise KeyboardInterrupt("the process is stopped")
        return real(store_, entry, folder)

    monkeypatch.setattr(migrate, "_fold_one", crash_on_the_second)
    with pytest.raises(KeyboardInterrupt):
        vstore.run_fold(store, live_roots=[live])
    monkeypatch.undo()
    monkeypatch.setattr(versionstore.discover, "all_live_paths", lambda *a, **k: [])
    statuses = json.loads((vault.root / "store/state/migration.json").read_text("ascii"))
    assert sum(1 for v in statuses["fold"]["entries"].values() if v["status"] == "verified") == 3
    report = vstore.run_fold(store, live_roots=[live])
    assert report.verified == 4 and not report.failed
    assert sorted(e.id for e in store.entries()) == sorted(
        ids[k] for k in ("full", "snap", "extract", "imported"))
    assert vstore.run_fold(store, live_roots=[live]).verified == 4      # again: nothing new


def test_a_folder_that_fails_verification_stays_an_entry_of_its_own(upgraded, monkeypatch):
    vault, live, store, ids = upgraded
    target = vault.backups_dir / ids["full"] / "notes.txt"
    real_check = migrate.check_folder

    def folder_changes_after_the_read(store_, records, folder):
        if folder.name == ids["full"] and target.read_bytes() == b"not a save file":
            target.write_bytes(b"edited by hand")
        return real_check(store_, records, folder)

    monkeypatch.setattr(migrate, "check_folder", folder_changes_after_the_read)
    report = vstore.run_fold(store, live_roots=[live])
    assert [k for k, _why in report.failed] == [ids["full"]]
    assert "notes.txt" in report.failed[0][1] and report.fsck_ok is True
    assert not store.has_entry(ids["full"])                          # nothing committed
    listed = {e.id: e for e in vstore.list_vault_entries(vault, store, live_roots=[live])}
    assert listed[ids["full"]].ref == EntryRef(ids["full"])           # the folder is shown
    assert listed[ids["full"]].origin == "catalog"
    status = vstore.store_status(vault.root, store, live_roots=[live])
    assert [k for k, _why in status.fold_failed] == [ids["full"]]
    assert target.read_bytes() == b"edited by hand"                   # the folder is kept
    report = vstore.run_fold(store, live_roots=[live])                 # read again, as it is now
    assert report.verified == 4 and report.failed == ()
    assert store.entry(ids["full"]).file("notes.txt").inline == b"edited by hand"


def test_a_fold_short_of_space_waits_and_resumes(upgraded, monkeypatch):
    vault, live, store, ids = upgraded
    real_usage = shutil.disk_usage
    monkeypatch.setattr(versionstore.shutil, "disk_usage",
                        lambda p: real_usage(p)._replace(free=1024))
    report = vstore.run_fold(store, live_roots=[live])
    assert report.waiting_for_space > 0 and report.verified == 0 and "free space" in report.message
    status = vstore.store_status(vault.root, store, live_roots=[live])
    assert status.fold_waiting_for_space == report.waiting_for_space
    assert vstore.fold_pending(vault.root)
    monkeypatch.undo()
    monkeypatch.setattr(versionstore.discover, "all_live_paths", lambda *a, **k: [])
    assert vstore.run_fold(store, live_roots=[live]).verified == 4
    assert not vstore.fold_pending(vault.root)


def test_the_cli_upgrade_folds_and_store_fold_resumes(tmp_path, monkeypatch, capsys):
    live = steam_live(tmp_path / "NMS")
    vault = Vault(tmp_path / "vault")
    operations.create_full_backup(vault, live, warnings=[])
    monkeypatch.setattr(appstate, "load", lambda *a, **k: None)
    assert cli.main(["--vault", str(vault.root), "store", "upgrade", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "Vault upgraded" in out and "1 old backups are now in the store" in out
    assert cli.main(["--vault", str(vault.root), "store", "fold"]) == 0
    assert "1 old backups are now in the store" in capsys.readouterr().out
    assert catalog.looks_like_vault_dir(vault.root)
