"""organizer.provenance (CL1.5): which versions an app made, and the last version before it
changed a playthrough - over journals built by hand, and over real live operations."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from nms_save_vault.core import api, appinfo
from nms_save_vault.core.liveops import FaultInjector, SimulatedCrash
from nms_save_vault.core.liveops import opjournal as jr
from nms_save_vault.core.liveops.execute import execute
from nms_save_vault.core.liveops.planner import plan_place
from nms_save_vault.core.liveops.recovery import pending_recovery
from nms_save_vault.core.organizer import provenance as pv
from nms_save_vault.core.organizer.journal import JournalState, ObsRecord, OpRecord, op_fields
from nms_save_vault.core.organizer.model import Edge, LiveRef, LiveTarget

from .liveops_support import STEAM_ACCOUNT, steam_world

SC = "nms-savecraft"
SC_STAMP = "nms-savecraft 0.1.0"
VAULT_STAMP = "nms-save-vault 0.3.6"
SLOT1 = LiveTarget("steam", "st_1", 1)
SLOT2 = LiveTarget("steam", "st_1", 2)
LIVE = LiveRef("steam", STEAM_ACCOUNT)


def op(seq, placed, evicted, *, made=(), app=SC_STAMP, target=SLOT1, status="done",
       kind="place", op_id=None):
    kept = {v: {"kind": "store", "entry": f"kept-{seq}", "slot": 1, "member": "A"}
            for v in evicted if v}
    return OpRecord(id=f"r-{seq}", seq=seq, at=100 * seq, src="app", op_id=op_id or f"op-{seq}",
                    kind=kind, target=target, pair=placed, evicted=evicted, source=None,
                    preserved=kept, status=status, undo_of=None, note="", app=app,
                    made=tuple(made))


def seen(seq, pair, target=SLOT1):
    return ObsRecord(id=f"r-{seq}", seq=seq, at=100 * seq, src="scan", target=target, pair=pair,
                     members={}, note_revs={}, entry=None, quality="exact")


def played(parent, child, at):
    return Edge(parent, child, "observed", "play", "journal", None, at, SLOT1)


def journal(records, edges=(), pids=None):
    first: dict = {}
    for rec in records:                      # as replay does: the first record holding a vid
        for vid in rec.pair:
            if vid and vid not in first:
                first[vid] = rec.order
    metas = {v: SimpleNamespace(pid=p) for v, p in (pids or {}).items()}
    return JournalState(targets={}, edges=tuple(edges), events=(), events_by_seq={}, ops={},
                        metas=metas, first_seen=first, note_rev={}, problems=(),
                        records=tuple(records))


def test_only_what_an_operation_names_as_made_is_made():
    state = journal([op(1, ("c", None), ("v1", "v0")),                  # a copy
                     op(2, ("e1", None), ("c", None), made=("e1",))])   # an edit
    assert pv.made_by(state, SC) == {"e1"}
    assert [c.op_id for c in pv.changes_by(state, SC)] == ["op-1", "op-2"]


def test_a_live_save_copied_in_and_played_on_is_still_a_good_version():
    """The game's own save placed by the editor is no edit: once played on and then edited,
    the pair just before the edit is the answer, of the playthrough in the slot."""
    state = journal([op(1, ("pol", None), ("m1", "m0")),
                     seen(2, ("p2", "pol")),
                     op(3, ("x", "p2"), ("p2", "pol"), made=("x",))],
                    pids={"pol": "POL", "p2": "POL", "x": "POL", "m1": "MAIN", "m0": "MAIN"})
    assert pv.made_by(state, SC) == {"x"}
    assert pv.last_before(state, SC, target=SLOT1).pair == ("p2", "pol")
    assert pv.last_before(state, SC, pid="MAIN").pair == ("m1", "m0")


def test_the_last_version_before_is_the_one_before_the_first_edit_of_a_run():
    """v1 -> edit e1 -> the game plays on to g1 -> edit e2: still v1, never g1 or e1."""
    state = journal([op(1, ("e1", "v1"), ("v1", "v0"), made=("e1",)),
                     seen(2, ("g1", "e1")),
                     op(3, ("e2", "g1"), ("g1", "e1"), made=("e2",))],
                    pids={v: "P" for v in ("v0", "v1", "e1", "g1", "e2")})
    assert pv.touched_by(state, SC) == {"e1", "g1", "e2"}        # g1 by the window, no edge
    before = pv.last_before(state, SC, target=SLOT1)
    assert (before.pair, before.change.op_id) == (("v1", "v0"), "op-1")
    assert before.kept["v1"] == pv.KeptCopy("store", "kept-1", 1, "A")


def test_a_descent_edge_touches_a_save_seen_only_elsewhere():
    state = journal([op(1, ("e1", None), ("v1", None), made=("e1",))],
                    edges=[played("e1", "g9", 150)])
    assert pv.touched_by(state, SC) == {"e1", "g9"}


def test_a_clean_version_played_after_going_back_is_the_newer_answer():
    state = journal([op(1, ("e1", "v1"), ("v1", "v0"), made=("e1",)),
                     op(2, ("v1", None), ("e1", "v1"), app=VAULT_STAMP, kind="undo"),
                     seen(3, ("v2", "v1")),
                     op(4, ("e3", "v2"), ("v2", "v1"), made=("e3",))],
                    pids={v: "P" for v in ("v0", "v1", "v2", "e1", "e3")})
    assert pv.last_before(state, SC, target=SLOT1).pair == ("v2", "v1")


def test_a_slot_is_asked_for_the_playthrough_it_holds_after_a_swap():
    """Edit Q in slot 2, edit P in slot 1, the slots are swapped, P is edited again in slot 2:
    slot 2 now holds P, so its answer is P's p0, not Q's q0."""
    vault = dict(app=VAULT_STAMP, kind="swap")
    state = journal([op(1, ("q1", None), ("q0", None), made=("q1",), target=SLOT2),
                     op(2, ("p1", None), ("p0", None), made=("p1",), target=SLOT1),
                     op(3, ("q1", None), ("p1", None), target=SLOT1, **vault),
                     op(4, ("p1", None), ("q1", None), target=SLOT2, **vault),
                     op(5, ("p2", None), ("p1", None), made=("p2",), target=SLOT2)],
                    pids={"q0": "Q", "q1": "Q", "p0": "P", "p1": "P", "p2": "P"})
    assert pv.last_before(state, SC, target=SLOT2).pair == ("p0", None)
    assert pv.last_before(state, SC, target=SLOT1).pair == ("q0", None)
    assert pv.last_before(state, SC, pid="P").pair == ("p0", None)


def test_failed_and_repeated_operations_and_other_apps_answer_nothing():
    state = journal([op(1, ("e1", None), ("a1", None), made=("e1",)),
                     op(2, ("e3", None), ("a2", None), made=("e3",), status="rolled_back"),
                     op(3, ("e4", None), ("a3", None), made=("e4",), op_id="op-1"),
                     op(4, ("x1", None), ("b1", None), app=VAULT_STAMP),
                     op(5, ("x2", None), ("b2", None), app="0.3.5")],     # before stamps named one
                    pids={"a1": "A"})
    assert [c.op_id for c in pv.changes_by(state, SC)] == ["op-1"]
    assert pv.made_by(state, SC) == {"e1"}
    assert {c.op_id for c in pv.changes_by(state, "nms-save-vault")} == {"op-4", "op-5"}
    assert pv.last_before(state, SC, pid="A").pair == ("a1", None)
    assert pv.last_before(state, SC, pid="B") is None


def test_a_slot_of_unknown_playthrough_answers_for_itself_alone():
    state = journal([op(1, ("e1", None), ("a1", None), made=("e1",), target=SLOT1),
                     op(2, ("e2", None), ("b1", None), made=("e2",), target=SLOT2)])
    assert pv.last_before(state, SC, target=SLOT1).pair == ("a1", None)
    assert pv.last_before(state, SC).pair == ("b1", None)                   # the newest of all


def test_slots_with_an_instance_tag_match_only_their_own():
    tagged = LiveTarget("steam", "st_1", 1, "aaaa1111")
    assert pv.same_slot(tagged, SLOT1) and pv.same_slot(SLOT1, tagged)
    assert not pv.same_slot(tagged, LiveTarget("steam", "st_1", 1, "bbbb2222"))
    assert pv.KeptCopy.from_json({"kind": "store"}) is None


def test_made_travels_in_the_record_and_only_when_there_is_some():
    with_made = op_fields("app", "op-1", "place", SLOT1, ("e" * 64, None), None,
                          made=("e" * 64,))
    assert with_made["made"] == ["e" * 64]
    assert "made" not in op_fields("app", "op-1", "place", SLOT1, ("e" * 64, None), None)


def _place_as_savecraft(world, *, crash_at=None):
    plan = plan_place(world.ctx, world.polestar(), LIVE.target(1))
    previous = appinfo.set_app("nms-savecraft", "0.1.0")
    try:
        if crash_at is None:
            return execute(world.ctx, plan, plan.confirmations).op_id
        world.ctx.faults = FaultInjector({crash_at})
        with pytest.raises(SimulatedCrash):
            execute(world.ctx, plan, plan.confirmations)
    finally:
        appinfo.set_app(previous.name, previous.version)
    world.ctx.faults = None
    (op_id,) = jr.read_ops(world.ctx)
    return op_id


def test_the_api_history_names_a_savecraft_copy_and_where_the_replaced_pair_was_kept(tmp_path):
    world = steam_world(tmp_path)
    op_id = _place_as_savecraft(world)
    history = api.open_history(world.vault_root)
    [change] = history.changes_by(SC)
    assert (change.op_id, change.app, change.target.slot, change.made) == (op_id, SC_STAMP, 1, ())
    assert history.made_by(SC) == set()                           # a copy of a backup
    before = history.last_before(SC, slot=api.slot_of(world.live, 1))
    assert before is not None and before.pair == change.evicted
    store = world.ctx.versions
    for vid, kept in before.kept.items():                         # each copy reads back
        assert kept.kind == "store"
        assert store.entry(kept.entry).member(kept.slot, kept.member).vid == vid
    assert history.last_before(SC, slot=api.slot_of(world.live, 2)) is None


@pytest.mark.parametrize("point", ["after_validated", "after_oplog", "after_record"])
def test_an_operation_finished_by_recovery_in_another_app_names_the_app_that_ran_it(tmp_path,
                                                                                    point):
    world = steam_world(tmp_path)
    op_id = _place_as_savecraft(world, crash_at=point)
    world.refresh()                       # NMS Save Vault starts next and finishes it
    pending_recovery(world.ctx)
    line = [r for r in world.ctx.vault.read_oplog() if r.get("op_id") == op_id]
    assert [r["app"] for r in line] == [SC_STAMP]
    records = [r for r in world.ctx.store.journal_records() if r.get("op_id") == op_id]
    assert [r["app"] for r in records] == [SC_STAMP]
    assert [c.op_id for c in api.open_history(world.vault_root).changes_by(SC)] == [op_id]


def test_a_vault_without_a_journal_has_an_empty_history(tmp_path):
    (tmp_path / "vault").mkdir()
    history = api.open_history(tmp_path / "vault")
    assert history.changes_by(SC) == [] and history.made_by(SC) == set()
    with pytest.raises(api.StoreUnavailable):
        api.open_history(tmp_path / "missing")
