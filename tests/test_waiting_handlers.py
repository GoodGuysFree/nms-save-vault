"""Every handler that can keep the window waiting a second or more on a slow computer shows the
busy pointer while it works (GGF 2026-11-23; the inventory of 2026-11-23: App.refresh scans
every live folder on the window's thread and about fifteen handlers reach it)."""
from __future__ import annotations

import inspect

import pytest

from nms_save_vault import gui
from nms_save_vault.ui import waiting
from nms_save_vault.ui.organizer import histhooks, history, table, view


@pytest.mark.parametrize("method", [
    gui.App.refresh,                 # the scan of every live folder (Xbox: every blob)
    gui.App._live_paths,             # discovery on disk
    gui.App._apply_theme,
    gui.ChooseSourceDialog._populate,
    gui.CopySlotDialog._reload_slots,
    table.OrganizerTable._fill_versions,      # a save file's versions and their graph
    table.OrganizerTable.set_model,
    view.OrganizerView._scanned,              # a scan lands: every table and window
    history.HistoryWindow.set_model,          # the graph of every version
])
def test_a_slow_handler_waits_under_the_busy_pointer(method):
    assert getattr(method, "__wrapped__", None) is not None, method.__qualname__


@pytest.mark.parametrize("method", [
    gui.App.destroy,                 # waits for the worker and the index to stop
    gui.App.on_undo,                 # plans the undo (opens the organizer) before asking
    gui.App.switch_vault,            # discovers and opens the vault chosen
    gui.App.on_free_old_folders,     # opens the store before the first wait window
    gui.App._import_vault_dir,       # reads the other vault's catalog first
    gui.App.on_copy_slot,            # scans the target folder
    gui.App.on_rescan,               # finds the folders, then says how many
    gui.App.on_discover,             # imports what it finds, then says what
    histhooks.HistoryHooks.open_history,      # the graph of every version, then the window
])
def test_a_handler_that_asks_waits_only_around_its_slow_part(method):
    assert "wait_cursor" in inspect.getsource(method), method.__qualname__


def test_the_playthrough_report_is_read_under_the_busy_pointer(gui_app, monkeypatch):
    seen = []
    monkeypatch.setattr(gui.playtime, "collect",
                        lambda *a, **k: seen.append(waiting.is_held(gui_app)) or "report")
    gui_app._report = None
    try:
        assert gui_app._playthrough_report() == "report"
    finally:
        gui_app._report = None
    assert seen == [True] and not waiting.is_held(gui_app)


def test_the_live_paths_are_found_under_the_busy_pointer(gui_app, monkeypatch):
    seen = []
    monkeypatch.setattr(gui.discover, "all_live_paths",
                        lambda state: seen.append(waiting.is_held(gui_app)) or [])
    assert gui_app._live_paths() == []
    assert seen == [True] and not waiting.is_held(gui_app)


def test_a_check_for_updates_asked_for_shows_it_until_the_answer(gui_app, monkeypatch):
    """Check for updates... runs on a thread of its own, up to six seconds: the arrow with the
    hourglass until the answer comes."""
    import threading
    import time

    gate = threading.Event()
    monkeypatch.setattr(gui.updates, "check", lambda *a, **k: gate.wait(5) and None)
    answered = []
    monkeypatch.setattr(gui.App, "_update_check_done",
                        lambda self, outcome, quiet: answered.append(quiet))
    gui_app._check_for_updates(quiet=False)
    try:
        gui_app.indicator.update()
        assert str(gui_app.cget("cursor")) in (waiting.STARTING, waiting.WAIT)
    finally:
        gate.set()
    end = time.monotonic() + 10
    while not answered and time.monotonic() < end:
        gui_app.update()
        time.sleep(0.01)
    gui_app.indicator.update()
    assert answered == [False] and str(gui_app.cget("cursor")) == ""
