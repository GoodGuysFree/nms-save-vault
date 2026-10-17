"""Regression tests from the review of the Move dialog (U14-U18): the dialog never writes anything
other than what it shows. Apply is enabled only when the plan on screen was computed for exactly
the current picks (version, account, slot)."""
from __future__ import annotations

import dataclasses
import gc
import re
import threading
import tkinter as tk
from types import SimpleNamespace

import pytest

from nms_save_vault.core import liveops, safety
from nms_save_vault.ui.organizer import move as M
from nms_save_vault.ui.organizer import ops as O

from .conftest import steam_save
from .liveops_support import STEAM_ACCOUNT, T0, steam_world
from .test_ui_move import _open, _polestar, _wait, running, threads, view  # noqa: F401

VAULTY = 0x0A0A0A0A0A0A0A01
OLDIE = 0x0B0B0B0B0B0B0B02


@pytest.fixture
def world(tmp_path, monkeypatch):
    """steam_world plus two vault-only save files captured from the Desktop account."""
    from nms_save_vault.core import store as vstore

    monkeypatch.setattr(safety, "is_game_running", lambda: False)
    w = steam_world(tmp_path)
    staging = tmp_path / "staging" / STEAM_ACCOUNT
    steam_save(staging, 4, 0, "Vaulty", 1000, VAULTY, T0 + 40, extra="vaulty")
    steam_save(staging, 5, 0, "Oldie", 900, OLDIE, T0 + 30, extra="oldie")
    store = vstore.open_store(w.vault_root, live_roots=[w.live, w.other])
    store.add_from_live(staging, platform="steam", account_key=w.live.name, kind="full",
                        label="old backup", scope=vstore.CaptureScope())
    w.refresh()
    return w


def _line(view, name):
    return next(line for line in view.model.lines() if line.name == name)


def _replace_dialog(view, app):
    slot = next(s for s in view.model.live_board().sources[0].slots if s.name == "Second")
    d = view.open_replace_for(slot)
    app.update()
    return d


def _tick(d):
    for var in d._confirm_vars.values():
        var.set(True)
    d._update_apply()


def _enabled(d) -> bool:
    return d.apply_button.instate(["!disabled"])


# 2. right click -----------------------------------------------------------------------------


def test_right_click_opens_move_for_the_clicked_row(view, gui_app, monkeypatch):
    a, b = _line(view, "Polestar"), _line(view, "Vaulty")
    view.table.select(a.pid)
    gui_app.update()
    iid_b = view.table._iid_of[b.pid]
    monkeypatch.setattr(view.table.tree, "identify_row", lambda y: iid_b)
    menus = []
    monkeypatch.setattr(tk.Menu, "tk_popup", lambda self, x, y, entry="": menus.append(self))
    view.table._context_menu(SimpleNamespace(x=5, y=5, x_root=0, y_root=0))
    assert view.table.selection_ref() == (b.pid, None)
    menus[0].invoke(0)
    d = view.move_dialog
    _wait(gui_app, lambda: d.picker is not None and not d.busy)
    assert d.name == "Vaulty"
    d.cancel()


# 3. a pick made while a plan runs --------------------------------------------------------------


def test_a_pick_made_while_planning_is_planned_next_and_apply_waits(view, gui_app):
    d = _replace_dialog(view, gui_app)
    picker = d.picker_widget
    x, y = _line(view, "Vaulty"), _line(view, "Oldie")
    picker._choose(x.pid, x.representative)
    assert d.busy
    picker._choose(y.pid, y.representative)                   # while the plan for X runs
    assert not _enabled(d)
    _wait(gui_app, lambda: d.plan is not None and not d.busy)
    assert picker.chosen == (y.pid, y.representative)
    assert d.plan.source.name == "Oldie" and d.in_name.cget("text") == "Oldie"
    assert d.plan_matches(d.plan)
    d.cancel()


# 4. account radios while a plan runs ---------------------------------------------------------------


def test_an_account_picked_while_planning_is_the_account_planned(view, gui_app):
    d = _open(view, gui_app)
    other = next(c for c in d.picker.candidates if c.target.slot != d.slot and not c.blocked)
    d._choose(other.target.slot)                               # planning: busy
    radios = list(d.account_bar.winfo_children())
    chosen_ref = d.accounts[1]
    radios[1].invoke()                                         # the other account, while busy
    assert not _enabled(d)
    _wait(gui_app, lambda: d.plan is not None and not d.busy)
    assert d.account == chosen_ref and d.plan.targets[0].target.ref == chosen_ref
    assert d.account_var.get() == 0 and d.plan_matches(d.plan)   # rebuilt: shown account first
    d.cancel()


def test_a_slot_row_picked_while_planning_is_the_slot_planned(view, gui_app):
    d = _open(view, gui_app)
    free = [c for c in d.picker.candidates if not c.blocked and c.target.slot != d.slot]
    d._choose(free[0].target.slot)
    d.tree.selection_set(f"s{free[1].target.slot}")
    gui_app.update()
    assert not _enabled(d)
    _wait(gui_app, lambda: d.plan is not None and not d.busy)
    assert d.slot == free[1].target.slot and d.plan.targets[0].target.slot == d.slot
    d.cancel()


# 1. a stale result whose reload fails ---------------------------------------------------------------


def test_a_failed_reload_after_stale_never_reuses_the_old_replan(view, gui_app, monkeypatch):
    d = _replace_dialog(view, gui_app)
    picker = d.picker_widget
    x, y = _line(view, "Vaulty"), _line(view, "Oldie")
    picker._choose(x.pid, x.representative)
    _wait(gui_app, lambda: d.plan is not None and not d.busy)
    p1 = d.plan
    _tick(d)
    real_execute, real_picker = M.execute, M.eviction_picker
    replan = dataclasses.replace(p1, plan_id="fresh")
    monkeypatch.setattr(M, "execute", lambda *a, **k: liveops.OpOutcome(
        None, "stale", "stale", (), None, "n/a", None, replan=replan))
    fail = {"n": 1}

    def flaky(*a, **k):
        if fail["n"]:
            fail["n"] -= 1
            raise OSError("the live folder could not be read right now")
        return real_picker(*a, **k)
    monkeypatch.setattr(M, "eviction_picker", flaky)
    d.apply()
    _wait(gui_app, lambda: not d.busy and not fail["n"])
    assert d.plan is None and not _enabled(d)                  # nothing verified: no Apply
    assert "could not be planned" in d.error.cget("text")      # and the reason stays (6)
    assert d._keep_gen is None
    executed = []

    def record(ctx, plan, *a, **k):
        executed.append(plan)
        return real_execute(ctx, plan, *a, **k)
    monkeypatch.setattr(M, "execute", record)
    picker._choose(y.pid, y.representative)                    # the owner now picks Oldie
    _wait(gui_app, lambda: not d.busy and d.plan is not None)
    assert d.plan.source.name == "Oldie" and d.name == "Oldie"
    _tick(d)
    assert _enabled(d)
    d.on_done = lambda o: None
    d.apply()
    _wait(gui_app, lambda: executed and not d.executing)
    assert executed[0].source.name == "Oldie"


def test_a_stale_result_shows_the_replan_and_keeps_apply_for_the_same_picks(view, gui_app,
                                                                         monkeypatch):
    d = _open(view, gui_app)
    _tick(d)
    fresh = dataclasses.replace(d.plan, plan_id="fresh")
    monkeypatch.setattr(M, "execute", lambda *a, **k: liveops.OpOutcome(
        None, "stale", "stale", (), None, "n/a", None, replan=fresh))
    d.apply()
    _wait(gui_app, lambda: d.plan is fresh and not d.busy)
    assert d.plan_matches(fresh) and "changed" in d.error.cget("text")
    d.cancel()


# 6. an error stays visible --------------------------------------------------------------------------


def test_a_planning_error_stays_visible(view, gui_app, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk went away")
    monkeypatch.setattr(M, "plan_place", boom)
    d = _open(view, gui_app)
    _wait(gui_app, lambda: not d.busy and d.error.cget("text"))
    assert "could not be planned" in d.error.cget("text") and "disk went away" in d.error.cget("text")
    assert d.plan is None and not _enabled(d)
    d.cancel()


# 7. a drop onto a blocked slot ------------------------------------------------------------------------


def test_a_drop_on_a_blocked_slot_says_why_and_picks_no_other_slot(view, gui_app, monkeypatch):
    real = M.eviction_picker

    def blocking(ctx, source, account=None):
        picker = real(ctx, source, account)
        cands = tuple(dataclasses.replace(c, blocked="PINNED") if c.target.slot == 4 else c
                      for c in picker.candidates)
        return dataclasses.replace(picker, candidates=cands)
    monkeypatch.setattr(M, "eviction_picker", blocking)
    d = view.open_move_for(_polestar(view), slot=4)
    _wait(gui_app, lambda: d.picker is not None and not d.busy)
    assert d.slot is None and d.plan is None and not _enabled(d)
    assert "cannot take" in d.error.cget("text") and "PINNED" in d.error.cget("text")
    d.cancel()


# 5. closing mid-write -----------------------------------------------------------------------------------


def _slow_keep(view, gui_app, monkeypatch):
    gate = threading.Event()
    target = next(s for s in view.model.live_board().sources[0].slots
                  if s.state in ("occupied", "partial")).target
    view.run_plan("Keep a copy in the vault", liveops.plan_keep, target)
    _wait(gui_app, lambda: view.plan_dialog is not None and view.plan_dialog.winfo_exists())
    d = view.plan_dialog
    real = O.execute

    def slow(*a, **k):
        gate.wait(10)
        return real(*a, **k)
    monkeypatch.setattr(O, "execute", slow)
    for var in d.vars.values():
        var.set(True)
    d._update()
    d.apply()
    assert d.busy
    return d, gate


def test_escape_and_the_title_bar_x_do_not_close_a_dialog_mid_write(view, gui_app, monkeypatch):
    d, gate = _slow_keep(view, gui_app, monkeypatch)
    done = []
    d.on_done = done.append
    cmd = re.search(r"\[(\S+)", d.bind("<Escape>")).group(1)
    args = ["0"] * 19
    args[14] = d._w
    d.tk.call(cmd, *args)                                      # what pressing Esc runs
    d.tk.call(d.protocol("WM_DELETE_WINDOW"))                  # the title-bar X
    gui_app.update()
    assert d.winfo_exists()
    gate.set()
    _wait(gui_app, lambda: done and not d.winfo_exists())
    assert done[0].status == "done"


def test_a_dialog_closed_anyway_still_reports_its_outcome(view, gui_app, monkeypatch):
    d, gate = _slow_keep(view, gui_app, monkeypatch)
    done = []
    d.on_done = done.append
    d.destroy()                                                # forced: not through cancel()
    gate.set()
    _wait(gui_app, lambda: done)
    assert done[0].status == "done"


def test_the_recovery_dialog_refuses_close_mid_write_and_reports_after(view, gui_app, monkeypatch):
    gate = threading.Event()
    item = liveops.RecoveryItem("op-20260101T000000Z-bbbb", "place", ("Steam slot 1",), "torn",
                                "back", ("forward", "back", "leave"), "Stopped.", "2026-01-01")

    def slow(ctx, op_id, choice, confirmed=frozenset()):
        gate.wait(10)
        return liveops.OpOutcome(op_id, "done", "Done.", (), None, "n/a", None)
    monkeypatch.setattr(O, "resolve_recovery", slow)
    changed = []
    d = view.open_recovery([item])
    d.on_changed = lambda: changed.append(1)
    d.apply(item)
    assert d.busy
    d.cancel()
    assert d.winfo_exists()
    gate.set()
    _wait(gui_app, lambda: changed)


# 8. Tk variables are never released on the worker thread ------------------------------------------------


def test_no_tk_variable_is_collected_on_the_worker_thread(view, gui_app, monkeypatch):
    seen = []
    real = tk.Variable.__del__

    def spy(self):
        seen.append(threading.current_thread() is threading.main_thread())
        try:
            real(self)
        except RuntimeError:
            pass
    monkeypatch.setattr(tk.Variable, "__del__", spy)
    d = _open(view, gui_app)
    _tick(d)
    d.on_done = lambda o: None
    d.apply()
    _wait(gui_app, lambda: not d.winfo_exists())
    d2 = view.open_recovery([liveops.RecoveryItem(
        "op-20260101T000000Z-cccc", "place", ("Steam slot 1",), "torn", "back", ("back",),
        "Stopped.", "2026-01-01")])
    monkeypatch.setattr(O, "resolve_recovery", lambda *a, **k: liveops.OpOutcome(
        "x", "done", "Done.", (), None, "n/a", None))
    d2.apply(d2.items[0])
    _wait(gui_app, lambda: not d2.busy)
    d2.destroy()
    view.move_dialog = view.recovery_dialog = view.plan_dialog = None
    del d, d2
    _wait(gui_app, lambda: not gui_app.worker.busy)
    gc.collect()
    gui_app.update()
    assert seen and all(seen)
