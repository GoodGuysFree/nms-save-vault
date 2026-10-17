"""organizer.lineage: the owner's corrections, Combine and Split, and the effective save file
of every version (D1 2.7)."""
from __future__ import annotations

import pytest

from nms_save_vault.core import safety
from nms_save_vault.core.organizer.lineage import Corrections, LineageGraph, read_lineage
from nms_save_vault.core.organizer.notes import Notes

from .conftest import steam_save
from .test_organizer_readmodel import MAIN, OTHER, OTHER_PID, PID, T0, World, _vid


@pytest.fixture(autouse=True)
def game_not_running(monkeypatch):
    monkeypatch.setattr(safety, "is_game_running", lambda: False)


def _three(tmp_path):
    """Three versions of one save file in three slots: nothing links them."""
    w = World(tmp_path)
    steam_save(w.steam, 9, 0, "Main Save", 100, MAIN, T0, extra="a")
    steam_save(w.steam, 3, 0, "Main Save", 200, MAIN, T0 + 10, extra="b")
    steam_save(w.steam, 5, 0, "Main Save", 300, MAIN, T0 + 20, extra="c")
    return w, [_vid(w.steam, s) for s in (9, 3, 5)]


def _corrections(store):
    ticks = iter(range(T0, T0 + 99))                 # corrections made in order, second by second
    return Corrections(store, clock=lambda: next(ticks))


def _row(model, vid, pid=PID):
    return next(r for r in model.versions(pid) if r.head == vid)


def test_without_a_correction_nothing_is_linked(tmp_path):
    w, _ = _three(tmp_path)
    model = w.refresh()
    assert all(not r.in_tree and r.parent is None for r in model.versions(PID))


def test_an_owner_parent_draws_the_link(tmp_path):
    w, (a, b, c) = _three(tmp_path)
    w.refresh()
    cor = _corrections(w.store)
    cor.set_parent(b, a, note="I know this one")
    model = w.refresh()
    row = _row(model, b)
    assert (row.parent, row.parent_tier, row.parent_method) == (a, "owner", "owner")
    assert row.in_tree and _row(model, a).in_tree and not _row(model, c).in_tree
    cor.set_parent(b, None)                    # a later correction wins: "no known parent"
    row = _row(w.refresh(), b)
    assert row.parent is None and not row.in_tree


def test_the_journals_edge_is_overridden_and_unrelated_drops_it(tmp_path):
    w = World(tmp_path)
    steam_save(w.steam, 9, 0, "Main Save", 100, MAIN, T0, extra="a")
    w.refresh()
    steam_save(w.steam, 9, 0, "Main Save", 200, MAIN, T0 + 10, extra="b")
    model = w.refresh()
    new, old = model.versions(PID)[0].head, model.versions(PID)[1].head
    assert _row(model, new).parent == old and _row(model, new).parent_tier == "observed"
    cor = _corrections(w.store)
    c1 = cor.mark_unrelated(new, old)
    assert _row(w.refresh(), new).parent is None
    cor.remove_correction(c1["id"])
    assert _row(w.refresh(), new).parent == old
    cor.set_parent(new, None)
    assert _row(w.refresh(), new).parent is None
    with pytest.raises(ValueError):
        cor.remove_correction("cor-nope")


def test_corrections_are_validated(tmp_path):
    w, (a, b, c) = _three(tmp_path)
    steam_save(w.steam, 7, 0, "Other Run", 50, OTHER, T0, extra="o")
    model = w.refresh()
    o = _vid(w.steam, 7)
    cor = Corrections(w.store)
    cor.set_parent(b, a)
    graph = LineageGraph.build([], read_lineage(w.store))
    with pytest.raises(ValueError, match="own parent"):
        cor.set_parent(a, a)
    with pytest.raises(ValueError, match="comes after"):
        cor.set_parent(a, b, graph=graph)
    with pytest.raises(ValueError, match="Combine"):
        cor.set_parent(a, o, line_of=model.line_of)
    with pytest.raises(ValueError):
        cor.set_parent("xyz", a)
    cor.set_parent(a, c, line_of=model.line_of)       # a parent saved later is allowed


def test_a_cycle_is_dropped_and_reported(tmp_path):
    w, (a, b, _) = _three(tmp_path)
    w.refresh()
    cor = _corrections(w.store)
    cor.set_parent(b, a)
    cor.set_parent(a, b)                  # no graph given: the validation is skipped
    graph = LineageGraph.build([], read_lineage(w.store))
    assert dict(graph.parent) == {b: a} and len(graph.dropped) == 1


def test_combine_makes_one_line_and_merges_the_notes(tmp_path):
    w = World(tmp_path)
    steam_save(w.steam, 9, 0, "Main Save", 100, MAIN, T0, extra="a")
    steam_save(w.steam, 4, 0, "Main (copy)", 50, OTHER, T0 - 5, extra="o")
    notes = Notes(w.store)
    notes.set_save_note(PID, "keep going", base_rev=None)
    notes.set_save_note(OTHER_PID, "the copy's words", base_rev=None)
    cor = Corrections(w.store)
    cor.combine([OTHER_PID], PID, names={OTHER_PID: "Main (copy)"})
    model = w.refresh()
    assert [line.pid for line in model.lines()] == [PID]
    note = notes.save_note(PID)
    assert note.rev == 2 and note.history[0].text == "keep going"
    assert note.text == "keep going\n\n--- merged from Main (copy) ---\nthe copy's words"
    with pytest.raises(ValueError, match="already one"):
        cor.combine([OTHER_PID], PID)
    cor.uncombine(OTHER_PID)
    assert {line.pid for line in w.refresh().lines()} == {PID, OTHER_PID}
    assert notes.save_note(PID).rev == 2                 # the merged revision stays
    with pytest.raises(ValueError):
        cor.uncombine(OTHER_PID)


def test_split_seeds_a_new_line_and_carries_the_twin_forward(tmp_path):
    w = World(tmp_path)
    steam_save(w.steam, 9, 0, "Main Save", 100, MAIN, T0, extra="a")
    w.refresh()
    steam_save(w.steam, 9, 0, "Main Save", 200, MAIN, T0 + 10, extra="b")
    model = w.refresh()
    b, a = model.versions(PID)[0].head, model.versions(PID)[1].head
    cor = Corrections(w.store)
    pid = cor.split(PID, [b], "Main Save - permadeath try")
    assert pid == f"split:{PID}:1"
    model = w.refresh()
    assert {line.pid for line in model.lines()} == {PID, pid}
    assert model.line(pid).name == "Main Save - permadeath try"
    assert model.line_of(b) == pid and model.line_of(a) == PID
    assert _row(model, b, pid).parent is None       # the edge a -> b leaves its line: a link
    steam_save(w.steam, 9, 0, "Main Save", 300, MAIN, T0 + 20, extra="c")
    model = w.refresh()                             # the journal carries the split-off twin on
    c = _vid(w.steam, 9)
    assert model.line_of(c) == pid
    assert [r.head for r in model.versions(pid)] == [c, b]
    cor.unsplit(pid)
    assert w.refresh().line_of(b) == PID
    assert cor.split(PID, [a]) == f"split:{PID}:1"
    assert cor.split(PID, [b]) == f"split:{PID}:2"
    with pytest.raises(ValueError):
        cor.split(f"split:{PID}:2", [a])
    with pytest.raises(ValueError):
        cor.unsplit(f"split:{PID}:9")             # no such split
