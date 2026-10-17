"""U20: the vault upgrade window (D3 4.1-4.4) on tmp vaults only: never starts by itself, the
space check, the upgrade with its fold, cancel, copy-in (off until ticked), undo of the upgrade,
the interrupted undo (Finish / Keep) and Edit > Undo with the status-bar "Last change"."""
from __future__ import annotations

import shutil
import threading
import time
import tkinter as tk
from pathlib import Path
from types import SimpleNamespace

import pytest

from nms_save_vault.core import operations, safety
from nms_save_vault.core import store as vstore
from nms_save_vault.core.catalog import Vault
from nms_save_vault.core.store import rollback, upgrade, versionstore
from nms_save_vault.ui.organizer import vaultwin as W

from .store_helpers import steam_live


def _wait(app, condition, timeout: float = 30.0) -> None:
    end = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < end, "timed out"
        app.update()
        time.sleep(0.005)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache-home"))
    monkeypatch.setattr(safety, "is_game_running", lambda: False)
    monkeypatch.setattr(versionstore.discover, "all_live_paths", lambda *a, **k: [])


@pytest.fixture
def legacy(tmp_path):
    live = steam_live(tmp_path / "NMS")
    vault = Vault(tmp_path / "vault")
    operations.create_full_backup(vault, live, warnings=[])
    operations.snapshot_live(vault, live, "test")
    hand = tmp_path / "my copy"
    shutil.copytree(live, hand)
    (hand / "save.hg").write_bytes(b"a hand-made copy that differs")
    handmade = operations.import_backup(vault, hand, copy_into_vault=False, warnings=[])
    return SimpleNamespace(vault=vault, live=live, hand=hand, handmade=handmade.id,
                           env=W.VaultEnv(vault.root, [live], None, "test"))


def _window(gui_app, legacy, changed=None):
    top = tk.Toplevel(gui_app)
    top.withdraw()
    win = W.VaultWindow(top, worker=gui_app.worker, themed=gui_app.themed, env=legacy.env,
                        on_changed=changed)
    _wait(gui_app, lambda: not win.busy and win.state is not None)
    return win


def _upgrade(gui_app, legacy):
    win = _window(gui_app, legacy)
    win.confirm_var.set(True)
    win._update_buttons()
    win.upgrade()
    _wait(gui_app, lambda: not win.busy and win.state is not None and win.state.mode == "store")
    return win


def test_it_never_starts_by_itself_and_waits_for_the_confirmation(gui_app, legacy):
    win = _window(gui_app, legacy)
    for _ in range(20):
        gui_app.update()
        time.sleep(0.01)
    assert vstore.vault_mode(legacy.vault.root) == "legacy"
    assert win.state.plan is not None and win.state.plan.files > 0
    assert win.upgrade_button.instate(["disabled"])
    win.confirm_var.set(True)
    win._update_buttons()
    assert win.upgrade_button.instate(["!disabled"])
    win.destroy()


def test_too_little_space_blocks_the_upgrade_and_says_why(gui_app, legacy, monkeypatch):
    real = shutil.disk_usage
    monkeypatch.setattr(upgrade.shutil, "disk_usage",
                        lambda p: SimpleNamespace(total=real(p).total, used=0, free=1000))
    win = _window(gui_app, legacy)
    assert win.state.plan.problems and win.state.plan.problems[0][0] == "space"
    win.confirm_var.set(True)
    win._update_buttons()
    assert win.upgrade_button.instate(["disabled"])
    win.destroy()


def test_the_upgrade_copies_aside_folds_and_shows_the_store(gui_app, legacy):
    changed = []
    win = _window(gui_app, legacy, lambda: changed.append(1))
    win.confirm_var.set(True)
    win._update_buttons()
    win.upgrade()
    assert win.busy and win.stop_button.winfo_manager() == "pack"
    _wait(gui_app, lambda: not win.busy and win.state.mode == "store")
    assert changed and vstore.vault_mode(legacy.vault.root) == "store"
    st = win.state.status
    assert st.copy_aside is not None and st.fold_total == st.fold_done > 0
    assert any(p.name.startswith("vault.pre-store-") for p in legacy.vault.root.parent.iterdir())
    assert win.undo_button is not None
    win.destroy()


def test_stop_cancels_a_running_job_and_the_window_refuses_to_close_meanwhile(
        gui_app, legacy, monkeypatch):
    seen = {}

    def slow(env, dest, box, cancel):
        seen["cancel"] = cancel
        cancel.wait(10)
        raise upgrade.UpgradeError("cancelled", "The upgrade was stopped. Your vault was not "
                                                "changed.")
    monkeypatch.setattr(W, "upgrade_job", slow)
    win = _window(gui_app, legacy)
    win.confirm_var.set(True)
    win._update_buttons()
    win.upgrade()
    _wait(gui_app, lambda: "cancel" in seen)
    win.cancel()                                          # Close / Esc / X: refused mid-run
    assert win.winfo_exists()
    win._stop()
    _wait(gui_app, lambda: not win.busy)
    assert seen["cancel"].is_set() and "stopped" in win.error.cget("text")
    assert vstore.vault_mode(legacy.vault.root) == "legacy"
    win.destroy()


def test_copy_in_is_off_until_ticked_and_copies_only_what_is_ticked(gui_app, legacy):
    win = _upgrade(gui_app, legacy)
    win.refresh()
    _wait(gui_app, lambda: not win.busy and win.copy_vars)
    assert list(win.copy_vars) == [legacy.handmade]
    assert not any(v.get() for v in win.copy_vars.values())       # default OFF
    assert win.copy_button.instate(["disabled"])
    win.copy_vars[legacy.handmade].set(True)
    win._update_buttons()
    assert win.copy_button.instate(["!disabled"])
    before = sorted(p.name for p in legacy.hand.iterdir())
    win.copy_selected()
    _wait(gui_app, lambda: not win.busy and not win.copy_vars)
    assert vstore.open_store(legacy.vault.root, live_roots=[legacy.live]).has_entry(legacy.handmade)
    assert sorted(p.name for p in legacy.hand.iterdir()) == before
    win.destroy()


def test_undo_the_upgrade_from_the_window(gui_app, legacy):
    win = _upgrade(gui_app, legacy)
    win.plan_undo()
    _wait(gui_app, lambda: not win.busy and win.rollback_button is not None)
    assert win.rollback_button.instate(["!disabled"])
    win.run_undo()
    _wait(gui_app, lambda: not win.busy and win.state.mode == "legacy")
    assert vstore.vault_mode(legacy.vault.root) == "legacy"
    assert win.upgrade_button is not None
    win.destroy()


def _interrupt_undo(legacy, gui_app):
    """Upgrade, then an undo that crashes before its commit point."""
    vstore.run_upgrade(vstore.plan_upgrade(legacy.vault.root, live_roots=[legacy.live]))
    store = vstore.open_store(legacy.vault.root, live_roots=[legacy.live])

    def crash(point):
        if point == "rollback.before_store_move":
            raise OSError(5, "power loss")
    plan = rollback.plan_rollback(legacy.vault.root, store)
    with pytest.raises(OSError):
        rollback.run_rollback(plan, store, faults=crash)
    assert W.needs_attention(legacy.vault.root)


def test_an_interrupted_undo_can_be_finished(gui_app, legacy):
    _interrupt_undo(legacy, gui_app)
    win = _window(gui_app, legacy)
    assert win.state.interrupted_undo and win.finish_button and win.keep_button
    win.finish_undo()
    _wait(gui_app, lambda: not win.busy and win.rollback_button is not None)
    win.run_undo()
    _wait(gui_app, lambda: not win.busy and win.state.mode == "legacy")
    assert not W.needs_attention(legacy.vault.root)
    win.destroy()


def test_an_interrupted_undo_can_be_kept_as_an_upgrade(gui_app, legacy):
    _interrupt_undo(legacy, gui_app)
    win = _window(gui_app, legacy)
    win.keep()
    _wait(gui_app, lambda: not win.busy and win.state.mode == "store")
    assert vstore.vault_mode(legacy.vault.root) == "store" and not W.needs_attention(
        legacy.vault.root)
    win.destroy()


def test_the_app_offers_the_window_at_start_only_after_an_interrupted_undo(gui_app, legacy,
                                                                       monkeypatch):
    opened = []
    monkeypatch.setattr(gui_app, "open_vault_window", lambda: opened.append(1))
    monkeypatch.setattr(gui_app.vault, "root", legacy.vault.root)
    gui_app._offer_interrupted_undo()
    assert opened == []
    _interrupt_undo(legacy, gui_app)
    gui_app._offer_interrupted_undo()
    assert opened == [1]


def test_the_tools_menu_opens_it_and_nothing_else_does(gui_app):
    menu = gui_app.menubar
    labels = []
    for i in range(menu.index("end") + 1):
        if menu.type(i) == "cascade":
            sub = menu.nametowidget(menu.entrycget(i, "menu"))
            labels += [sub.entrycget(j, "label") for j in range(sub.index("end") + 1)
                       if sub.type(j) == "command"]
    assert "Vault upgrade..." in labels and "Undo the last change to the active live folder" in labels
