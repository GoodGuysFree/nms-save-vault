"""U19: the classic tab re-routed - the whole-folder restore is the panic restore (its dialog),
per-slot Undo is the live operations' plan_undo, Clear slot shows the steps window, and the
Backups pane lists the vault through D3's one listing. Tmp vaults and synthetic Steam folders."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from nms_save_vault.core import liveops, operations, safety, savedir
from nms_save_vault.core import store as vstore
from nms_save_vault.core.catalog import Vault
from nms_save_vault.core.liveops import classic
from nms_save_vault.core.store import versionstore
from nms_save_vault.ui.organizer.panic import PanicDialog

from .conftest import steam_save
from .store_helpers import IDENT, steam_live


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
def world(tmp_path, gui_app, monkeypatch):
    """An upgraded vault with a full backup of the live folder, and the app pointed at it."""
    live = steam_live(tmp_path / "NMS")
    vault = Vault(tmp_path / "vault")
    operations.create_full_backup(vault, live, warnings=[])
    vstore.run_upgrade(vstore.plan_upgrade(vault.root, live_roots=[live]))
    backup = operations.create_full_backup(vault, live, warnings=[])        # store mode
    monkeypatch.setattr(gui_app, "vault", vault)
    monkeypatch.setattr(gui_app, "live_dir", live)
    monkeypatch.setattr(gui_app, "_require_live", lambda: True)
    return type("W", (), dict(vault=vault, live=live, backup=backup))


def _change_slot_one(live: Path):
    steam_save(live, 1, 0, "Changed", 100, IDENT, 1_790_100_000, extra="changed-a")
    steam_save(live, 1, 1, "Changed", 90, IDENT, 1_790_100_100, extra="changed-b")


def test_the_whole_folder_restore_is_the_panic_restore_dialog(gui_app, world):
    _change_slot_one(world.live)
    entry = next(e for e in gui_app._backup_entries() if e.id == world.backup.id)
    gui_app.on_restore({"type": "entry", "entry": entry})
    d = gui_app.panic_dialog
    assert isinstance(d, PanicDialog)
    _wait(gui_app, lambda: d.plan is not None)
    assert world.backup.id in d.choices.values() and d.plan.kind == "panic_restore"
    assert d.winfo_toplevel().title().startswith("Restore the whole folder")
    assert d.apply_button.cget("text") == d.plan.outcome
    assert d.account_check.winfo_manager() == "pack"          # Steam: the account data is a choice
    assert not d.account_var.get()
    for var in d.vars.values():
        var.set(True)
    d._update()
    d.apply()
    _wait(gui_app, lambda: not d.winfo_exists())
    assert savedir.scan_any(world.live).slots[1].display_name == "Main"
    assert gui_app.last_change.get().startswith("Last change")


def test_the_backups_pane_lists_store_entries_through_the_one_listing(gui_app, world):
    ids = {e.id for e in gui_app._backup_entries()}
    assert world.backup.id in ids
    gui_app._populate_backups()
    texts = {gui_app.backup_tree.item(r, "text") for r in gui_app.backup_tree.get_children("")}
    assert world.backup.id in texts


def test_per_slot_undo_goes_through_plan_undo(gui_app, world, monkeypatch):
    other = world.live.parent / "src"
    steam_save(other, 1, 0, "Elsewhere", 5000, IDENT + 7, 1_790_200_000, extra="e-a")
    classic.repopulate(world.vault, other, 1, world.live, 2)
    seen = []
    real = liveops.plan_undo
    monkeypatch.setattr(liveops, "plan_undo", lambda ctx, op_id: seen.append(op_id) or real(ctx, op_id))
    gui_app.on_undo_slot(world.live, 2)
    _wait(gui_app, lambda: seen)
    _wait(gui_app, lambda: getattr(gui_app, "plan_dialog", None) is not None
          and gui_app.plan_dialog.winfo_exists() and gui_app.plan_dialog.plan is not None)
    d = gui_app.plan_dialog
    assert d.plan.undo_of == seen[0] and d.winfo_toplevel() is not None
    d.cancel()
