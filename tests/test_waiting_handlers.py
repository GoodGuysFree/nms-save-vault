"""Every handler that can keep the window waiting a second or more on a slow computer shows the
busy pointer while it works (GGF 2026-11-23; the inventory of 2026-11-23: App.refresh scans
every live folder on the window's thread and about fifteen handlers reach it)."""
from __future__ import annotations

import inspect

import pytest

from nms_save_vault import gui
from nms_save_vault.ui import waiting
from nms_save_vault.ui.organizer import correct, histhooks, history, notes, table, view


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
    # the audit of 2026-11-28 (GGF: "go over the entire UI of SV and add the same")
    history.HistoryWindow.show,               # another save file's graph
    history.HistoryWindow.set_mode,           # Key versions / All: every row drawn again
    history.HistoryWindow.toggle_run,         # a run opened or closed: the same
    table.OrganizerTable.set_chips,           # every line filtered again
    table.OrganizerTable._find_now,           # Find over every line
    table.OrganizerTable.sort_by,             # every line sorted again
    notes.NotesWindow._load_note,             # the notes document read from disk
    correct.CorrectDialog._fill_existing,     # the lineage document read
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
    gui.App._show_history_of,        # lists every backup of the vault for that save
    gui.App._offer_new_live_folders,  # looks for save folders on disk before asking
    notes.NotesWindow.confirm_close,  # Save chosen: the note written under the lock
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


# --- a message never waits under the busy pointer (the audit of 2026-11-28) ------------------


def test_rescan_says_a_settings_save_failure_after_the_wait(gui_app, monkeypatch, dialogs):
    dialogs.expect()
    held = []
    monkeypatch.setattr(gui.discover, "merge_live_sources", lambda state: 0)
    monkeypatch.setattr(gui.appstate, "save", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    monkeypatch.setattr(gui, "_showwarning", lambda *a, **k: held.append(waiting.is_held(gui_app)))
    monkeypatch.setattr(gui.App, "refresh", lambda self, *a, **k: None)
    gui_app.on_rescan()
    gui_app.update()
    assert held == [False]
    dialogs.take()


def test_a_theme_that_cannot_be_saved_is_said_after_the_wait(gui_app, monkeypatch):
    held = []
    monkeypatch.setattr(gui.appstate, "save", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    monkeypatch.setattr(gui, "_showwarning", lambda *a, **k: held.append(waiting.is_held(gui_app)))
    gui_app._apply_theme(persist=True)
    gui_app.update()
    assert held == [False]


def test_a_refresh_the_index_asks_for_never_holds_the_window():
    """Facts arriving refresh the table many times a minute: a busy pointer there would flicker
    and eat clicks; only what the owner does waits (set_chips, Find, sort_by)."""
    assert getattr(table.OrganizerTable.refresh, "__wrapped__", None) is None


# --- work away from the window the owner waits for (the audit of 2026-11-28) -----------------


class _Deep:
    def __init__(self, has=None):
        self.asked, self.has = [], has

    def deep(self, vid):
        return self.has

    def request_deep(self, vid):
        self.asked.append(vid)


class _Event:
    def __init__(self, vid, kind):
        self.vid, self.kind = vid, kind


def test_read_deeper_detail_shows_the_indicator_until_it_is_ready(gui_app, monkeypatch):
    view_ = gui_app.organizer_view
    monkeypatch.setattr(type(view_), "facts", property(lambda self: _Deep()), raising=False)
    view_.read_deep("v1")
    assert view_.waiting_for_deep()
    gui_app.indicator.update()
    assert str(gui_app.cget("cursor")) in (waiting.STARTING, waiting.WAIT)
    view_._index_events([_Event("v1", "deep-ready")])
    assert not view_.waiting_for_deep()


def test_deeper_detail_already_read_does_not_wait(gui_app, monkeypatch):
    view_ = gui_app.organizer_view
    monkeypatch.setattr(type(view_), "facts", property(lambda self: _Deep(has={"x": 1})),
                        raising=False)
    view_.read_deep("v1")
    assert not view_.waiting_for_deep()


def test_deeper_detail_that_never_comes_stops_waiting(gui_app, monkeypatch):
    view_ = gui_app.organizer_view
    monkeypatch.setattr(type(view_), "facts", property(lambda self: _Deep()), raising=False)
    view_.read_deep("v1")
    monkeypatch.setattr(view.time, "monotonic", lambda: 10 ** 9)
    assert not view_.waiting_for_deep()


def test_installing_an_update_shows_the_indicator_while_it_downloads():
    assert "_side_jobs" in inspect.getsource(gui.App._download_update)
