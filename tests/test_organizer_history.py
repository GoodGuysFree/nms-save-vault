"""organizer.history: the HistoryGraph - tree or dated, folding, key versions, lanes, links
(D1 2.10.4, 2.10.5)."""
from __future__ import annotations

import json

import pytest

from nms_save_vault.core import operations, safety
from nms_save_vault.core.organizer.history import format_history
from nms_save_vault.core.organizer.lineage import Corrections

from .conftest import steam_save
from .test_organizer_readmodel import MAIN, OTHER, PID, T0, World, _vid


@pytest.fixture(autouse=True)
def game_not_running(monkeypatch):
    monkeypatch.setattr(safety, "is_game_running", lambda: False)


def _corrections(store):
    ticks = iter(range(T0, T0 + 99))
    return Corrections(store, clock=lambda: next(ticks))


def _forked(tmp_path):
    """a -> b -> d, a -> c (a fork at a), and e that nothing links. All by the owner's word."""
    w = World(tmp_path)
    plays = {9: 100, 3: 200, 5: 300, 7: 400, 11: 50}
    for slot, play in plays.items():
        steam_save(w.steam, slot, 0, "Main Save", play, MAIN, T0 + play, extra=f"s{slot}")
    a, b, c, d, e = (_vid(w.steam, s) for s in (9, 3, 5, 7, 11))
    cor = _corrections(w.store)
    cor.set_parent(b, a)
    cor.set_parent(c, a)
    cor.set_parent(d, b)
    return w, (a, b, c, d, e), cor


def test_a_fork_takes_two_lanes_and_the_unlinked_version_is_dated(tmp_path):
    w, (a, b, c, d, e), _ = _forked(tmp_path)
    graph = w.refresh().history(PID, "all")
    # newest first: d(400) b(200)... by play time the order is d, c, b, a
    assert [n.vid for n in graph.nodes] == [d, c, b, a]
    assert [(n.row, n.lane) for n in graph.nodes] == [(0, 0), (1, 1), (2, 0), (3, 0)]
    assert graph.lanes == 2
    assert graph.pass_through == ((), (0,), (1,), ())
    assert {(x.parent, x.child) for x in graph.edges} == {(a, b), (a, c), (b, d)}
    assert all(x.tier == "owner" and x.method == "owner" and not x.cross_platform
               for x in graph.edges)
    assert [r.head for r in graph.dated] == [e]            # no recorded link: listed by date
    assert "fork" in graph.nodes[3].flags and "root" in graph.nodes[3].flags
    assert "tip" in graph.nodes[0].flags and "tip" in graph.nodes[1].flags


def test_key_mode_collapses_the_middle_and_counts_what_it_hid(tmp_path):
    w, (a, b, c, d, e), cor = _forked(tmp_path)
    steam_save(w.steam, 13, 0, "Main Save", 500, MAIN, T0 + 500, extra="s13")
    f = _vid(w.steam, 13)
    cor.set_parent(f, d)                                    # a -> b -> d -> f, and a -> c
    operations.create_full_backup(w.vault, w.steam, warnings=[])
    for slot in (9, 3, 5, 7, 11, 13):                       # nothing is live any more
        steam_save(w.steam, slot, 0, "Other", 1, OTHER, T0, extra=f"o{slot}")
    model = w.refresh()
    allg, key = model.history(PID, "all"), model.history(PID, "key")
    assert len(allg.nodes) == 5
    assert {n.vid for n in key.nodes} == {a, c, f}          # root and fork, and the two tips
    into_f = next(x for x in key.edges if x.child == f)
    assert (into_f.parent, into_f.hidden) == (a, 2)         # b and d are plain steps
    assert next(x for x in key.edges if x.child == c).hidden == 0
    assert [(n.row, n.lane) for n in key.nodes] == [(0, 0), (1, 1), (2, 0)]
    assert [r.head for r in key.dated] == [e]
    assert model.versions(PID)[-1].key                      # the oldest dated row is key


def test_a_resave_without_play_folds_in_default_mode_only(tmp_path):
    w = World(tmp_path)
    steam_save(w.steam, 9, 0, "Main Save", 100, MAIN, T0, extra="a")
    w.refresh()
    steam_save(w.steam, 9, 0, "Main Save", 100, MAIN, T0 + 10, extra="b")    # touch
    w.refresh()
    steam_save(w.steam, 9, 0, "Main Save", 200, MAIN, T0 + 20, extra="c")
    model = w.refresh()
    default, every = model.history(PID), model.history(PID, "all")
    assert len(every.nodes) == 3 and len(default.nodes) == 2
    root = default.nodes[-1]
    assert dict(root.folded) == {"touch": 1}
    assert default.edges[0].hidden == 1 and every.edges[0].hidden == 0
    assert all(x.method == "journal" for x in every.edges)


def test_a_fork_point_that_is_no_head_is_a_node(tmp_path):
    w = World(tmp_path)
    steam_save(w.steam, 9, 0, "Main Save", 200, MAIN, T0 + 200, extra="head")
    steam_save(w.steam, 9, 1, "Main Save", 100, MAIN, T0 + 100, extra="partner")
    steam_save(w.steam, 3, 0, "Main Save", 300, MAIN, T0 + 300, extra="x")
    steam_save(w.steam, 5, 0, "Main Save", 400, MAIN, T0 + 400, extra="y")
    partner, x, y = _vid(w.steam, 9, 1), _vid(w.steam, 3), _vid(w.steam, 5)
    cor = _corrections(w.store)
    cor.set_parent(x, partner)
    cor.set_parent(y, partner)
    graph = w.refresh().history(PID, "key")
    kinds = {n.vid: n.kind for n in graph.nodes}
    assert kinds == {y: "version", x: "version", partner: "fork-member"}
    assert graph.nodes[-1].flags == frozenset({"fork"}) and graph.lanes == 2


def test_a_split_leaves_a_link_instead_of_a_branch(tmp_path):
    w, (a, b, c, d, e), cor = _forked(tmp_path)
    pid = cor.split(PID, [c], "the other try")
    model = w.refresh()
    main, other = model.history(PID, "all"), model.history(pid, "all")
    assert c not in {n.vid for n in main.nodes} and main.lanes == 1
    assert [(k.vid, k.other_pid, k.other_vid) for k in main.links] == [(a, pid, c)]
    assert [(k.vid, k.other_pid, k.other_vid) for k in other.links] == [(c, PID, a)]
    assert [n.vid for n in other.nodes] == [c]              # its own tree: one root
    assert other.edges == ()


def test_modes_are_checked_and_the_graph_is_json_safe(tmp_path):
    w, _, _ = _forked(tmp_path)
    model = w.refresh()
    with pytest.raises(ValueError):
        model.history(PID, "bogus")
    assert model.history("DDDAD1BDD39F9C73", "all").nodes == ()
    text = json.dumps(model.to_dict(model.history(PID, "all")))
    assert '"lanes": 2' in text


def test_the_ascii_picture_matches_the_lanes(tmp_path):
    w, (a, b, c, d, e), _ = _forked(tmp_path)
    graph = w.refresh().history(PID, "all")
    lines = format_history(graph, "Main Save", live={d: ["S7"]})
    assert lines[0] == 'Main Save, mode "all": 4 linked versions, 2 lanes; 1 listed by date'
    assert [ln[:4] for ln in lines[1:5]] == ["*   ", "| * ", "* | ", "*/  "]
    assert "live S7, tip" in lines[1] and "fork, root" in lines[4]
    assert lines[5].strip().startswith("no recorded link") and lines[6].startswith("-")


def test_a_fork_point_belongs_to_its_own_save_file_and_is_drawn_once(tmp_path):
    w = World(tmp_path)
    steam_save(w.steam, 9, 0, "Main Save", 200, MAIN, T0 + 200, extra="head")
    steam_save(w.steam, 9, 1, "Main Save", 100, MAIN, T0 + 100, extra="partner")
    steam_save(w.steam, 3, 0, "Main Save", 300, MAIN, T0 + 300, extra="x")
    steam_save(w.steam, 5, 0, "Main Save", 400, MAIN, T0 + 400, extra="y")
    partner, x, y = _vid(w.steam, 9, 1), _vid(w.steam, 3), _vid(w.steam, 5)
    head = _vid(w.steam, 9, 0)
    cor = _corrections(w.store)
    cor.set_parent(x, partner)
    cor.set_parent(y, partner)
    pid = cor.split(PID, [partner], "root try")
    model = w.refresh()
    main, other = model.history(PID, "all"), model.history(pid, "all")
    assert model.line_of(partner) == pid and model.line_of(head) == PID
    assert partner in {n.vid for n in other.nodes} and partner not in {n.vid for n in main.nodes}
    assert {n.vid: n.kind for n in other.nodes} == {partner: "fork-member", x: "version",
                                                    y: "version"}    # the twins follow it
    drawn = [(e.parent, e.child) for g in (main, other) for e in g.edges]
    assert sorted(drawn) == sorted([(partner, x), (partner, y)])      # each edge drawn once
    assert main.nodes == () and main.links == () and [r.head for r in main.dated] == [head]


def test_a_lagging_copy_is_marked_on_a_tree_node_too(tmp_path):
    w, (a, b, c, d, e), _ = _forked(tmp_path)
    lines = format_history(w.refresh().history(PID, "all"), "Main Save", behind={d: 3660})
    assert "copy 1h01 behind" in next(ln for ln in lines if d[:8] in ln)
