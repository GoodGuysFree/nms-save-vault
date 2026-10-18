"""A NEW store-mode vault in the rest of the app: import, the vault window and the undo, a vault
that is only browsed, and old folders copied into a store vault (review of 1ab33af)."""
from __future__ import annotations

import shutil
import time
import tkinter as tk
from types import SimpleNamespace

import pytest

from nms_save_vault.core import catalog, operations as ops, safety
from nms_save_vault.core import store as vstore
from nms_save_vault.core.catalog import Vault
from nms_save_vault.core.store import classic, migrate, rollback, upgrade, versionstore
from nms_save_vault.ui.organizer import vaultwin as W

from .store_helpers import steam_live


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache-home"))
    monkeypatch.setattr(safety, "is_game_running", lambda: False)
    monkeypatch.setattr(versionstore.discover, "all_live_paths", lambda *a, **k: [])


def _wait(app, condition, timeout: float = 30.0) -> None:
    end = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < end, "timed out"
        app.update()
        time.sleep(0.005)


def _new_vault_with_backup(tmp_path, name):
    live = steam_live(tmp_path / f"live_{name}")
    vault = Vault(tmp_path / name)
    entry = ops.create_full_backup(vault, live, warnings=[])
    return vault, entry, live


def test_browsing_a_new_vault_creates_no_store_but_the_first_write_does(tmp_path, new_vault_store):
    vault = Vault(tmp_path / "fresh")
    vault.ensure()                                   # the GUI opening its default vault
    vault.load()
    assert versionstore.vault_mode(vault.root) == "legacy"
    assert not catalog.looks_like_vault_dir(vault.root)
    vault.save()                                     # the first write
    assert versionstore.vault_mode(vault.root) == "store"
    assert (vault.root / "catalog.json").is_file()
    assert Vault(vault.root).load().entries == []


def test_a_new_store_vault_is_a_vault_to_import_from_and_merge(tmp_path, new_vault_store):
    a, entry, _ = _new_vault_with_backup(tmp_path, "A")
    assert versionstore.vault_mode(a.root) == "store" and catalog.looks_like_vault_dir(a.root)
    (a.root / "catalog.json").unlink()               # even without a catalog: FORMAT says so
    assert catalog.looks_like_vault_dir(a.root)
    Vault(a.root).save()

    b = Vault(tmp_path / "B")
    b.ensure()
    ops.create_full_backup(b, steam_live(tmp_path / "live_b"), warnings=[])   # B is a store now
    preview = ops.preview_vault_import(b, a.root)
    assert preview["merge"] and entry.id in [e.id for e in preview["new"]]
    result = ops.import_vault(b, a.root, copy_into_vault=True)
    assert entry.id in [e.id for e in classic.list_entries(b, live_roots=[])]
    assert result is not None


def test_an_empty_new_vault_is_importable_too(tmp_path, new_vault_store):
    a = Vault(tmp_path / "A")
    a.save()
    b = Vault(tmp_path / "B")
    b.save()
    assert ops.preview_vault_import(b, a.root)["total"] == 0


def test_the_undo_of_the_upgrade_has_nothing_to_do_in_a_new_vault(tmp_path, new_vault_store):
    a, _entry, _ = _new_vault_with_backup(tmp_path, "A")
    assert rollback.created_as_store(a.root)
    store = versionstore.open_store(a.root, live_roots=())
    plan = rollback.plan_rollback(a.root, store)
    assert [c for c, _t in plan.problems] == ["new-vault"]
    assert "no upgrade to undo" in plan.problems[0][1] and plan.problems[0][1].isascii()
    assert not upgrade.upgrade_needed(a.root)


def test_an_upgraded_vault_is_not_a_new_one(tmp_path, monkeypatch):
    monkeypatch.setattr(versionstore, "NEW_VAULTS_USE_STORE", False)
    old, _e, live = _new_vault_with_backup(tmp_path, "OLD")
    vstore.run_upgrade(vstore.plan_upgrade(old.root, live_roots=[live]))
    assert not rollback.created_as_store(old.root)


def _new_vault_with_snapshot(tmp_path, name):
    live = steam_live(tmp_path / f"live_{name}")
    vault = Vault(tmp_path / name)
    ops.snapshot_live(vault, live, "mine")           # another id than the old vault's backup
    return vault, live


def _copy_old_vault_in(tmp_path, monkeypatch, fresh_root):
    monkeypatch.setattr(versionstore, "NEW_VAULTS_USE_STORE", False)
    old, entry, live = _new_vault_with_backup(tmp_path, "OLD")
    monkeypatch.setattr(versionstore, "NEW_VAULTS_USE_STORE", True)
    shutil.copytree(old.root, fresh_root, dirs_exist_ok=True)
    return old, entry, live


def test_old_folders_copied_into_a_browsed_new_vault_upgrade_as_usual(tmp_path, monkeypatch,
                                                                       new_vault_store):
    fresh = Vault(tmp_path / "_SaveVault")
    fresh.ensure()                                   # the app was started first
    _copy_old_vault_in(tmp_path, monkeypatch, fresh.root)
    assert versionstore.vault_mode(fresh.root) == "legacy" and upgrade.upgrade_needed(fresh.root)


def test_old_folders_copied_into_a_store_vault_are_offered_for_the_fold(tmp_path, monkeypatch,
                                                                         new_vault_store):
    fresh, live = _new_vault_with_snapshot(tmp_path, "_SaveVault")      # already a store
    old, entry, _ = _copy_old_vault_in(tmp_path, monkeypatch, fresh.root)
    store = versionstore.open_store(fresh.root, live_roots=[live])
    assert versionstore.vault_mode(fresh.root) == "store"
    assert [e.id for e in migrate.unfolded(store, live_roots=[live])] == [entry.id]
    report = migrate.run_fold(store, live_roots=[live])
    assert report.verified == 1 and not report.failed
    assert migrate.unfolded(store, live_roots=[live]) == []
    assert store.has_entry(entry.id)


@pytest.fixture
def env(tmp_path):
    return lambda root, live: W.VaultEnv(root, [live], None, "test")


def _window(gui_app, env):
    top = tk.Toplevel(gui_app)
    top.withdraw()
    win = W.VaultWindow(top, worker=gui_app.worker, themed=gui_app.themed, env=env)
    _wait(gui_app, lambda: not win.busy and win.state is not None)
    return win


def test_the_window_of_a_new_vault_cannot_undo_an_upgrade(gui_app, tmp_path, env, new_vault_store):
    vault, _e, live = _new_vault_with_backup(tmp_path, "A")
    win = _window(gui_app, env(vault.root, live))
    assert win.state.mode == "store" and win.state.new_vault
    assert win.undo_button.instate(["disabled"])
    assert win.state.unfolded == 0
    plan = W.rollback_plan_job(win.env, False)
    assert [c for c, _t in plan.problems] == ["new-vault"]


def test_the_window_offers_the_fold_for_old_folders_in_a_store_vault(gui_app, tmp_path, env,
                                                                      monkeypatch, new_vault_store):
    vault, live = _new_vault_with_snapshot(tmp_path, "_SaveVault")
    _copy_old_vault_in(tmp_path, monkeypatch, vault.root)
    win = _window(gui_app, env(vault.root, live))
    assert win.state.unfolded == 1 and getattr(win, "fold_button", None) is not None
    win.fold_button.invoke()
    _wait(gui_app, lambda: not win.busy and win.state is not None and win.state.unfolded == 0)
