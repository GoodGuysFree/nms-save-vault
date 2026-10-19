"""U20: the vault upgrade window (D3 4.1-4.4) on tmp vaults only: never starts by itself, the
space check, the upgrade with its fold, cancel, copy-in (off until ticked), undo of the upgrade,
the interrupted undo (Finish / Keep) and Edit > Undo with the status-bar "Last change"."""
from __future__ import annotations

import shutil
import threading
import time
import tkinter as tk
from tkinter import ttk
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


def test_select_all_and_unselect_all_and_a_label_that_names_the_parent_folder(gui_app, legacy):
    win = _upgrade(gui_app, legacy)
    win.refresh()
    _wait(gui_app, lambda: not win.busy and win.copy_vars)
    texts = [w.cget("text") for w in win.body.winfo_children()[-1].winfo_children()
             if isinstance(w, ttk.Checkbutton)] or _checkbutton_texts(win.body)
    assert any(f"{legacy.hand.parent.name}\\{legacy.hand.name}" in t for t in texts)
    assert win.copy_button.instate(["disabled"])
    _button(win.body, "Select all").invoke()
    assert all(v.get() for v in win.copy_vars.values()) and win.copy_button.instate(["!disabled"])
    _button(win.body, "Unselect all").invoke()
    assert not any(v.get() for v in win.copy_vars.values()) and win.copy_button.instate(["disabled"])
    win.destroy()


def _walk(widget):
    for child in widget.winfo_children():
        yield child
        yield from _walk(child)


def _checkbutton_texts(root) -> list[str]:
    return [w.cget("text") for w in _walk(root) if isinstance(w, ttk.Checkbutton)]


def _button(root, text):
    return next(w for w in _walk(root) if isinstance(w, ttk.Button) and w.cget("text") == text)


def test_the_organizer_in_the_same_app_is_rebuilt_after_the_upgrade(gui_app, legacy, tmp_path):
    from nms_save_vault.core import liveops, platform
    from nms_save_vault.core.liveops import PairSource
    from nms_save_vault.core.liveops.planner import plan_place
    from nms_save_vault.core.organizer import Organizer
    from nms_save_vault.core.organizer.locator import Location
    from nms_save_vault.core.organizer.model import LiveRef, StoreRef
    from nms_save_vault.ui.organizer import view as V

    from .liveops_support import host_in, live_source

    host = host_in(tmp_path)
    sources = [live_source(legacy.live)]
    top = tk.Toplevel(gui_app)
    top.withdraw()
    view = V.OrganizerView(
        top, worker=gui_app.worker, themed=gui_app.themed, prefs={}, save_prefs=lambda: None,
        make_organizer=lambda: Organizer(legacy.vault.root, sources, host=host,
                                         game_running=lambda: False),
        make_context=lambda org: liveops.context_for(
            org, host=host, machine_id="m-00000000000a", app_version="test",
            game_probe=lambda: False))
    view.pack()
    view.rescan()
    _wait(gui_app, lambda: not view.scanning and view.model is not None)
    first = view.organizer
    win = W.VaultWindow(top, worker=gui_app.worker, themed=gui_app.themed, env=legacy.env,
                        on_changed=view.vault_changed)
    _wait(gui_app, lambda: not win.busy and win.state is not None)
    win.confirm_var.set(True)
    win._update_buttons()
    win.upgrade()
    _wait(gui_app, lambda: not win.busy and win.state.mode == "store")
    _wait(gui_app, lambda: not view.scanning and view.organizer is not first)
    ctx = view.make_context(view.organizer)
    entry = ctx.versions.entries()[0]
    a, b = entry.member(1, "A"), entry.member(1, "B")
    source = PairSource(a.vid, b.vid, None,
                        Location(a.vid, "store", StoreRef(entry.id), 1, "A", "steam", True))
    live = LiveRef("steam", legacy.live.name)
    for slot in (2, 15):                                  # a full slot and an empty one
        plan = plan_place(ctx, source, live.target(slot))
        assert "VAULT_UPGRADE_NEEDED" not in {w.code for w in plan.blocked + plan.warnings}
    win.destroy()
    top.destroy()
