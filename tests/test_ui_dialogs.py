"""Keyboard and sizing basics of the organizer's windows, the keyboard route to the context
menus, the tooltip's end of life, the buttons that say why they cannot act, and a vault whose
store cannot be opened. A Tk menu is never posted (it would block): the posting seam is
replaced and the handlers are checked up to it."""
from __future__ import annotations

import json
import tkinter as tk
from tkinter import ttk

import pytest

from nms_save_vault.core import liveops
from nms_save_vault.core.organizer import Organizer
from nms_save_vault.core.organizer.cache import ScanCache
from nms_save_vault.ui import dialogs
from nms_save_vault.ui import tooltip as tip
from nms_save_vault.ui.organizer import compare as C
from nms_save_vault.ui.organizer import live as L
from nms_save_vault.ui.organizer import table as T
from nms_save_vault.ui.organizer import view as V
from nms_save_vault.ui.organizer import vaultwin as W

from .test_organizer_readmodel import PID
from .test_ui_history import forked, host  # noqa: F401  (fixtures)
from .test_ui_move import (  # noqa: F401  (fixtures)
    _open, _polestar, _wait, running, threads, view, world)


def _fire(handler, widget=None):
    return handler(type("E", (), {"widget": widget})())


# --- the shared helper -----------------------------------------------------------------------


def test_escape_cancels_unless_a_write_runs(host):  # noqa: F811
    calls, busy = [], {"on": True}
    dialogs.bind_escape(host, lambda: calls.append(1), lambda: busy["on"])
    assert host.bind("<Escape>")
    assert _fire(host._escape_handler) == "break" and calls == []
    busy["on"] = False
    _fire(host._escape_handler)
    assert calls == [1]


def test_return_presses_the_default_but_leaves_a_text_box_and_a_button_alone(host):  # noqa: F811
    calls = []
    dialogs.bind_return(host, lambda: calls.append("default"))
    assert host.bind("<Return>")
    text = tk.Text(host)
    assert _fire(host._return_handler, text) is None and calls == []
    pressed = []
    button = ttk.Button(host, command=lambda: pressed.append(1))
    assert _fire(host._return_handler, button) == "break" and pressed == [1] and calls == []
    _fire(host._return_handler, ttk.Entry(host))
    assert calls == ["default"]


def test_a_minimum_size_is_capped_to_the_screen(host, monkeypatch):  # noqa: F811
    monkeypatch.setattr(tk.Misc, "winfo_screenheight", lambda self: 700)
    monkeypatch.setattr(tk.Misc, "winfo_screenwidth", lambda self: 1000)
    dialogs.cap_minsize(host, 860, 640)
    assert host.minsize() == (860 if 860 <= 900 else 900, 630)
    dialogs.cap_minsize(host, 2000, 2000)
    assert host.minsize() == (900, 630)


def test_a_footer_built_first_is_visited_last_by_tab(host):  # noqa: F811
    foot = ttk.Frame(host)
    body = ttk.Frame(host)
    assert host.winfo_children() == [foot, body]
    dialogs.tab_last(foot)
    assert host.winfo_children() == [body, foot]


def test_focus_first_gives_the_list_the_focus(host, gui_app):  # noqa: F811
    host.deiconify()
    entry = ttk.Entry(host)
    entry.pack()
    dialogs.focus_first(host, entry)
    gui_app.update()
    assert str(host.tk.call("focus", "-lastfor", host)) == str(entry)


# --- the tooltip ---------------------------------------------------------------------------


def test_the_tooltip_cancels_its_timer_when_the_widget_goes(host, gui_app):  # noqa: F811
    frame = tk.Frame(host)
    frame.pack()
    t = tip.Tooltip(frame, lambda _w, _k: "text", key_at=lambda _e: "row")
    t._on_motion(type("E", (), {"x_root": 1, "y_root": 1})())
    assert t._after is not None
    frame.destroy()
    assert t._after is None
    t._show("row", 1, 1)                    # the timer that already fired: no TclError
    t.hide()


def test_a_click_hides_the_tooltip_even_where_the_widget_binds_button_1(host, gui_app):  # noqa: F811
    host.deiconify()
    frame = tk.Frame(host, width=50, height=50)
    frame.pack()
    frame.bind("<Button-1>", lambda e: None)          # the widget's own, more specific binding
    t = tip.Tooltip(frame, lambda _w, _k: "text", key_at=lambda _e: "row")
    gui_app.update()
    t.row = "row"
    frame.event_generate("<ButtonPress-1>", x=5, y=5)
    gui_app.update()
    assert t.row is None


# --- the live list and the table by keyboard ----------------------------------------------------


def test_shift_f10_and_the_menu_key_open_the_slot_menu_at_the_focused_slot(view, gui_app):  # noqa: F811
    live = view.live
    assert live.canvas.bind("<Shift-F10>")
    seen = []
    live.on_context = lambda token, slot, event: seen.append((token, slot.label,
                                                              event.x_root, event.y_root))
    token = next(iter(live.drawn))
    live.select_slot(token)
    live._on_menu_key()
    x0, y0, x1, y1 = live.drawn[token].box
    got = seen[-1]
    assert got[0] == token and got[2] == live.canvas.winfo_rootx() + int(x0 + T.px(40))
    assert got[3] == live.canvas.winfo_rooty() + int((y0 + y1) / 2)


def test_the_view_posts_the_slot_menu_through_one_seam(view, gui_app, monkeypatch):  # noqa: F811
    posted = []
    monkeypatch.setattr(V.OrganizerView, "_post", staticmethod(
        lambda menu, x, y: posted.append((menu.index("end") is not None, x, y))))
    token = next(iter(view.live.drawn))
    view.live.select_slot(token)
    view.live._on_menu_key()
    assert posted and posted[0][0]


def test_the_table_opens_its_menu_for_the_focused_row_from_the_keyboard(view, gui_app,  # noqa: F811
                                                                         monkeypatch):
    table = view.table
    assert table.tree.bind("<Shift-F10>") and table.tree.bind("<App>")
    posted, moves = [], []
    monkeypatch.setattr(T.OrganizerTable, "_post", staticmethod(
        lambda menu, x, y: posted.append((menu.entrycget(0, "label"), x, y))))
    table.on_move = lambda pid, head: moves.append(pid)
    iid = table.tree.get_children("")[0]
    table.tree.focus(iid)
    table.tree.selection_set(iid)
    monkeypatch.setattr(T.OrganizerTable, "context_position",
                        lambda self: (iid, 11, 22))
    table._context_key()
    assert posted == [("Move to live...", 11, 22)]


def test_context_position_is_inside_the_focused_row(view, gui_app):  # noqa: F811
    table = view.table
    view.winfo_toplevel().deiconify()
    gui_app.update()
    iid = table.tree.get_children("")[0]
    table.tree.focus(iid)
    where = table.context_position()
    assert where is not None and where[0] == iid
    assert where[1] >= table.tree.winfo_rootx() and where[2] >= table.tree.winfo_rooty()
    view.winfo_toplevel().withdraw()


# --- buttons that say why ---------------------------------------------------------------------------


def test_move_notes_and_history_are_disabled_with_a_reason_until_a_file_is_selected(  # noqa: F811
        view, gui_app):
    for button in (view.move_button, view.notes_button, view.history_button):
        view.table.tree.selection_set(())
        view.table.tree.focus("")
        view._update_actions()
        assert button.instate(["disabled"])
        assert "Select a save file" in view.action_why[button]
    view.table.select(_polestar(view))
    gui_app.update()
    for button in (view.move_button, view.notes_button, view.history_button):
        assert button.instate(["!disabled"]) and view.action_why[button] == ""


def test_move_waits_for_the_scan_and_says_so(view, gui_app):  # noqa: F811
    view.table.select(_polestar(view))
    gui_app.update()
    view.scanning = True
    view._update_actions()
    assert view.move_button.instate(["disabled"]) and "scan" in view.action_why[view.move_button]
    assert view.notes_button.instate(["!disabled"])
    assert view.open_move_for(_polestar(view)) is None and "busy" in view.alerts
    view.scanning = False
    view._update_actions()
    assert view.move_button.instate(["!disabled"])


def test_ctrl_f_focuses_find(view, gui_app):  # noqa: F811
    assert view.winfo_toplevel().bind("<Control-f>")
    top = view.winfo_toplevel()
    top.deiconify()
    gui_app.update()
    view.focus_find()
    gui_app.update()
    assert str(top.tk.call("focus", "-lastfor", top)) == str(view.table.find)
    assert view._ctrl_f() == "break"          # on screen: the shortcut is ours
    top.withdraw()


# --- Move: the size, Escape, the order of Tab ------------------------------------------------------


def test_the_move_dialog_never_asks_for_more_than_the_screen_has(view, gui_app, monkeypatch):  # noqa: F811
    monkeypatch.setattr(tk.Misc, "winfo_screenheight", lambda self: 600)
    d = _open(view, gui_app)
    assert d.minsize()[1] <= int(600 * dialogs.SCREEN_SHARE)
    assert d.winfo_children()[-1] is d.foot           # the footer is last in the Tab order
    assert d.bind("<Escape>")


def test_escape_does_not_close_the_move_dialog_during_a_write(view, gui_app):  # noqa: F811
    d = _open(view, gui_app)
    d.executing = True
    _fire(d._escape_handler)
    assert d.winfo_exists()
    d.executing = False
    _fire(d._escape_handler)
    gui_app.update()
    assert not d.winfo_exists()


# --- the other windows -----------------------------------------------------------------------------


def test_history_compare_and_pick_close_on_escape_and_start_on_the_list(host, gui_app, forked):  # noqa: F811
    from nms_save_vault.ui.organizer import history as H

    w, (a, b, c, *_), _ = forked if len(forked) == 3 else (None, None, None)
    model = w.refresh()
    hist = H.HistoryWindow(host, model=model, themed=gui_app.themed, pid=PID)
    cmp_ = C.CompareWindow(host, model=model, themed=gui_app.themed, pid=PID, a=a, b=b)
    try:
        assert str(hist.transient()) == str(host) and str(cmp_.transient()) == str(host)
        assert hist.bind("<Escape>") and cmp_.bind("<Escape>")
        assert hist.winfo_children().index(hist.body) >= 0
        pick = C.PickDialog(cmp_)
        assert pick.bind("<Return>") and pick.bind("<Escape>")
        _fire(pick._escape_handler)
        assert not pick.winfo_exists()
        _fire(hist._escape_handler)
        _fire(cmp_._escape_handler)
        assert not hist.winfo_exists() and not cmp_.winfo_exists()
    finally:
        for win in (hist, cmp_):
            if win.winfo_exists():
                win.destroy()


def test_history_tab_order_visits_the_list_before_the_branch_tips(host, gui_app, forked):  # noqa: F811
    from nms_save_vault.ui.organizer import history as H

    w, _, _ = forked
    hist = H.HistoryWindow(host, model=w.refresh(), themed=gui_app.themed, pid=PID)
    try:
        left = hist.tips_canvas.master.master            # the Labelframe's parent
        kids = left.winfo_children()
        tips = hist.tips_canvas.master
        assert kids.index(tips) == len(kids) - 1
    finally:
        hist.destroy()


def test_the_column_chooser_closes_on_escape_applies_on_return_and_keeps_focus_on_the_row(
        view, gui_app):  # noqa: F811
    view.winfo_toplevel().deiconify()
    gui_app.update()
    chooser = T.ColumnChooser(view.table)
    try:
        assert chooser.bind("<Escape>") and chooser.bind("<Return>")
        key = chooser.order[1]
        chooser.move(key, 1)
        assert chooser.order[2] == key
        gui_app.update()
        assert str(chooser.tk.call("focus", "-lastfor", chooser)) == str(chooser.moves[(key, 1)])
        chooser.on[key].set(False)
        _fire(chooser._return_handler, None)
        assert not chooser.winfo_exists() and key not in view.table.displayed_columns()
    finally:
        if chooser.winfo_exists():
            chooser.destroy()
        view.winfo_toplevel().withdraw()


# --- a store the app cannot open --------------------------------------------------------------------


def _organizer(world):
    return Organizer(world.vault_root, world.sources, read_only=True, cache=ScanCache(None, None),
                     game_running=lambda: False)


def test_a_store_made_by_a_newer_app_is_an_alert_not_an_empty_vault(world):  # noqa: F811
    path = world.vault_root / "store" / "FORMAT"
    body = json.loads(path.read_text("ascii"))
    body["read_compat"] = 99
    path.write_text(json.dumps(body), "ascii")
    organizer = _organizer(world)
    assert organizer.version_store is None
    assert "newer NMS Save Vault" in organizer.store_error and "update the app" in organizer.store_error
    organizer.refresh(record=False)
    alerts = V.scan_alerts(organizer)
    assert "newer NMS Save Vault" in alerts["store:open"]
    assert "cannot be opened" in V.summary_text(organizer.model, organizer)


def test_a_damaged_format_says_to_run_fsck(world):  # noqa: F811
    (world.vault_root / "store" / "FORMAT").write_bytes(b"{ not json")
    organizer = _organizer(world)
    assert "nmsvault store fsck" in organizer.store_error and "damaged" in organizer.store_error
    assert V.scan_alerts(organizer)["store:open"] == organizer.store_error


def test_a_healthy_store_has_no_error(world):  # noqa: F811
    assert _organizer(world).store_error is None


def test_the_planner_calls_it_unavailable_not_in_need_of_an_upgrade(world, monkeypatch):  # noqa: F811
    (world.vault_root / "store" / "FORMAT").write_bytes(b"{ not json")
    organizer = Organizer(world.vault_root, world.sources, cache=ScanCache(None, None),
                          game_running=lambda: False)
    ctx = liveops.context_for(organizer, host=world.host, machine_id="m-00000000000a",
                              app_version="t", game_probe=lambda: False)
    assert ctx.versions is None
    assert liveops.planner.vault_problem(ctx).code == "VAULT_UNAVAILABLE"


# --- the vault window ---------------------------------------------------------------------------------


def test_the_vault_window_names_a_store_it_cannot_open(gui_app, world):  # noqa: F811
    (world.vault_root / "store" / "FORMAT").write_bytes(b"{ not json")
    env = W.VaultEnv(world.vault_root, [world.live], None, "test")
    state = W.read_state(env)
    assert state.unopenable and "nmsvault store fsck" in state.unopenable


def test_a_long_result_is_shown_whole_and_wraps(gui_app, world):  # noqa: F811
    top = tk.Toplevel(gui_app)
    top.withdraw()
    env = W.VaultEnv(world.vault_root, [world.live], None, "test")
    win = W.VaultWindow(top, worker=gui_app.worker, themed=gui_app.themed, env=env)
    try:
        _wait(gui_app, lambda: not win.busy and win.state is not None)
        text = "x" * 150 + " " + "y" * 150 + " end"
        win._copied(type("R", (), {"message": text})())
        assert win.result.cget("text").endswith("end") and len(win.result.cget("text")) > 200
    finally:
        _wait(gui_app, lambda: not win.busy)
        win.destroy()
        top.destroy()


def test_the_rollback_view_offers_to_choose_the_copy_when_it_cannot_be_found(gui_app, world,  # noqa: F811
                                                                              tmp_path):
    top = tk.Toplevel(gui_app)
    top.withdraw()
    env = W.VaultEnv(world.vault_root, [world.live], None, "test")
    win = W.VaultWindow(top, worker=gui_app.worker, themed=gui_app.themed, env=env)
    try:
        _wait(gui_app, lambda: not win.busy and win.state is not None)
        plan = type("P", (), {"export": (), "restore": (),
                              "problems": (("copy-missing", "not found"),)})()
        win._rollback_plan = plan
        win._render()
        labels = [w.cget("text") for w in win.body.winfo_children() if isinstance(w, ttk.Button)]
        assert "Choose the folder the copy is in..." in labels
        asked = []
        win.plan_undo = lambda: asked.append(win.copy_folder)
        win.set_copy(tmp_path)
        assert asked == [tmp_path]
    finally:
        win.destroy()
        top.destroy()
