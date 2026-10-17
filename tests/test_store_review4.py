"""Version store review fixes (D3 B-14..B-16): rollback with a missing folder, a stale plan,
store writes during an undo, unreadable manifests, a lost migration record, read-only copy,
and no lock on a missing vault."""
from __future__ import annotations

import dataclasses
import hashlib
import json

import pytest

from nms_save_vault.core import catalog, operations, safety
from nms_save_vault.core import store as vstore
from nms_save_vault.core.catalog import Vault
from nms_save_vault.core.store import errors, migrate, rollback, upgrade, versionstore

from .store_helpers import steam_live


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache-home"))
    monkeypatch.setattr(safety, "is_game_running", lambda: False)
    monkeypatch.setattr(versionstore.discover, "all_live_paths", lambda *a, **k: [])


def _setup(tmp_path, *, delete_snapshot=False):
    live = steam_live(tmp_path / "NMS")
    vault = Vault(tmp_path / "vault")
    operations.create_full_backup(vault, live, warnings=[])
    snap = operations.snapshot_live(vault, live, "t")
    if delete_snapshot:
        import shutil
        shutil.rmtree(vault.root / "snapshots" / snap.id)
    result = vstore.run_upgrade(vstore.plan_upgrade(vault.root, live_roots=[live]))
    store = vstore.open_store(vault.root, live_roots=[live])
    return vault, live, store, vault.root.parent / result.copy_aside, snap


def test_missing_folder_is_skipped_and_reported(tmp_path):
    vault, _live, store, _copy, snap = _setup(tmp_path, delete_snapshot=True)
    plan = rollback.plan_rollback(vault.root, store)
    assert plan.problems == () and snap.id not in plan.restore
    assert [k for k, _ in plan.skipped] == [snap.id]
    result = rollback.run_rollback(plan, store)
    assert vstore.vault_mode(vault.root) == "legacy"
    assert result.skipped and snap.id in result.skipped[0][0]


def test_bad_item_after_marker_does_not_strand_the_vault(tmp_path, monkeypatch):
    vault, live, store, copy, _snap = _setup(tmp_path)
    (vault.root / "backups" / next(p.name for p in (vault.root / "backups").iterdir())
     / "extra.txt").write_text("x", encoding="utf-8")        # differs from the copy: restored
    plan = rollback.plan_rollback(vault.root, store)
    assert plan.restore

    def boom(*_a):
        raise errors.RollbackError("copy-mismatch", "cannot be put back")
    monkeypatch.setattr(rollback, "_restore", boom)
    result = rollback.run_rollback(plan, store)
    assert vstore.vault_mode(vault.root) == "legacy"
    assert {k for k, _ in result.skipped} >= set(plan.restore)


def test_stale_plan_after_a_new_backup_refuses(tmp_path):
    vault, live, store, _copy, _snap = _setup(tmp_path)
    plan = rollback.plan_rollback(vault.root, store)
    operations.create_full_backup(Vault(vault.root), live, warnings=[])
    with pytest.raises(errors.RollbackError) as exc:
        rollback.run_rollback(plan, store)
    assert exc.value.code == "vault-changed"
    assert vstore.vault_mode(vault.root) == "store"
    assert not (vault.root / "store" / "state" / "upgrade.json").exists()


def test_second_rollback_from_a_stale_plan_refuses(tmp_path):
    vault, _live, store, _copy, _snap = _setup(tmp_path)
    first = rollback.plan_rollback(vault.root, store)
    second = rollback.plan_rollback(vault.root, store)
    rollback.run_rollback(first, store)
    with pytest.raises(errors.RollbackError) as exc:
        rollback.run_rollback(second, store)
    assert exc.value.code == "not-upgraded"
    assert not (vault.root / "store").exists()


def test_fold_and_writes_never_recreate_a_moved_store(tmp_path):
    vault, _live, store, _copy, _snap = _setup(tmp_path)
    data = b"x"
    digest = hashlib.sha256(data).hexdigest()
    rollback.run_rollback(rollback.plan_rollback(vault.root, store), store)
    with pytest.raises((errors.StoreError, OSError)):
        migrate.run_fold(store)
    with pytest.raises(OSError):
        store.objects.put(data, digest)
    assert not (vault.root / "store").exists()


def test_fold_save_checks_the_mode(tmp_path):
    vault, _live, store, _copy, _snap = _setup(tmp_path)
    (vault.root / "store" / "state" / "upgrade.json").write_text(
        json.dumps({"state": "rolling-back"}), encoding="utf-8")
    with pytest.raises(errors.StoreNotUpgraded):
        migrate._save(store, {}, state="running", waiting=0)


def test_fold_refuses_while_the_upgrade_lock_is_held(tmp_path):
    vault, _live, store, _copy, _snap = _setup(tmp_path)
    with safety.exclusive_lock(upgrade.upgrade_lock_path(vault.root), timeout=0):
        with pytest.raises(errors.StoreBusy):
            migrate.run_fold(store)


def test_unreadable_manifest_blocks_unless_forced(tmp_path):
    vault, live, _store, _copy, _snap = _setup(tmp_path)
    bad = vault.root / "store" / "entries" / ("f" * 32 + ".json")
    bad.write_bytes(b"{ not json")
    store = vstore.open_store(vault.root, live_roots=[live])
    plan = rollback.plan_rollback(vault.root, store)
    assert plan.problems and plan.problems[0][0] == "corrupt-manifests"
    with pytest.raises(errors.RollbackError):
        rollback.run_rollback(plan, store)
    forced = rollback.plan_rollback(vault.root, store, force=True)
    assert forced.problems == () and forced.corrupt == (bad.name,)
    result = rollback.run_rollback(forced, store)
    assert bad.name in result.message


def test_lost_migration_record_is_not_an_empty_upgrade(tmp_path):
    vault, live, store, copy, _snap = _setup(tmp_path)
    (vault.root / "store" / "state" / "migration.json").unlink()
    plan = rollback.plan_rollback(vault.root, store)
    assert plan.problems and plan.problems[0][0] == "migration-missing"
    with pytest.raises(errors.RollbackError):
        rollback.run_rollback(plan, store)
    ok = rollback.plan_rollback(vault.root, store, copy=copy)
    assert ok.problems == () and ok.copy == copy
    rollback.run_rollback(ok, store)
    assert vstore.vault_mode(vault.root) == "legacy"


def test_export_keeps_the_owners_catalog_fields(tmp_path):
    vault, live, store, _copy, _snap = _setup(tmp_path)
    made = operations.create_full_backup(Vault(vault.root), live, warnings=[])
    v = Vault(vault.root).load()
    v.upsert(dataclasses.replace(made, note="keep me", label="mine"))
    rollback.run_rollback(rollback.plan_rollback(vault.root, store), store)
    kept = Vault(vault.root).load().get(made.id)
    assert kept is not None and kept.note == "keep me" and kept.label == "mine"


def test_copy_is_opened_read_only(tmp_path, monkeypatch):
    vault, _live, store, copy, _snap = _setup(tmp_path)
    touched = []
    real = catalog.Vault._remove_stale_temp_files

    def spy(self):
        touched.append(self.root)
        return real(self)
    monkeypatch.setattr(catalog.Vault, "_remove_stale_temp_files", spy)
    rollback.run_rollback(rollback.plan_rollback(vault.root, store), store)
    assert copy not in touched


def test_upgrade_lock_does_not_create_a_missing_vault(tmp_path):
    vault, live, _store, _copy, _snap = _setup(tmp_path)
    gone = tmp_path / "gone"
    fake = dataclasses.replace(upgrade.UpgradePlan(
        gone, 0, 0, "", tmp_path / "d", 0, 0, 0, False, (), ()), problems=())
    with pytest.raises(errors.UpgradeError):
        upgrade.run_upgrade(fake)
    assert not gone.exists()
