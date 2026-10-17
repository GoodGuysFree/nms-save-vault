"""The History window (D6 U24): rows, tips and panel from D1's graph, and the window."""
from __future__ import annotations

import tkinter as tk

import pytest

from nms_save_vault.core import operations
from nms_save_vault.ui.organizer import history as H

from .conftest import steam_save
from .test_organizer_history import _forked, game_not_running  # noqa: F401
from .test_organizer_readmodel import MAIN, OTHER, PID, T0, _vid


@pytest.fixture
def forked(tmp_path):
    w, vids, cor = _forked(tmp_path)
    return w, vids, cor


def _long(tmp_path):
    """a -> b -> d -> f and a -> c; e unlinked; nothing live (so key mode folds b and d)."""
    w, (a, b, c, d, e), cor = _forked(tmp_path)
    steam_save(w.steam, 13, 0, "Main Save", 500, MAIN, T0 + 500, extra="s13")
    f = _vid(w.steam, 13)
    cor.set_parent(f, d)
    operations.create_full_backup(w.vault, w.steam, warnings=[])
    for slot in (9, 3, 5, 7, 11, 13):
        steam_save(w.steam, slot, 0, "Other", 1, OTHER, T0, extra=f"o{slot}")
    return w, (a, b, c, d, e, f), cor


def test_rows_have_sections_how_it_is_linked_and_the_dated_list(forked):
    w, (a, b, c, d, e), _ = forked
    rows = H.history_rows(w.refresh(), PID, "all")
    kinds = [(r.kind, r.head) for r in rows]
    assert kinds[0] == ("sec", None) and rows[0].text == "LINKED HISTORY"
    assert [h for k, h in kinds if h] == [d, c, b, a, e]
    how = {r.head: r.values[3] for r in rows if r.head}
    assert how == {d: "set by you", c: "set by you", b: "set by you", a: "branch starts here",
                   e: "first seen"}
    assert rows[-2].text == "NOT LINKED - BY DATE" or rows[-2].kind == "sec"
    assert rows[-1].tags[:2] == ("ver", "dim")
    assert all(r.image[0] == "lane" for r in rows if r.head in (a, b, c, d))
    assert [r.values[1] for r in rows if r.head][:1] == ["0h06"]      # 400 s of play


def test_key_mode_folds_the_plain_steps_into_a_run_you_can_open(tmp_path):
    w, (a, b, c, d, e, f), _ = _long(tmp_path)
    model = w.refresh()
    rows = H.history_rows(model, PID, "key")
    runs = [r for r in rows if r.kind == "run"]
    assert len(runs) == 1 and set(runs[0].run) == {b, d}
    assert runs[0].values[0] == "> 2 more versions" and "click to show" in runs[0].values[6]
    assert b not in [r.head for r in rows]
    opened = H.history_rows(model, PID, "key", {runs[0].key})
    assert {b, d} <= {r.head for r in opened if r.head}
    assert "click to hide" in next(r for r in opened if r.kind == "run").values[6]
    assert H.key_counts(model, PID)[1] == 6


def test_the_tips_say_where_each_branch_started(tmp_path):
    w, (a, b, c, d, e, f), _ = _long(tmp_path)
    tips = H.branch_tips(w.refresh(), PID)
    assert [t.head for t in tips] == [f, c]
    main, side = tips
    assert main.name == "main line" and side.name == "branch 2"
    assert main.since.endswith("3 versions since") and side.since.endswith("1 version since")
    assert main.where == "Vault" and not main.live


def test_the_panel_names_the_parent_and_branches_and_says_when_there_is_none(forked):
    w, (a, b, c, d, e), _ = forked
    model = w.refresh()
    text = "".join(t for t, _ in H.panel_segments(model, PID, a))
    assert "2 branches continue from here" in text and "nothing shown - no recorded parent" in text
    child = "".join(t for t, _ in H.panel_segments(model, PID, d))
    assert "WHAT CHANGED SINCE #" in child and "its parent, set by you" in child
    assert "+3 min played" in child and "not indexed yet" in child


@pytest.fixture
def host(gui_app):
    top = tk.Toplevel(gui_app)
    top.withdraw()
    yield top
    top.destroy()


def test_the_window_shows_rows_tips_panel_and_follows_the_buttons(host, gui_app, tmp_path):
    w, (a, b, c, d, e, f), _ = _long(tmp_path)
    model = w.refresh()
    calls = []
    win = H.HistoryWindow(host, model=model, themed=gui_app.themed, pid=PID, head=c,
                          on_move=lambda *x: calls.append(("move", *x)),
                          on_compare=lambda *x: calls.append(("compare", *x)),
                          on_lock=lambda *x: calls.append(("lock", *x)))
    gui_app.update()
    try:
        assert win.title() == "History - Main Save" and win.head == c
        assert win.counts.cget("text").startswith("6 versions - 2 branch tips")
        assert "Key versions" in win.key_button.cget("text")
        assert [r.kind for r in win.rows.values()].count("run") == 1
        assert "WHAT CHANGED" in win.panel_text() and win.lock_button.cget("text") == "Lock"
        win.move()
        win.compare()
        win.lock()
        assert calls == [("move", PID, c), ("compare", PID, c, None), ("lock", PID, c, True)]
        win.set_mode("all")
        assert b in win._iid_of_head and win.head == c
        run_iid = next(i for i, r in win.rows.items() if r.kind == "ver" and r.head == a)
        win.tree.selection_set(win._iid_of_head[d], run_iid)
        win.compare()
        assert calls[-1][0] == "compare" and calls[-1][2:] == (d, a) or calls[-1][2:] == (a, d)
        win.set_mode("key")
        key = next(r.key for r in win.rows.values() if r.kind == "run")
        win.toggle_run(key)
        assert b in win._iid_of_head
    finally:
        win.destroy()


def test_dropped_corrections_are_noted(host, gui_app, forked):
    w, (a, b, c, d, e), cor = forked
    cor.set_parent(a, d)                      # a cycle: it comes after
    model = w.refresh()
    win = H.HistoryWindow(host, model=model, themed=gui_app.themed, pid=PID)
    try:
        assert model.history(PID, "all").problems
        assert win.problems.winfo_manager() and "Left out" in win.problems.cget("text")
    finally:
        win.destroy()


# --- opened from the Organizer tab -------------------------------------------------------


@pytest.fixture
def view(gui_app, tmp_path, make_wgs_account, monkeypatch):
    import time

    from nms_save_vault.core import safety
    from nms_save_vault.ui.organizer import view as V

    from .organizer_world import build_world

    monkeypatch.setattr(safety, "is_game_running", lambda: False)
    world = build_world(tmp_path, make_wgs_account)
    top = tk.Toplevel(gui_app)
    top.withdraw()
    widget = V.OrganizerView(top, worker=gui_app.worker, themed=gui_app.themed, prefs={},
                             save_prefs=lambda: None, make_organizer=lambda: world.organizer)
    widget.pack(fill=tk.BOTH, expand=True)
    widget.rescan()
    end = time.monotonic() + 15
    while widget.scanning:
        assert time.monotonic() < end
        gui_app.update()
        time.sleep(0.005)
    widget.world = world
    yield widget
    if widget.histories.history is not None and widget.histories.history.winfo_exists():
        widget.histories.history.destroy()
    top.destroy()


def _wait(app, condition, timeout: float = 15.0) -> None:
    import time

    end = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < end, "timed out"
        app.update()
        time.sleep(0.005)


def test_history_opens_from_the_tab_and_a_lock_goes_through_the_worker(view, gui_app):
    from .organizer_world import PIDS

    pid = PIDS["main"]
    view.table.select(pid)
    view.open_history()
    win = view.histories.history
    assert win is not None and win.pid == pid and win.title() == "History - Main Save"
    assert view.history_button.winfo_exists() and view.detail.history_button.winfo_exists()
    head = win.head
    win.lock()
    _wait(gui_app, lambda: not view.scanning and any(
        r.locked for r in view.model.versions(pid) if r.head == head))
    assert win.lock_button.cget("text") == "Unlock"          # the window followed the new model
    again = view.histories.open_history(pid)
    assert again is win
