"""Regression tests from the U19 / U21-22 review: a store entry's slot row never reads the current
directory; the whole-folder restore keeps its old abilities (repair a damaged Xbox index, account
data, the kept-account note); Undo of a slot move offers a copy before the empty-slot steps and
never rolls back other slots silently; redaction; plan jobs go ahead of scans in the UI worker.
Tmp vaults and synthetic folders only."""
from __future__ import annotations

import shutil
import threading
import time

import pytest

from nms_save_vault import gui
from nms_save_vault.core import aliases, msstore, operations, safety, savedir
from nms_save_vault.core import operations as ops
from nms_save_vault.core import state as appstate
from nms_save_vault.core import store as vstore
from nms_save_vault.core.catalog import Vault
from nms_save_vault.core.liveops import classic
from nms_save_vault.core.store import versionstore
from nms_save_vault.ui.organizer.panic import PanicDialog

from .conftest import steam_save
from .store_helpers import IDENT, steam_live


def _wait(app, cond, timeout: float = 60.0) -> None:
    end = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < end, "timed out"
        app.update()
        time.sleep(0.005)


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    from nms_save_vault.core.liveops import planner

    monkeypatch.setattr(safety, "is_game_running", lambda: False)
    monkeypatch.setattr(versionstore.discover, "all_live_paths", lambda *a, **k: [])
    monkeypatch.setattr(planner, "XBOX_SYNC_SECONDS", 0)


@pytest.fixture
def world(tmp_path, gui_app, monkeypatch):
    live = steam_live(tmp_path / "NMS")
    vault = Vault(tmp_path / "vault")
    operations.create_full_backup(vault, live, warnings=[])
    vstore.run_upgrade(vstore.plan_upgrade(vault.root, live_roots=[live]))
    backup = operations.create_full_backup(vault, live, warnings=[])        # store mode
    monkeypatch.setattr(gui_app, "vault", vault)
    monkeypatch.setattr(gui_app, "live_dir", live)
    monkeypatch.setattr(gui_app, "_require_live", lambda: True)
    source = appstate.Source(id="steam-test", platform="steam", account=live.name,
                             path=str(live), label="Test live")
    monkeypatch.setattr(gui_app.state, "sources", [source])
    return type("W", (), dict(vault=vault, live=live, backup=backup))


def _inline_worker(app, monkeypatch):
    def run(fn, force, message):
        try:
            return {"result": fn(force)}
        except Exception as exc:  # noqa: BLE001
            return {"error": exc}
    monkeypatch.setattr(app, "_run_worker", run)


def _store_slot_row(app, world):
    app._populate_backups()
    tree = app.backup_tree
    node = next(r for r in tree.get_children("") if tree.item(r, "text") == world.backup.id)
    return app._meta_for(tree, tree.get_children(node)[0]), app._meta_for(tree, node)["entry"]


# --- 1. a store entry never reads the current directory ----------------------------------------


def test_a_store_entry_slot_row_copies_from_the_store_not_the_cwd(gui_app, world, monkeypatch,
                                                                tmp_path):
    meta, entry = _store_slot_row(gui_app, world)
    assert entry.path == "" and meta["dir"] == ""
    cwd = tmp_path / "cwd" / "st_76561190000000009"
    steam_save(cwd, meta["slot"], 0, "FromCWD", 1, IDENT + 50, 1_790_300_000, extra="cwd")
    monkeypatch.chdir(cwd)
    errors = []
    monkeypatch.setattr(gui, "_showerror", lambda t, m: errors.append(m))
    monkeypatch.setattr(gui, "_showinfo", lambda *a: None)
    monkeypatch.setattr(gui, "_askyesno", lambda *a, **k: True)
    _inline_worker(gui_app, monkeypatch)
    gui_app.on_copy_slot(source=gui_app._source_from(meta), dest_dir=world.live, dest_slot=3,
                         ask=False)
    assert errors == []
    held = savedir.scan_any(world.live).slots[3].display_name
    assert held == "Main" and held != "FromCWD"          # the backup's slot, read through the store


def test_an_empty_or_relative_source_folder_is_refused_everywhere(world, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for bad in ("", ".", "some/relative"):
        with pytest.raises(ops.OperationError):
            classic.repopulate(world.vault, bad, 1, world.live, 3)
        with pytest.raises(ops.OperationError):
            ops.extract_slot(world.vault, bad, 1, warnings=[])
        with pytest.raises(ops.OperationError):
            classic.folder_pair(bad, 1)


def test_extracting_a_slot_of_a_store_entry_goes_through_the_store(gui_app, world):
    meta, entry = _store_slot_row(gui_app, world)
    before = {e.id for e in gui_app._backup_entries()}
    made = classic.extract_from_entry(world.vault, entry.id, meta["slot"], warnings=[])
    assert made.id not in before and made.id in {e.id for e in gui_app._backup_entries()}


# --- 2. the whole-folder restore keeps its old abilities ------------------------------------------


def _dialog(app, entry):
    app.on_restore({"type": "entry", "entry": entry})
    d = app.panic_dialog
    assert isinstance(d, PanicDialog)
    return d


def _go(app, d):
    for v in d.vars.values():
        v.set(True)
    d._update()
    d.apply()
    _wait(app, lambda: not d.winfo_exists() or (not d.busy and d.error.cget("text")))


def test_a_damaged_xbox_index_is_repaired_by_the_restore(gui_app, make_wgs_account, tmp_path,
                                                         monkeypatch, dialogs):
    dialogs.expect()
    live = make_wgs_account(tmp_path / "live", [
        ("Slot1Auto", '{"a":1}', "Keep", "", 1, 100), ("Slot1Manual", '{"b":1}', "Keep", "", 1, 90)])
    vault = Vault(tmp_path / "vault")
    vault.ensure()
    vstore.create_store(vault.root)
    ops.promote_member(vault, live, 1, 1, allow_game_running=True)
    bk = tmp_path / "bk" / "wgs" / live.name
    shutil.copytree(live, bk)
    entry = ops.import_backup(vault, bk, label="bk")
    index = live / "containers.index"
    index.write_bytes(index.read_bytes()[:-30])
    monkeypatch.setattr(gui_app, "vault", vault)
    monkeypatch.setattr(gui_app, "live_dir", live)
    monkeypatch.setattr(gui_app, "_require_live", lambda: True)
    d = _dialog(gui_app, entry)
    _wait(gui_app, lambda: d.repair)
    assert d.plan is None and d.apply_button.instate(["disabled"])    # it asks first
    assert d.account_check.winfo_manager() == ""                      # Xbox: no account-data box
    d.repair_var.set(True)
    d._update()
    assert d.apply_button.instate(["!disabled"])
    d.apply()
    _wait(gui_app, lambda: not d.winfo_exists() or (not d.busy and d.error.cget("text")))
    msstore.split_index(index.read_bytes())                           # reads again
    assert any((vault.root / "damaged-index").iterdir())              # the damaged one is kept
    notes = dialogs.take()                                            # and the owner is told
    assert any("index was damaged" in m for _k, _t, m in notes)


def test_a_ticked_account_data_box_restores_it(gui_app, world, dialogs):
    dialogs.expect()
    acct = world.live / "accountdata.hg"
    original = acct.read_bytes()
    acct.write_bytes(original[:-4] + b"XXXX")
    steam_save(world.live, 1, 0, "Changed", 100, IDENT, 1_790_100_000, extra="changed-a")
    d = _dialog(gui_app, world.backup)
    _wait(gui_app, lambda: d.plan is not None)
    assert d.account_check.winfo_manager() == "pack" and not d.account_var.get()
    d.account_var.set(True)
    d._replan()
    _wait(gui_app, lambda: d.plan is not None)
    assert "ACCOUNTDATA_ROLLBACK" in d.plan.confirmations
    _go(gui_app, d)
    assert acct.read_bytes() == original
    assert savedir.scan_any(world.live).slots[1].display_name == "Main"
    assert any("restores the account data" in m for _k, _t, m in dialogs.take())


def test_an_unticked_restore_keeps_the_folders_account_data_and_says_so(gui_app, world,
                                                                      dialogs):
    dialogs.expect()
    acct = world.live / "accountdata.hg"
    mine = acct.read_bytes()[:-4] + b"MINE"
    acct.write_bytes(mine)
    d = _dialog(gui_app, world.backup)
    _wait(gui_app, lambda: d.plan is not None)
    _go(gui_app, d)
    assert acct.read_bytes() == mine
    assert any(ops.ACCOUNT_DATA_KEPT in m for _k, _t, m in dialogs.take())


def test_a_steam_folder_with_no_account_data_gets_the_backups(gui_app, world, dialogs):
    dialogs.expect()
    (world.live / "accountdata.hg").unlink()
    d = _dialog(gui_app, world.backup)
    _wait(gui_app, lambda: d.plan is not None)
    _go(gui_app, d)
    assert (world.live / "accountdata.hg").is_file()
    dialogs.take()


# --- 3 and 4. Undo of a slot move -------------------------------------------------------------------


def _move_into_empty_slot_three(world):
    other = world.live.parent / "src" / "st_76561190000000008"
    steam_save(other, 1, 0, "Elsewhere", 5000, IDENT + 7, 1_790_200_000, extra="e-a")
    classic.repopulate(world.vault, other, 1, world.live, 3)
    steam_save(world.live, 3, 0, "Elsewhere", 9000, IDENT + 7, 1_790_400_000, extra="played")


def test_undo_into_an_empty_slot_offers_a_copy_before_the_steps_built_in_the_worker(
        gui_app, world, monkeypatch):
    _move_into_empty_slot_three(world)
    threads, order = [], []
    real = classic.empty_guide

    def guide(v, d, s):
        threads.append(threading.current_thread() is threading.main_thread())
        return real(v, d, s)
    monkeypatch.setattr(classic, "empty_guide", guide)
    monkeypatch.setattr(gui, "_askyesno", lambda t, m: order.append("ask") or True)
    monkeypatch.setattr(classic, "keep_copy", lambda *a, **k: order.append("keep"))
    monkeypatch.setattr(gui, "StepsWindow", lambda master, **kw: order.append("steps"))
    gui_app.on_undo_slot(world.live, 3)
    _wait(gui_app, lambda: "steps" in order)
    assert order == ["ask", "keep", "steps"] and threads == [False]


def test_a_failed_copy_stops_the_undos_steps(gui_app, world, monkeypatch):
    _move_into_empty_slot_three(world)
    errors, steps = [], []

    def refuse(*a, **k):
        raise ops.OperationError("the vault is full")
    monkeypatch.setattr(gui, "_askyesno", lambda *a, **k: True)
    monkeypatch.setattr(classic, "keep_copy", refuse)
    monkeypatch.setattr(gui, "_showerror", lambda t, m: errors.append(m))
    monkeypatch.setattr(gui, "StepsWindow", lambda master, **kw: steps.append(kw))
    gui_app.on_undo_slot(world.live, 3)
    _wait(gui_app, lambda: errors)
    assert steps == [] and "the vault is full" in errors[0]


def test_per_slot_undo_after_a_whole_folder_restore_asks_and_never_rolls_back_silently(
        gui_app, world, monkeypatch):
    steam_save(world.live, 1, 0, "Changed", 100, IDENT, 1_790_100_000, extra="changed-a")
    ops.restore_full(world.vault, world.backup, world.live, allow_game_running=True)
    plans, asked, whole = [], [], []
    monkeypatch.setattr(gui, "PlanDialog", lambda master, **kw: plans.append(kw))
    monkeypatch.setattr(gui, "_askyesno", lambda t, m: asked.append(m) or False)
    monkeypatch.setattr(gui_app, "on_undo", lambda d=None: whole.append(d))
    gui_app.on_undo_slot(world.live, 1)
    _wait(gui_app, lambda: asked)
    assert "whole-folder restore" in asked[0] and plans == [] and whole == []   # declined
    monkeypatch.setattr(gui, "_askyesno", lambda t, m: True)
    gui_app.on_undo_slot(world.live, 1)
    _wait(gui_app, lambda: whole)
    assert plans == [] and whole[0] == world.live             # explicit, as the whole-folder undo


# --- 6. redaction ---------------------------------------------------------------------------------------


def test_the_restore_dialog_redacts_account_ids_in_its_choices(gui_app, world, monkeypatch,
                                                              tmp_path):
    amap = aliases.AliasMap()
    amap.set("st_76561198012345678", "Alice")
    monkeypatch.setattr(aliases, "_active", amap)
    copy = tmp_path / "found" / "st_76561198012345678 - Copy"
    steam_save(copy, 1, 0, "Main", 7200, IDENT, 1_790_000_000, extra="copy")
    entry = ops.import_backup(world.vault, copy, label=copy.name, copy_into_vault=False)
    d = _dialog(gui_app, entry)
    _wait(gui_app, lambda: d.choices)
    assert not any("76561198012345678" in v for v in d.combo.cget("values"))
    assert not any("76561198012345678" in k for k in d.choices)
    d.destroy()


# --- 7. the UI worker -----------------------------------------------------------------------------------


def test_a_job_the_owner_waits_on_goes_ahead_of_a_queued_scan(gui_app):
    order, gate = [], threading.Event()
    gui_app.worker.submit(lambda: gate.wait(10))                 # occupies the worker
    gui_app.worker.submit(lambda: order.append("scan"), low=True)
    gui_app.worker.submit(lambda: order.append("plan"))
    gate.set()
    _wait(gui_app, lambda: len(order) == 2)
    assert order == ["plan", "scan"]
