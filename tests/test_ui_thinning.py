"""Thin the vault... (docs/thinning.md "The apps", Q4 and Q5): the list window plans in the UI
worker, shows one ticked row per backup that goes and what stays against the schedule, deletes
only the rows left ticked after the click, with progress, and says what went and what was
left. Made-up vaults in tmp_path only."""
from __future__ import annotations

import threading
import time
import tkinter as tk

import pytest

from nms_save_vault.core import safety
from nms_save_vault.core import store as vstore
from nms_save_vault.core.store import ThinError, thinning, versionstore
from nms_save_vault.ui import dialogs
from nms_save_vault.ui import thinning as TW

from .test_cli_thin import thin_world
from .test_theme_dark_audit import audit, mode  # noqa: F401  (mode: a fixture)


def _wait(app, condition, timeout: float = 30.0) -> None:
    end = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < end, "timed out"
        app.update()
        time.sleep(0.005)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setattr(safety, "is_game_running", lambda: False)
    monkeypatch.setattr(versionstore.discover, "all_live_paths", lambda *a, **k: [])


@pytest.fixture
def world(tmp_path):
    return thin_world(tmp_path)


@pytest.fixture
def opened(gui_app):
    windows = []

    def make(world, master=None):
        changed = []
        win = TW.ThinWindow(master or gui_app, worker=gui_app.worker, themed=gui_app.themed,
                            vault_root=world.root, on_changed=lambda: changed.append(1))
        win.changed = changed
        windows.append(win)
        _wait(gui_app, lambda: win.ready)
        return win
    yield make
    for win in windows:
        if win.winfo_exists():
            win.busy = False
            win.destroy()
    gui_app.update()


def entries(world) -> set[str]:
    return {e.id for e in vstore.open_store(world.root).entries()}


def _row_values(win, iid):
    return (win.tree.item(iid, "text"), *win.tree.item(iid, "values"))


# --- the words ------------------------------------------------------------------------------------


def test_the_words():
    assert TW.size_text(3 * (1 << 30) // 2) == "1.5 GB"
    assert TW.size_text(12 * (1 << 20)) == "12.0 MB" and TW.size_text(2048) == "2 KB"
    assert TW.delete_text(1, 2048) == "Delete 1 entry (frees 2 KB)"
    assert TW.delete_text(3, 1 << 30) == "Delete 3 entries (frees 1.0 GB)"
    assert TW.kind_text("full") == "full backup" and TW.kind_text("preserve") == "preserve"
    assert TW.when_text("2024-03-03T14:01:00") == "2024-03-03 14:01"
    saves = (thinning.ThinSave(1, "Main"), thinning.ThinSave(3, "Other"))
    assert TW.saves_text(saves) == "slot 1 Main, slot 3 Other"


# --- the list -------------------------------------------------------------------------------------


def test_every_entry_that_goes_is_a_ticked_row_with_its_words(gui_app, world, opened):
    win = opened(world)
    plan = win.plan
    assert [c.id for c in plan.candidates] == list(win.tree.get_children(""))
    for c in plan.candidates:
        assert _row_values(win, c.id) == (
            TW.TICKED, TW.when_text(c.created), "full backup", TW.saves_text(c.saves),
            TW.size_text(c.bytes), c.reason)
    assert win.delete_button.cget("text") == TW.delete_text(len(plan.candidates), plan.bytes)
    assert win.delete_button.instate(["!disabled"])
    kept = win.kept_tree.get_children("")
    assert list(kept) == [world.pinned.id]
    assert win.kept_tree.item(world.pinned.id, "values")[-1] == "you pinned it"
    assert "(1)" in win.kept_title.cget("text")
    assert TW.SCHEDULE_TEXT.split(":")[0] in win.intro.cget("text")
    assert entries(world) >= {c.id for c in plan.candidates}       # nothing deleted by looking


def test_unticking_a_row_keeps_it_and_the_button_follows(gui_app, world, opened):
    win = opened(world)
    keep = world.old.id
    others = [c.id for c in win.plan.candidates if c.id != keep]
    win.toggle(keep)
    assert win.tree.item(keep, "text") == TW.UNTICKED
    assert win.delete_button.cget("text") == TW.delete_text(
        len(others), win.plan.freed_bytes(others))
    win.toggle(keep)
    assert win.tree.item(keep, "text") == TW.TICKED
    for iid in win.tree.get_children(""):
        win.toggle(iid)
    assert win.delete_button.cget("text").startswith("Delete 0 entries")
    assert win.delete_button.instate(["disabled"])


def test_space_ticks_the_selected_row(gui_app, world, opened):
    win = opened(world)
    win.tree.selection_set(world.old.id)
    win._space()
    assert win.tree.item(world.old.id, "text") == TW.UNTICKED


def test_delete_removes_only_the_ticked_rows_then_says_what_went_and_refreshes(
        gui_app, world, opened):
    win = opened(world)
    keep = world.old.id
    gone = [c.id for c in win.plan.candidates if c.id != keep]
    win.toggle(keep)
    win.delete()
    _wait(gui_app, lambda: not win.busy)
    assert keep in entries(world) and not set(gone) & entries(world)
    assert f"{len(gone)} backup" in win.result.cget("text") and "deleted" in win.result.cget("text")
    assert all(win.tree.item(i, "text") == TW.DELETED for i in gone)
    assert win.tree.item(keep, "text") == TW.UNTICKED
    assert win.changed == [1] and not win.delete_button.winfo_manager()
    assert win.cancel_button.cget("text") == "Close"
    assert vstore.open_store(world.root).fsck().ok


def test_what_was_left_is_said_with_why(gui_app, world, opened):
    win = opened(world)
    thinning.pin_entry(world.root, world.old.id)            # in another window, meanwhile
    win.delete()
    _wait(gui_app, lambda: not win.busy)
    text = win.result.cget("text")
    assert world.old.id in entries(world)
    assert "Kept: " in text and "you pinned it" in text and "slot 1 Main" in text
    assert win.tree.item(world.old.id, "text") == TW.LEFT
    assert win.tree.item(world.old.id, "values")[-1] == "you pinned it"


def test_cancel_and_escape_close_and_delete_nothing(gui_app, world, opened):
    before = entries(world)
    win = opened(world)
    assert win.cancel_button.cget("text") == "Cancel"
    assert win.bind("<Escape>")
    win._escape_handler()
    assert not win.winfo_exists() and entries(world) == before
    win = opened(world)
    win.cancel_button.invoke()
    assert not win.winfo_exists() and entries(world) == before


def test_a_blocked_plan_says_why_and_offers_no_delete(gui_app, world, opened):
    store = vstore.open_store(world.root)
    digest = next(f.sha256 for e in store.entries() for f in e.files if f.inline is None)
    path = store.objects.path_for(digest)
    path.write_bytes(path.read_bytes()[:-1])
    before = entries(world)
    win = opened(world)
    assert "problem(s)" in win.message.cget("text") and "nothing is thinned" in win.message.cget("text")
    assert not win.delete_button.winfo_manager() and not win.tree.get_children("")
    assert win.cancel_button.cget("text") == "Close"
    win.delete()                                               # not offered: does nothing
    gui_app.update()
    assert entries(world) == before and not win.busy


def test_nothing_to_thin_says_so(gui_app, world, opened):
    while plan := thinning.plan_thin(world.root).candidates:   # a pin can make another go
        for c in plan:
            thinning.pin_entry(world.root, c.id)
    win = opened(world)
    assert win.message.cget("text") == TW.NOTHING_TEXT
    assert not win.delete_button.winfo_manager()


def test_a_plan_that_cannot_be_made_is_said_plainly(gui_app, tmp_path, opened):
    old = tmp_path / "old vault"
    old.mkdir()
    win = opened(type("W", (), {"root": old})())
    assert "Upgrade the vault first" in win.message.cget("text")
    assert not win.delete_button.winfo_manager()


def test_a_refused_run_is_said_and_the_lists_still_refresh(gui_app, world, opened, monkeypatch):
    def busy(*a, **k):
        raise ThinError("busy", "Another NMS Save Vault window is changing the vault.")
    monkeypatch.setattr(TW.thinning, "run_thin", busy)
    win = opened(world)
    win.delete()
    _wait(gui_app, lambda: not win.busy)
    assert "Another NMS Save Vault window" in win.message.cget("text")
    assert win.changed == [1]


def test_the_run_shows_its_progress_and_stop_stops_it(gui_app, world, opened, monkeypatch):
    started, release = threading.Event(), threading.Event()

    def slow(root, plan, chosen, *, progress=None, cancel=None):
        progress(thinning.ThinProgress(1, 3, chosen[0]))
        started.set()
        release.wait(10)
        return thinning.ThinResult((), 0, tuple((i, "not deleted: you stopped") for i in chosen),
                                   cancel.is_set(), "")
    monkeypatch.setattr(TW.thinning, "run_thin", slow)
    win = opened(world)
    win.delete()
    _wait(gui_app, started.is_set)
    _wait(gui_app, lambda: win.bar.cget("value") == 1)
    assert win.busy and float(win.bar.cget("maximum")) == 3
    assert win.status.cget("text") == "Deleting 2 of 3..."
    assert win.delete_button.instate(["disabled"]) and win.stop_button.winfo_manager() == "pack"
    win._escape_handler()                                     # never while it runs
    assert win.winfo_exists()
    win.stop_button.invoke()
    release.set()
    _wait(gui_app, lambda: not win.busy)
    assert win.result.cget("text").startswith("Stopped:")


def test_the_window_follows_the_theme(gui_app, world, opened, mode):
    win = opened(world)
    win.toggle(world.old.id)                     # an unticked row has its own colour
    gui_app.update()
    assert audit(win, mode) == []


def test_the_window_opens_over_the_main_window_with_the_list_focused(gui_app, world, opened):
    assert dialogs.over((100, 50, 1000, 600), (400, 300)) == (400, 150)
    assert dialogs.over((-1900, 0, 800, 600), (1000, 700)) == (-1900, 0)    # never off its left
    main = tk.Toplevel(gui_app)                 # a main window that is on screen (off the owner's)
    main.geometry("1000x700")
    try:
        gui_app.update()
        win = opened(world, master=main)
        gui_app.update()
        x, y = dialogs.over((main.winfo_rootx(), main.winfo_rooty(), main.winfo_width(),
                             main.winfo_height()), dialogs.window_size(win))
        assert win.geometry().endswith(f"+{x}+{y}")
        assert str(win.tk.call("focus", "-lastfor", win)) == str(win.tree)
        assert str(win.transient()) == str(main)
        win.destroy()
    finally:
        main.destroy()
