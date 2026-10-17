"""Regressions from the review of the history UI (D6 U23-U27): every dialog acts on the exact
effective save file or version it shows."""
from __future__ import annotations

import tkinter as tk

import pytest

from nms_save_vault.core.organizer.lineage import Corrections
from nms_save_vault.core.organizer.notes import Notes
from nms_save_vault.ui.organizer import compare as C
from nms_save_vault.ui.organizer import correct as K
from nms_save_vault.ui.organizer import history as H
from nms_save_vault.ui.organizer import table as T
from nms_save_vault.ui.organizer.notes import note_diff

from .conftest import steam_save
from .test_organizer_history import _forked, game_not_running  # noqa: F401
from .test_organizer_readmodel import MAIN, OTHER, OTHER_PID, PID, T0, World, _vid
from .test_ui_history import _dialog, _long, _texts, _wait


@pytest.fixture
def host(gui_app):
    top = tk.Toplevel(gui_app)
    top.withdraw()
    yield top
    top.destroy()


def _cor(store):
    ticks = iter(range(T0, T0 + 99))
    return Corrections(store, clock=lambda: next(ticks))


def _snapshot(notes, model, pid, head) -> str:
    return "".join(t for t, _ in note_diff(notes, model, pid, head))


def test_combine_from_a_split_line_moves_that_line_not_the_original(host, gui_app, tmp_path):
    w, (a, b, c, d, e), cor = _forked(tmp_path)
    steam_save(w.steam, 15, 0, "Side Save", 50, OTHER, T0 + 50, extra="side")
    split = cor.split(PID, [c], "the other try")
    win = _dialog(host, gui_app, w, cor, c, pid=split)
    try:
        win.combine_box.set("Side Save")
        win.apply_combine()
        _wait(gui_app, lambda: win.state["done"] == 1)
    finally:
        win.destroy()
    m = w.refresh()
    assert [(x["pid"], x["into"]) for x in cor.state().aliases] == [(split, OTHER_PID)]
    assert m.line_of(a) == PID and m.line_of(c) == OTHER_PID


def test_same_named_save_files_are_separate_combine_targets(host, gui_app, tmp_path):
    w, (a, b, c, d, e), cor = _forked(tmp_path)
    steam_save(w.steam, 15, 0, "Side Save", 50, OTHER, T0 + 50, extra="side")
    third = 0x1234567890ABCDEF
    steam_save(w.steam, 13, 0, "Main Save", 70, third, T0 + 70, extra="third")
    model = w.refresh()
    side = next(x for x in model.lines() if x.name == "Side Save")
    win = _dialog(host, gui_app, w, cor, model.versions(side.pid)[0].head, pid=side.pid)
    try:
        values = list(win.combine_box.cget("values"))
        assert len(values) == 2 and all(v.startswith("Main Save (") for v in values)
        assert set(win._into.values()) == {PID, f"{third:016X}"}
        label = next(v for v, p in win._into.items() if p == f"{third:016X}")
        win.combine_box.set(label)
        win.apply_combine()
        _wait(gui_app, lambda: win.state["done"] == 1)
    finally:
        win.destroy()
    assert [(x["pid"], x["into"]) for x in cor.state().aliases] == [(OTHER_PID, f"{third:016X}")]


def test_the_note_snapshot_is_read_under_the_raw_save_file(tmp_path):
    w = World(tmp_path)
    notes = Notes(w.store)
    steam_save(w.steam, 9, 0, "Main Save", 100, MAIN, T0 + 100, extra="m1")
    notes.set_save_note(PID, "main first words", base_rev=None)
    w.refresh()
    steam_save(w.steam, 3, 0, "Side Save", 50, OTHER, T0 + 50, extra="side")
    notes.set_save_note(OTHER_PID, "side secret plan", base_rev=None)
    w.refresh()
    side_head = _vid(w.steam, 3)
    _cor(w.store).combine([OTHER_PID], PID, names={OTHER_PID: "Side Save"})
    model = w.refresh()
    assert model.line_of(side_head) == PID
    assert "side secret plan" in _snapshot(notes, model, PID, side_head)
    assert "main first words" not in _snapshot(notes, model, PID, side_head)


def test_the_note_snapshot_of_a_split_line_is_found(tmp_path):
    w = World(tmp_path)
    notes = Notes(w.store)
    steam_save(w.steam, 9, 0, "Main Save", 100, MAIN, T0 + 100, extra="m1")
    notes.set_save_note(PID, "main first words", base_rev=None)
    w.refresh()
    head = _vid(w.steam, 9)
    split = _cor(w.store).split(PID, [head], "try")
    model = w.refresh()
    assert "main first words" in _snapshot(notes, model, split, head)


def _fork_member_world(tmp_path):
    w = World(tmp_path)
    steam_save(w.steam, 9, 0, "Main Save", 200, MAIN, T0 + 200, extra="head")
    steam_save(w.steam, 9, 1, "Main Save", 100, MAIN, T0 + 100, extra="partner")
    steam_save(w.steam, 3, 0, "Main Save", 300, MAIN, T0 + 300, extra="x")
    steam_save(w.steam, 5, 0, "Main Save", 400, MAIN, T0 + 400, extra="y")
    partner, x, y = _vid(w.steam, 9, 1), _vid(w.steam, 3), _vid(w.steam, 5)
    cor = _cor(w.store)
    cor.set_parent(x, partner)
    cor.set_parent(y, partner)
    return w, partner, x, y, cor


def test_a_fork_point_that_is_no_head_has_a_row_the_branches_attach_to(tmp_path):
    w, partner, x, y, _ = _fork_member_world(tmp_path)
    model = w.refresh()
    pair = next(r for r in model.versions(PID) if partner in r.pair)
    rows = H.history_rows(model, PID, "all")
    fork = [r for r in rows if r.key == ("m", partner)]
    assert len(fork) == 1 and fork[0].head == pair.head and fork[0].image[1].kind == "member"
    assert [r.head for r in rows if r.head == pair.head] == [pair.head]     # not also dated
    panel = "".join(t for t, _ in H.panel_segments(model, PID, x))
    assert "no recorded parent" not in panel and "its parent, set by you" in panel
    pair_panel = "".join(t for t, _ in H.panel_segments(model, PID, pair.head))
    assert "2 branches continue from here" in pair_panel
    anc = C.common_ancestor(model, PID, x, y)
    assert anc.head == pair.head and "branch point" in anc.text
    assert partner in {h for _, h in K.candidate_parents(model, PID, x)}
    specs = T.version_specs(model, PID, {})
    assert ("m", partner) in [s.key for s in specs]
    assert len([s for s in specs if s.head == pair.head]) == 1


def test_comes_from_shows_the_restore_point_and_set_never_erases_a_link(host, gui_app,
                                                                         tmp_path):
    w, partner, x, y, cor = _fork_member_world(tmp_path)
    win = _dialog(host, gui_app, w, cor, x)
    try:
        assert "restore point" in win.parent_box.get()
        assert win._parents[win.parent_box.get()] == partner
        win.apply_parent()                                  # unchanged: nothing is written
        assert win.status.cget("text") == "It already comes from that."
        assert len(cor.state().corrections) == 2
    finally:
        win.destroy()


def test_problems_belong_to_the_save_file_and_use_plain_words(tmp_path):
    w, (a, b, c, d, e), cor = _forked(tmp_path)
    cor.set_parent(a, d)                                    # a cycle in Main Save
    steam_save(w.steam, 15, 0, "Side Save", 50, OTHER, T0 + 50, extra="side")
    model = w.refresh()
    assert model.history(OTHER_PID, "all").problems == ()
    mine = model.history(PID, "all").problems
    assert len(mine) == 1 and "cor-" not in mine[0] and "would close a cycle" in mine[0]
    assert "left out" in mine[0]


def test_the_correct_list_marks_a_dropped_correction(host, gui_app, tmp_path):
    w, (a, b, c, d, e), cor = _forked(tmp_path)
    cor.set_parent(a, d)
    win = _dialog(host, gui_app, w, cor, a)
    try:
        assert any("not in effect" in t for t in _texts(win))
    finally:
        win.destroy()


def test_each_platform_live_copy_is_its_own_tip_and_a_lagging_one_is_behind(
        tmp_path, make_wgs_account):
    w = World(tmp_path)
    steam_save(w.steam, 2, 0, "Main Save", 9000, MAIN, T0 + 50, extra="s1")
    w.add_xbox(make_wgs_account, [("Slot4Auto", '{"x":"x1"}', "Main Save", "", 8000, T0, MAIN)])
    model = w.refresh()
    tips = H.branch_tips(model, PID)
    assert len(tips) == 2 and all(t.live for t in tips)
    assert any("behind by" in t.since for t in tips)


def test_history_opened_on_a_folded_version_unfolds_it(host, gui_app, tmp_path):
    w, (a, b, c, d, e, f), _ = _long(tmp_path)
    model = w.refresh()
    win = H.HistoryWindow(host, model=model, themed=gui_app.themed, pid=PID, head=b)
    try:
        assert win.head == b and b in win._iid_of_head
        assert win.tree.selection() == (win._iid_of_head[b],)
        win.set_mode("all")
        win.set_mode("key")                                 # folds again: b stays selected
        assert win.head == b and b in win._iid_of_head
        win.apply_palette(win._palette)                     # a theme or zoom change
        assert win.head == b and win.tree.selection() == (win._iid_of_head[b],)
        win.set_model(model)
        assert win.head == b
    finally:
        win.destroy()


def test_a_version_that_is_gone_selects_nothing(host, gui_app, tmp_path):
    w, (a, b, c, d, e, f), _ = _long(tmp_path)
    win = H.HistoryWindow(host, model=w.refresh(), themed=gui_app.themed, pid=PID, head=c)
    try:
        win._fill(select="0" * 64)
        assert win.head is None and win.tree.selection() == ()
    finally:
        win.destroy()


def test_the_branch_tips_grid_has_a_limited_height_and_scrolls(host, gui_app, tmp_path):
    w, *_ = _long(tmp_path)
    win = H.HistoryWindow(host, model=w.refresh(), themed=gui_app.themed, pid=PID)
    gui_app.update()
    try:
        assert int(win.tips_canvas.cget("height")) <= 120
        assert win.tips_canvas.cget("yscrollcommand")
        assert win.tips_canvas.bbox("all") is not None
    finally:
        win.destroy()


def test_a_correction_that_finishes_after_the_dialog_closed_still_reloads(host, gui_app,
                                                                         tmp_path):
    import threading

    w, (a, b, c, d, e), cor = _forked(tmp_path)
    win = _dialog(host, gui_app, w, cor, e)
    gate = threading.Event()
    gui_app.worker.submit(gate.wait, 10, on_done=lambda _r: None)
    win.parent_box.set(next(lbl for lbl, h in win._parents.items() if h == b))
    win.apply_parent()
    win.destroy()
    gate.set()
    _wait(gui_app, lambda: win.state["done"] == 1)
    assert len(cor.state().corrections) == 4


def test_a_notes_window_follows_a_combine_and_never_saves_under_a_gone_save_file(
        host, gui_app, tmp_path):
    from nms_save_vault.ui.organizer.notes import NotesWindow

    w, (a, b, c, d, e), cor = _forked(tmp_path)
    steam_save(w.steam, 15, 0, "Side Save", 50, OTHER, T0 + 50, extra="side")
    notes = Notes(w.store)
    nw = NotesWindow(host, notes=notes, model=w.refresh(), worker=gui_app.worker,
                     themed=gui_app.themed, pid=OTHER_PID)
    try:
        nw.editor.insert("1.0", "typed while combining")
        cor.combine([OTHER_PID], PID, names={OTHER_PID: "Side Save"})
        nw.set_model(w.refresh())
        assert nw.pid == PID and nw.title() == "Notes - Main Save"
        assert "typed while combining" in nw.editor.get("1.0", "end-1c")
        nw.save_note()
        _wait(gui_app, lambda: notes.save_note(PID) is not None
              and "typed while combining" in notes.save_note(PID).text)
        assert notes.save_note(OTHER_PID) is None
    finally:
        nw.destroy()


def test_the_snapshot_diff_runs_in_the_worker(host, gui_app, tmp_path, monkeypatch):
    import threading

    from nms_save_vault.ui.organizer import notes as N

    w, *_ = _long(tmp_path)
    seen = []
    real = N.note_diff

    def spy(*a, **k):
        seen.append(threading.current_thread() is threading.main_thread())
        return real(*a, **k)

    monkeypatch.setattr(N, "note_diff", spy)
    nw = N.NotesWindow(host, notes=Notes(w.store), model=w.refresh(), worker=gui_app.worker,
                       themed=gui_app.themed, pid=PID)
    try:
        _wait(gui_app, lambda: seen)
        assert seen and not any(seen)
    finally:
        nw.destroy()
