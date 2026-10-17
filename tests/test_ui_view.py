"""The Organizer tab assembled (D6 U12): the start-up scan in the worker, the selection hub
between the table, the live area, the detail pane and the notes window, pins, and the
alert bars."""
from __future__ import annotations

import time
import tkinter as tk

import pytest

from nms_save_vault.core import safety
from nms_save_vault.ui.organizer import view as V
from nms_save_vault.ui.organizer.notes import NotesWindow

from .organizer_world import PIDS, build_world


@pytest.fixture
def world(tmp_path, make_wgs_account, monkeypatch):
    monkeypatch.setattr(safety, "is_game_running", lambda: False)
    return build_world(tmp_path, make_wgs_account)


def _wait(app, condition, timeout: float = 15.0) -> None:
    end = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < end, "timed out"
        app.update()
        time.sleep(0.005)


@pytest.fixture
def view(gui_app, world):
    top = tk.Toplevel(gui_app)
    top.withdraw()
    prefs = {}
    widget = V.OrganizerView(top, worker=gui_app.worker, themed=gui_app.themed, prefs=prefs,
                             save_prefs=lambda: None, make_organizer=lambda: world.organizer)
    widget.pack(fill=tk.BOTH, expand=True)
    widget.rescan()
    _wait(gui_app, lambda: not widget.scanning)
    yield widget
    if widget.notes_window is not None and widget.notes_window.winfo_exists():
        widget.notes_window.destroy()
    top.destroy()


def test_the_scan_runs_in_the_worker_and_fills_everything(view):
    assert view.model is not None and len(view.table.tree.get_children("")) == 5
    assert len(view.live.drawn) == 30
    summary = view.summary.cget("text")
    versions = sum(len(view.model.versions(l.pid)) for l in view.model.lines())
    assert summary.startswith(f"5 save files, {versions} versions") and "Steam 3/15 live" in summary
    assert not view.progress.winfo_manager() and view.rescan_button.instate(["!disabled"])
    assert view.alerts == {}


def test_a_scan_asked_for_while_one_runs_follows_it(view, gui_app):
    view.rescan()
    assert view.scanning
    view.rescan()
    view.rescan(record=False)
    assert view._pending_scan is True
    _wait(gui_app, lambda: not view.scanning and view._pending_scan is None)
    _wait(gui_app, lambda: not view.scanning)


def test_the_table_selection_drives_the_detail_and_the_live_outline(view, gui_app):
    view.table.select(PIDS["cross"])
    gui_app.update()
    assert view.detail.title.cget("text") == "Cross Save"
    assert view.live.selected_pid == PIDS["cross"]


def test_a_live_slot_selects_its_save_file_and_an_empty_one_shows_its_card(view, gui_app):
    token = next(t for t, d in view.live.drawn.items() if d.slot.label == "S3")
    view.live.select_slot(token)
    gui_app.update()
    assert view.table.selection_ref() == (PIDS["other"], None)
    assert view.detail.title.cget("text") == "Other Run"
    empty = next(t for t, d in view.live.drawn.items() if d.slot.label == "S1")
    view.live.select_slot(empty)
    assert view.detail.title.cget("text").startswith("Steam slot 1")
    assert "Steam Cloud may fill it again" in view.detail.text_of("summary")
    assert view.table.tree.selection() == ()


def test_a_link_in_the_detail_selects_that_version(view, gui_app):
    oldest = view.model.versions(PIDS["main"])[-1].head
    view._link(PIDS["main"], oldest)
    gui_app.update()
    assert view.table.selection_ref() == (PIDS["main"], oldest)
    assert view.detail.title.cget("text") == "Main Save #1"


def test_the_notes_window_follows_the_selection(view, gui_app):
    view.table.select(PIDS["main"])
    gui_app.update()
    view.open_notes()
    window = view.notes_window
    assert isinstance(window, NotesWindow) and window.pid == PIDS["main"]
    window.withdraw()
    view.table.select(PIDS["cross"])
    gui_app.update()
    assert window.pid == PIDS["cross"]
    view.open_notes_for(PIDS["old"], None)
    assert view.notes_window is window and window.pid == PIDS["old"]


def test_unpinning_saves_and_reloads(view, gui_app):
    assert view.model.line(PIDS["main"]).pinned
    view._pin(PIDS["main"], False)
    _wait(gui_app, lambda: not view.model.line(PIDS["main"]).pinned)


def test_a_scan_that_fails_becomes_an_alert_bar(gui_app, world):
    top = tk.Toplevel(gui_app)
    top.withdraw()

    def broken():
        raise OSError("the vault drive is not there")

    widget = V.OrganizerView(top, worker=gui_app.worker, themed=gui_app.themed, prefs={},
                             save_prefs=lambda: None, make_organizer=broken)
    widget.pack()
    try:
        widget.rescan()
        _wait(gui_app, lambda: not widget.scanning)
        assert "scan-failed" in widget.alerts
        text = widget.alerts["scan-failed"].text.cget("text")
        assert "the vault drive is not there" in text
        widget.clear_alert("scan-failed")
        assert widget.alerts == {}
    finally:
        top.destroy()


def test_an_unreadable_live_folder_and_store_problems_are_alerts(world):
    import shutil

    shutil.rmtree(world.xbox)
    world.organizer.refresh(record=False)
    alerts = V.scan_alerts(world.organizer)
    assert any(k.startswith("live:xbox") for k in alerts)
    world.organizer.store.problems.append("notes.json does not parse; it is left untouched")
    alerts = V.scan_alerts(world.organizer)
    assert alerts["store:0"].startswith("Organizer: notes.json does not parse")


def test_the_summary_says_when_the_game_runs(world):
    import dataclasses

    world.organizer.scan = dataclasses.replace(world.organizer.scan, game_running=True)
    assert "Game: RUNNING" in V.summary_text(world.model, world.organizer)


def test_the_app_has_the_organizer_tab_and_looks_again_on_coming_back(gui_app, monkeypatch):
    from nms_save_vault import gui

    assert isinstance(gui_app.organizer_view, V.OrganizerView)
    looks = []
    monkeypatch.setattr(gui_app.organizer_view, "shown", lambda: looks.append(1))
    monkeypatch.setattr(gui.appstate, "save", lambda *a, **k: None)
    before = gui_app.current_view()
    try:
        gui_app.show_view(gui.CLASSIC)
        gui_app.update()
        gui_app.show_view(gui.ORGANIZER)
        gui_app.update()
        assert looks == [1]
    finally:
        gui_app.show_view(before)
        gui_app.update()
