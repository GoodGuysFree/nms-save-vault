"""The History window (D6 U24): rows, tips and panel from D1's graph, and the window."""
from __future__ import annotations

import time
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
        assert win.problems.winfo_manager() and "left out" in win.problems.cget("text")
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


# --- the Compare window (U25) -------------------------------------------------------------


def test_the_ancestor_is_found_over_the_drawn_edges_only(tmp_path):
    from nms_save_vault.ui.organizer import compare as C

    w, (a, b, c, d, e, f), _ = _long(tmp_path)
    model = w.refresh()
    anc = C.common_ancestor(model, PID, f, c)
    assert anc.head == a and anc.text.startswith("#2, branch point; A played +")
    assert (anc.a_since, anc.b_since) == (3, 1)
    assert "ancestor of" in C.common_ancestor(model, PID, f, d).text
    assert C.common_ancestor(model, PID, f, e).head is None
    assert "no common ancestor recorded" in C.common_ancestor(model, PID, e, f).text


def test_facts_may_be_absent_and_a_fact_view_shows_when_present(tmp_path):
    import dataclasses
    from types import SimpleNamespace

    from nms_save_vault.ui.organizer import compare as C

    w, (a, b, c, d, e, f), _ = _long(tmp_path)
    model = w.refresh()
    ra, rf = (next(r for r in model.versions(PID) if r.head == h) for h in (a, f))
    facts = {x.name: x for x in C.compare_facts(model, PID, ra, rf)}
    assert facts["Play time"].differs and facts["Bases"].a == "not indexed"
    assert not facts["Platform"].differs
    va = SimpleNamespace(own_bases=3, base_parts=900, ship_names=("Old",), companion_names=())
    vf = SimpleNamespace(own_bases=4, base_parts=900, ship_names=("Old", "New"),
                         companion_names=())
    rich = {x.name: x for x in C.compare_facts(
        model, PID, dataclasses.replace(ra, facts=va), dataclasses.replace(rf, facts=vf))}
    assert (rich["Bases"].a, rich["Bases"].b) == ("3", "4") and not rich["Base parts"].differs
    assert C.named_differences(dataclasses.replace(ra, facts=va),
                               dataclasses.replace(rf, facts=vf)) == ["Ship: New - only in B"]


def test_the_compare_window_swaps_filters_and_picks(host, gui_app, tmp_path):
    from nms_save_vault.ui.organizer.compare import CompareWindow

    w, (a, b, c, d, e, f), _ = _long(tmp_path)
    model = w.refresh()
    win = CompareWindow(host, model=model, themed=gui_app.themed, pid=PID, a=f, b=c)
    gui_app.update()
    try:
        names = [r[0] for r in win.table()]
        assert "Play time" in names and "Platform" not in names     # differences only
        assert "ancestor: #2" in win.strip.cget("text")
        assert win.note.cget("text") == "content facts: not indexed yet for A and B"
        win.swap()
        assert (win.a, win.b) == (c, f)
        win.set_only_differences(False)
        assert "Platform" in [r[0] for r in win.table()]
        win.set_versions(a, e)
        assert "no common ancestor recorded" in win.strip.cget("text")
        win.pick()
    finally:
        win.destroy()


def test_compare_with_live_opens_from_the_history_and_says_when_nothing_is_live(view, gui_app):
    from .organizer_world import PIDS

    pid = PIDS["main"]
    view.table.select(pid)
    view.open_history()
    win = view.histories.history
    older = next(r.head for r in view.model.versions(pid) if not r.live_in)
    win._select_head(older)
    win.compare()
    cmp = view.histories.compare
    assert cmp is not None and cmp.a == older and cmp.b != older
    cmp.destroy()
    view.histories.compare = None
    assert view.histories.open_compare(PIDS["old"], view.model.versions(PIDS["old"])[0].head) is None
    assert "compare" in view.alerts


# --- the correction dialogs (U27) ---------------------------------------------------------


def _dialog(host, gui_app, w, cor, head, pid=PID):
    from nms_save_vault.ui.organizer.correct import CorrectDialog

    state = {"model": w.refresh(), "done": 0}

    def done():
        state["model"] = w.refresh()
        state["done"] += 1
        win.set_model(state["model"])

    win = CorrectDialog(host, model=state["model"], corrections=cor, worker=gui_app.worker,
                        themed=gui_app.themed, pid=pid, head=head, on_done=done)
    win.ask = lambda *a, **k: True
    win.state = state
    return win


def test_candidates_leave_out_the_version_and_what_came_after_it(forked):
    from nms_save_vault.ui.organizer import correct as K

    w, (a, b, c, d, e), _ = forked
    model = w.refresh()
    assert {h for _, h in K.candidate_parents(model, PID, b)} == {a, c, e}   # not b, not d
    assert set(K.branch_of(model, PID, b)) == {b, d}


def test_comes_from_is_drawn_then_undone_through_remove_correction(host, gui_app, forked):
    w, (a, b, c, d, e), cor = forked
    win = _dialog(host, gui_app, w, cor, e)
    try:
        win.parent_box.set(next(lbl for lbl, h in win._parents.items() if h == b))
        win.apply_parent()
        _wait(gui_app, lambda: win.state["done"] == 1)
        row = next(r for r in win.state["model"].versions(PID) if r.head == e)
        assert row.parent == b and row.in_tree
        mine = f"#{_number(win, e)} comes from #{_number(win, b)}"
        assert _texts(win).count(mine) == 1 and len(_texts(win)) == 4      # and the three before
        _select(win, mine)
        win.undo()
        _wait(gui_app, lambda: win.state["done"] == 2)
        assert next(r for r in win.state["model"].versions(PID) if r.head == e).parent is None
        assert len(_texts(win)) == 3
        win.parent_box.set(next(lbl for lbl, h in win._parents.items() if h == b))
        win.apply_parent()
        _wait(gui_app, lambda: win.state["done"] == 3)
        win.parent_box.set("(nothing - it has no known parent)")
        win.apply_parent()
        _wait(gui_app, lambda: win.state["done"] == 4)
        assert any("has no known parent" in t for t in _texts(win))
    finally:
        win.destroy()


def _texts(win):
    return [win.existing.item(i, "text") for i in win.existing.get_children()]


def _select(win, containing):
    win.existing.selection_set(next(i for i in win.existing.get_children()
                                    if containing in win.existing.item(i, "text")))


def _number(win, head):
    from nms_save_vault.ui.organizer.words import version_numbers

    return version_numbers(win.model, PID)[head]


def test_a_refused_correction_says_why_and_changes_nothing(host, gui_app, forked):
    w, (a, b, c, d, e), cor = forked
    win = _dialog(host, gui_app, w, cor, a)
    try:
        assert all(h not in (b, c, d) for h in win._parents.values())     # they come after a
        win.parent_box.set(next(lbl for lbl, h in win._parents.items() if h == e))
        win.corrections = type("X", (), {"set_parent": staticmethod(
            lambda *a, **k: (_ for _ in ()).throw(ValueError("that version comes after this one")
                                                  )), "state": cor.state})()
        win.apply_parent()
        _wait(gui_app, lambda: win.status.cget("text").startswith("Not done"))
        assert "comes after" in win.status.cget("text") and win.state["done"] == 0
    finally:
        win.destroy()


def test_split_makes_a_save_file_of_its_own_and_unsplit_takes_it_back(host, gui_app, forked):
    w, (a, b, c, d, e), cor = forked
    win = _dialog(host, gui_app, w, cor, c)
    try:
        assert "become a save file of their own" in win.split_info.cget("text")
        win.split_name.set("the other try")
        win.apply_split()
        _wait(gui_app, lambda: win.state["done"] == 1)
        lines = {l.name for l in win.state["model"].lines()}
        assert "the other try" in lines
        assert any("were split off as the other try" in t for t in _texts(win))
        _select(win, "were split off")
        win.undo()
        _wait(gui_app, lambda: win.state["done"] == 2)
        assert "the other try" not in {l.name for l in win.state["model"].lines()}
    finally:
        win.destroy()


def test_combine_joins_two_save_files_and_undo_separates_them(host, gui_app, forked):
    w, (a, b, c, d, e), cor = forked
    steam_save(w.steam, 15, 0, "Side Save", 50, OTHER, T0 + 50, extra="side")
    model = w.refresh()
    side = next(l for l in model.lines() if l.name == "Side Save")
    head = model.versions(side.pid)[0].head
    win = _dialog(host, gui_app, w, cor, head, pid=side.pid)
    try:
        win.combine_box.set("Main Save")
        win.apply_combine()
        _wait(gui_app, lambda: win.state["done"] == 1)
        assert {l.name for l in win.state["model"].lines()} == {"Main Save"}
        assert win.winfo_exists() is not None
    finally:
        win.destroy()
    win = _dialog(host, gui_app, w, cor, head, pid=PID)
    try:
        assert any("was combined into this one" in t for t in _texts(win))
        _select(win, "was combined into this one")
        win.undo()
        _wait(gui_app, lambda: win.state["done"] == 1)
        assert {l.name for l in win.state["model"].lines()} == {"Main Save", "Side Save"}
    finally:
        win.destroy()


def test_the_dialog_opens_from_the_history_window_and_a_read_only_store_says_so(view, gui_app):
    from .organizer_world import PIDS

    pid = PIDS["main"]
    view.table.select(pid)
    view.open_history()
    win = view.histories.history
    win.correct()
    dlg = view.histories.correct
    assert dlg is not None and dlg.pid == pid and dlg.head == win.head
    dlg.destroy()
    view.histories.correct = None
    view.organizer.store.read_only = True
    assert view.histories.open_correct(pid, win.head) is None and "correct" in view.alerts


class _Facts:
    def __init__(self, found):
        self.found, self.asked = found, []

    def changes(self, child, parent, *, edge_kind=None):
        self.asked.append((child, parent, edge_kind))
        return self.found


def test_the_explanation_wraps_to_the_width_it_has(host, gui_app, forked):
    w, _, _ = forked
    win = H.HistoryWindow(host, model=w.refresh(), themed=gui_app.themed, pid=PID)
    try:
        win.geometry("700x500")
        gui_app.update()
        wide = int(str(win.explain.cget("wraplength")))
        win.geometry("1400x500")
        gui_app.update()
        assert int(str(win.explain.cget("wraplength"))) > wide
        assert int(str(win.explain.cget("wraplength"))) <= win.explain.winfo_width()
    finally:
        win.destroy()


def test_what_changed_comes_from_the_content_index_for_the_drawn_parent(forked):
    from types import SimpleNamespace

    w, (a, b, c, d, e), _ = forked
    model = w.refresh()
    row = next(r for r in model.versions(PID) if r.head == d)
    found = SimpleNamespace(lines=lambda: ["2 bases changed", "new ship 'X' (Fighter)"])
    index = _Facts(found)
    text = "".join(t for t, _ in H.panel_segments(model, PID, d, index))
    assert index.asked == [(d, row.parent, row.parent_kind)]
    assert "new ship 'X' (Fighter)" in text and "+3 min played" in text
    pending = "".join(t for t, _ in H.panel_segments(model, PID, d, _Facts(None)))
    assert H.PENDING in pending and "+3 min played" in pending
    plain = "".join(t for t, _ in H.panel_segments(model, PID, d))
    assert H.PENDING not in plain and "+3 min played" in plain


class _StatusFacts(_Facts):
    def __init__(self, found=None, statuses=None):
        super().__init__(found)
        self.statuses = statuses or {}

    def status(self, vid):
        return self.statuses.get(vid, "queued")


def _event(kind, vid):
    from types import SimpleNamespace
    return SimpleNamespace(seq=1, kind=kind, vid=vid, status=None)


def test_an_unreadable_version_ends_the_wait_with_a_plain_line(forked):
    w, (a, b, c, d, e), _ = forked
    model = w.refresh()
    row = next(r for r in model.versions(PID) if r.head == d)
    for bad in (d, row.parent):
        facts = _StatusFacts(statuses={bad: "unreadable"})
        text = "".join(t for t, _ in H.panel_segments(model, PID, d, facts))
        assert H.UNREADABLE in text and H.PENDING not in text
    waiting = "".join(t for t, _ in H.panel_segments(model, PID, d, _StatusFacts()))
    assert H.PENDING in waiting and H.UNREADABLE not in waiting


def test_the_panel_follows_index_events_instead_of_polling(host, gui_app, forked):
    from types import SimpleNamespace

    w, (a, b, c, d, e), _ = forked
    model = w.refresh()
    row = next(r for r in model.versions(PID) if r.head == d)
    facts = _StatusFacts()
    win = H.HistoryWindow(host, model=model, themed=gui_app.themed, pid=PID, head=d,
                          facts=lambda: facts)
    try:
        assert H.PENDING in win.panel_text()
        gui_app.update()
        asked = len(facts.asked)
        time.sleep(0.9)                                        # longer than the old 700 ms poll
        gui_app.update()
        assert len(facts.asked) == asked
        win.on_index_events([_event("changes", "someone-else")])
        assert len(facts.asked) == asked                       # not about this version
        facts.found = SimpleNamespace(lines=lambda: ["2 bases changed"])
        win.on_index_events([_event("changes", d)])
        assert "2 bases changed" in win.panel_text() and H.PENDING not in win.panel_text()
        facts.found = None
        facts.statuses = {row.parent: "error"}
        win.on_index_events([_event("failed", row.parent)])
        assert H.UNREADABLE in win.panel_text()
    finally:
        win.destroy()


def test_an_event_for_a_version_no_longer_shown_leaves_the_panel_alone(host, gui_app, forked):
    w, (a, b, c, d, e), _ = forked
    facts = _StatusFacts()
    win = H.HistoryWindow(host, model=w.refresh(), themed=gui_app.themed, pid=PID, head=d,
                          facts=lambda: facts)
    try:
        win._select_head(a)
        before, asked = win.panel_text(), len(facts.asked)
        win.on_index_events([_event("changes", d), _event("indexed", d)])
        assert win.panel_text() == before and len(facts.asked) == asked
        assert win.head == a
    finally:
        win.destroy()
