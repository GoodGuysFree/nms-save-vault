"""organizer.provenance (CL1.5): which versions an app made, and the last version before it
changed a slot - over a journal built by hand, and once over a real live operation."""
from __future__ import annotations

import calendar
from types import SimpleNamespace

import pytest

from nms_save_vault.core import api, appinfo
from nms_save_vault.core.liveops.execute import execute
from nms_save_vault.core.liveops.planner import plan_place
from nms_save_vault.core.organizer import provenance as pv
from nms_save_vault.core.organizer.journal import JournalState, OpRecord
from nms_save_vault.core.organizer.model import Edge, LiveRef, LiveTarget

from .liveops_support import STEAM_ACCOUNT, steam_world

SC = "nms-savecraft"
SC_STAMP = "nms-savecraft 0.1.0"
VAULT_STAMP = "nms-save-vault 0.3.6"
SLOT1 = LiveTarget("steam", "st_1", 1)
SLOT2 = LiveTarget("steam", "st_1", 2)


def op(seq, at, placed, evicted, *, app=SC_STAMP, target=SLOT1, status="done", kind="edit",
       op_id=None):
    kept = {v: {"entry": f"kept-{seq}", "vid": v} for v in evicted if v}
    return OpRecord(id=f"r-{seq}", seq=seq, at=at, src="app", op_id=op_id or f"op-{seq}",
                    kind=kind, target=target, pair=placed, evicted=evicted, source=None,
                    preserved=kept, status=status, undo_of=None, note="", app=app)


def played(parent, child, at):
    return Edge(parent, child, "observed", "play", "journal", None, at, SLOT1)


def journal(records, edges=(), pids=None):
    metas = {v: SimpleNamespace(pid=p) for v, p in (pids or {}).items()}
    return JournalState(targets={}, edges=tuple(edges), events=(), events_by_seq={}, ops={},
                        metas=metas, first_seen={}, note_rev={}, problems=(),
                        records=tuple(records))


def test_a_copy_is_not_made_by_the_app_that_moved_it_an_edit_is():
    state = journal([op(1, 200, ("c", None), ("v1", "v0"), kind="place"),
                     op(2, 300, ("e1", None), ("c", None))])
    backup = [(None, 100, ("c",))]                         # a backup held "c" first: a copy
    assert pv.made_by(state, SC, backup) == {"e1"}
    assert pv.first_recorded_by(state, backup)["c"] is None
    assert pv.first_recorded_by(state, backup)["v1"] is None      # evicted: it was there
    assert [c.op_id for c in pv.changes_by(state, SC)] == ["op-1", "op-2"]


def test_an_edit_kept_as_an_entry_before_it_is_placed_is_still_the_editors():
    state = journal([op(1, 400, ("e2", None), ("v1", None))])
    entries = [(SC_STAMP, 400, ("e2",)), (None, 400, ("v1",))]   # an edit entry, a snapshot
    assert pv.made_by(state, SC, entries) == {"e2"}


def test_the_last_version_before_is_the_one_before_the_first_edit_of_a_run():
    """v1 -> edit e1 -> the game plays on to p1 -> edit e2: still v1, never p1 or e1."""
    state = journal([op(1, 100, ("e1", "v1"), ("v1", "v0")),
                     op(2, 300, ("e2", "p1"), ("p1", "e1"))],
                    edges=[played("e1", "p1", 200)])
    assert pv.touched_by(state, SC) == {"e1", "p1", "e2"}
    before = pv.last_before(state, SC, target=SLOT1)
    assert (before.pair, before.change.op_id) == (("v1", "v0"), "op-1")
    assert before.kept == {"v1": {"entry": "kept-1", "vid": "v1"},
                           "v0": {"entry": "kept-1", "vid": "v0"}}


def test_a_clean_version_played_after_going_back_is_the_newer_answer():
    state = journal([op(1, 100, ("e1", "v1"), ("v1", "v0")),
                     op(2, 200, ("v1", None), ("e1", "v1"), app=VAULT_STAMP, kind="undo"),
                     op(3, 400, ("e3", "v2"), ("v2", "v1"))],
                    edges=[played("v1", "v2", 300)])
    assert pv.last_before(state, SC, target=SLOT1).pair == ("v2", "v1")


def test_by_slot_by_playthrough_and_failed_or_repeated_operations():
    state = journal([op(1, 100, ("e1", None), ("a1", None)),
                     op(2, 200, ("e2", None), ("b1", None), target=SLOT2),
                     op(3, 300, ("e3", None), ("a2", None), status="rolled_back"),
                     op(4, 400, ("e4", None), ("a3", None), op_id="op-1")],   # a repeat of op-1
                    pids={"a1": "AAAA", "b1": "BBBB"})
    assert pv.last_before(state, SC, target=SLOT1).pair == ("a1", None)
    assert pv.last_before(state, SC, target=SLOT2).pair == ("b1", None)
    assert pv.last_before(state, SC, pid="BBBB").pair == ("b1", None)
    assert pv.last_before(state, SC, pid="CCCC") is None
    assert pv.last_before(state, SC).pair == ("b1", None)        # the newest of all
    assert [c.op_id for c in pv.changes_by(state, SC)] == ["op-1", "op-2"]


def test_an_app_never_answers_for_another():
    state = journal([op(1, 100, ("x1", None), ("v1", None), app=VAULT_STAMP),
                     op(2, 200, ("x2", None), ("v2", None), app="0.3.5")])   # before stamps named an app
    assert pv.changes_by(state, SC) == [] and pv.last_before(state, SC) is None
    assert {c.op_id for c in pv.changes_by(state, "nms-save-vault")} == {"op-1", "op-2"}


def test_a_copy_entry_or_one_without_a_readable_time_claims_nothing():
    made = SimpleNamespace(kind="edit", created_utc="2026-11-08T10:00:00Z", app_version=SC_STAMP,
                           members=[SimpleNamespace(vid="e1"), SimpleNamespace(vid=None)])
    copy = SimpleNamespace(kind="snapshot", created_utc="2026-11-08T10:00:00Z",
                           app_version=SC_STAMP, members=[SimpleNamespace(vid="v1")])
    bad = SimpleNamespace(kind="edit", created_utc="soon", app_version=SC_STAMP, members=[])
    at = calendar.timegm((2026, 11, 8, 10, 0, 0))
    assert pv.entry_origins([made, copy, bad]) == [(SC_STAMP, at, ("e1",)), (None, at, ("v1",))]


def test_the_api_history_names_a_savecrafts_place_and_what_it_replaced(tmp_path):
    """A real place run as Savecraft: one change by it, nothing made (a copy of a backup), and
    the slot's pair before it, kept in the vault."""
    world = steam_world(tmp_path)
    plan = plan_place(world.ctx, world.polestar(), LiveRef("steam", STEAM_ACCOUNT).target(1))
    previous = appinfo.set_app("nms-savecraft", "0.1.0")
    try:
        outcome = execute(world.ctx, plan, plan.confirmations)
    finally:
        appinfo.set_app(previous.name, previous.version)
    assert outcome.status == "done", outcome
    history = api.open_history(world.vault_root)
    [change] = history.changes_by(SC)
    assert (change.op_id, change.app, change.target.slot) == (outcome.op_id, SC_STAMP, 1)
    assert history.made_by(SC) == set()
    before = history.last_before(SC, slot=api.slot_of(world.live, 1))
    assert before.pair == change.evicted and set(before.kept) == {v for v in before.pair if v}
    preserved = {loc["entry"] if "entry" in loc else str(loc) for loc in before.kept.values()}
    assert preserved                                          # where to get it back from
    assert history.last_before(SC, slot=api.slot_of(world.live, 2)) is None


def test_a_vault_without_a_journal_has_an_empty_history(tmp_path):
    (tmp_path / "vault").mkdir()
    history = api.open_history(tmp_path / "vault")
    assert history.changes_by(SC) == [] and history.made_by(SC) == set()
    with pytest.raises(api.StoreUnavailable):
        api.open_history(tmp_path / "missing")
