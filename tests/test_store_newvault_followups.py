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


def test_opening_a_new_vault_starts_the_store_at_once_and_live_ops_work(tmp_path, new_vault_store):
    live = steam_live(tmp_path / "live")
    vault = Vault(tmp_path / "fresh")
    vault.ensure()                                   # the GUI opening its default vault
    vault.load()
    assert versionstore.vault_mode(vault.root) == "store"
    assert rollback.created_as_store(vault.root)
    assert not (vault.root / "catalog.json").exists()       # no catalog until something is added
    assert Vault(vault.root).load().entries == []
    assert not upgrade.upgrade_needed(vault.root)
    ops.create_full_backup(vault, live, warnings=[])        # no mode change in mid-session
    assert versionstore.vault_mode(vault.root) == "store"
    ops.promote_member(vault, live, 1, 1, allow_game_running=True)   # a live op: no "upgrade first"


def test_the_record_is_written_before_FORMAT(tmp_path):
    root = tmp_path / "v"
    versionstore.create_store(root, record={"created_in_store_mode": True})
    assert rollback.created_as_store(root)
    seen = []
    real = versionstore.safety.atomic_write_bytes

    def spy(path, data, **kw):
        seen.append(path.name)
        return real(path, data, **kw)

    root2 = tmp_path / "v2"
    versionstore.safety.atomic_write_bytes, saved = spy, real
    try:
        versionstore.create_store(root2, record={"created_in_store_mode": True})
    finally:
        versionstore.safety.atomic_write_bytes = saved
    assert seen.index("migration.json") < seen.index("FORMAT")


def test_concurrent_first_opens_start_one_store(tmp_path, new_vault_store):
    import threading

    root = tmp_path / "v"
    root.mkdir()
    barrier = threading.Barrier(6)
    results, errors = [], []

    def go():
        barrier.wait()
        try:
            results.append(versionstore.start_new_vault(root))
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    threads = [threading.Thread(target=go) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 1 and not errors
    assert rollback.created_as_store(root)


def test_a_new_store_vault_is_a_vault_to_import_from_and_merge(tmp_path, new_vault_store):
    a, entry, _ = _new_vault_with_backup(tmp_path, "A")
    assert versionstore.vault_mode(a.root) == "store" and catalog.looks_like_vault_dir(a.root)
    (a.root / "catalog.json").unlink(missing_ok=True)    # even without a catalog: FORMAT says so
    assert catalog.looks_like_vault_dir(a.root)

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
    a.ensure()
    b = Vault(tmp_path / "B")
    b.ensure()
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


def test_old_folders_copied_into_an_empty_folder_before_the_app_opens_it_upgrade_as_usual(
        tmp_path, monkeypatch, new_vault_store):
    fresh = tmp_path / "_SaveVault"
    fresh.mkdir()                                    # made by hand, still empty
    _copy_old_vault_in(tmp_path, monkeypatch, fresh)
    Vault(fresh).ensure()                            # not a vault the app created: left alone
    assert versionstore.vault_mode(fresh) == "legacy" and upgrade.upgrade_needed(fresh)


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


def test_a_fold_that_failed_for_an_entry_lists_it_instead_of_offering_it_again(
        tmp_path, monkeypatch, new_vault_store):
    fresh, live = _new_vault_with_snapshot(tmp_path, "_SaveVault")
    old, entry, _ = _copy_old_vault_in(tmp_path, monkeypatch, fresh.root)
    store = versionstore.open_store(fresh.root, live_roots=[live])

    def bad(store_, records, folder):
        raise migrate.FoldVerifyError("the folder and the store differ (x)")

    monkeypatch.setattr(migrate, "check_folder", bad)
    report = migrate.run_fold(store, live_roots=[live])
    assert [k for k, _w in report.failed] == [entry.id]
    assert migrate.unfolded(store, live_roots=[live]) == []
    assert [k for k, _w in migrate.fold_failed(fresh.root)] == [entry.id]


def test_the_organizer_made_at_start_sees_the_store_of_a_new_vault(tmp_path, new_vault_store):
    from nms_save_vault.core import platform, state as appstate
    from nms_save_vault.core.liveops import planner
    from nms_save_vault.core.liveops.context import context_for
    from nms_save_vault.core.organizer import Organizer

    monkey = pytest.MonkeyPatch()
    monkey.setattr(planner, "XBOX_SYNC_SECONDS", 0)
    try:
        live = steam_live(tmp_path / "live")
        src = appstate.Source(id="steam-a", platform="steam", account=live.name, path=str(live),
                              label="", writable=True, enabled=True)
        vault = Vault(tmp_path / "_SaveVault")
        vault.ensure()
        vault.load()
        host = platform.Host.current()
        org = Organizer(vault.root, [src], host=host, game_running=lambda: False)
        org.refresh()
        ops.create_full_backup(vault, live, warnings=[])
        org.refresh()
        ctx = context_for(org, host=host, machine_id="m-00000000000a", app_version="t",
                          game_probe=lambda: False)
        assert org.version_store is not None and planner.vault_problem(ctx) is None
    finally:
        monkey.undo()
