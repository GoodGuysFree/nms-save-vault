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


def test_two_versions_selected_in_the_table_open_compare_older_first(view, gui_app):
    pid = PIDS["main"]
    rows = view.model.versions(pid)
    newest, oldest = rows[0].head, rows[-1].head
    view.table.select(pid)
    gui_app.update()
    assert view.compare_button.instate(["disabled"]) and "two" in view.action_why[view.compare_button]
    view.table.select(pid, newest)
    gui_app.update()
    assert view.compare_button.instate(["disabled"])        # one version is not a pair
    iids = {ref: iid for iid, ref in view.table._version_of.items()}
    view.table.tree.selection_set((iids[(pid, newest)], iids[(pid, oldest)]))
    gui_app.update()
    assert view.compare_button.instate(["!disabled"]) and view.action_why[view.compare_button] == ""
    view.compare_button.invoke()
    cmp = view.histories.compare
    try:
        assert cmp is not None and (cmp.pid, cmp.a, cmp.b) == (pid, oldest, newest)
    finally:
        cmp.destroy()
    other = next(l.pid for l in view.model.lines() if l.pid != pid)
    view.table.select(other, view.model.versions(other)[0].head)
    gui_app.update()
    view.table.tree.selection_add(iids[(pid, oldest)])      # two save files: no pair
    gui_app.update()
    assert view.compare_pair() is None and view.compare_button.instate(["disabled"])


def test_search_finds_names_and_notes_without_the_index(view, gui_app):
    window = view.open_search()
    window.text_var.set("main")
    window.run()
    _wait(gui_app, lambda: window.result is not None)
    rows = window.rows()
    assert ("Main Save", "S9", "Save file", "Main Save", "", "") in rows
    assert ("Main Save", "S9", "Note", "Main run.", "", "") in rows
    assert "content index is off" in window.status.cget("text")
    assert view.open_search() is window
    window.text_var.set("old run")                 # only in the vault: that is where to look
    window.run()
    _wait(gui_app, lambda: window.rows() and window.rows()[0][0] == "Old Run")
    assert ("Old Run", "Vault", "Save name", "Old Run", "", "#1 (newest)") in window.rows()


def test_search_finds_bases_in_a_galaxy_and_shows_the_version_in_the_table(view, gui_app):
    from types import SimpleNamespace

    from .index.test_derive import facts_for

    pid = PIDS["main"]
    versions = view.model.versions(pid)
    stored = {versions[0].head: facts_for("rich_base")}
    window = view.open_search()
    window.facts = lambda: SimpleNamespace(facts=stored.get)
    window.field_var.set("Base galaxy")
    window.text_var.set("Eissentam")
    window.run()
    _wait(gui_app, lambda: window.result is not None)
    newest = f"#{len(versions)} (newest)"
    assert window.rows() == [
        ("Main Save", "S9", "Base galaxy", "10-Eissentam", "Synth Home", newest),
        ("Main Save", "S9", "Base galaxy", "10-Eissentam", "Synth Outpost", newest)]
    assert "not read by the content index yet" in window.status.cget("text")
    view.table.set_chips(("xbox",))                 # Main Save is hidden: the list comes back
    gui_app.update()
    window.show_selected()
    gui_app.update()
    assert view.table.selection_ref() == (pid, versions[0].head) and not view.table.chips


def test_a_search_result_names_its_versions_in_ranges():
    from nms_save_vault.ui.organizer.search import versions_text

    numbers = {f"v{n}": n for n in range(1, 15)}
    assert versions_text((), numbers) == ""                       # the save file itself
    assert versions_text(tuple(numbers), numbers) == "all 14"
    assert versions_text(("v14",), numbers) == "#14 (newest)"
    assert versions_text(("v5", "v3", "v2", "v1", "v9"), numbers) == "#1-#3, #5, #9"
    odd = tuple(f"v{n}" for n in range(1, 15, 2))
    assert versions_text(odd, numbers) == "#1, #3, #5, #7, #9, #11 +1 more"


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


def test_unreadable_slots_are_not_counted_as_live():
    from types import SimpleNamespace as NS

    def slot(state):
        return NS(state=state, vault_copy="durable")

    source = NS(label="Steam", slots=[slot("occupied")] * 3 + [slot("unreadable"), slot("empty")])
    model = NS(lines=lambda: [], versions=lambda pid: [], live_board=lambda: NS(sources=[source]))
    text = V.summary_text(model, None)
    assert "Steam 3/5 live, 1 unreadable" in text


def test_an_unaliased_account_id_and_the_home_path_never_reach_the_alerts(world, tmp_path,
                                                                           monkeypatch):
    from pathlib import Path

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "someone"))
    from nms_save_vault.core import aliases

    monkeypatch.setattr(aliases, "_HOME", None)
    aliases.set_active(aliases.AliasMap({}))
    path = tmp_path / "someone" / "NMS" / "st_76561198000000042"
    world.organizer.store.problems.append(f"cannot read {path}: damaged")
    world.organizer.store.problems.append("account 76561198000000043 is odd")
    text = " | ".join(V.scan_alerts(world.organizer).values())
    assert "76561198000000042" not in text and "76561198000000043" not in text
    assert "someone" not in text and "~" in text and "<steam id>" in text
