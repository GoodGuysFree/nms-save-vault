"""liveops.edit (CL5): an edit of a live Steam save, kept in the vault as an extract and placed
like any version - preserve, snapshot, journal, records naming the app and what it made, and
an undo that puts the saves back byte for byte (their metas too, but for the save time every
write sets)."""
from __future__ import annotations

import pytest

from nms_save_vault.core import api, appinfo, savejson
from nms_save_vault.core.hashname import key as K
from nms_save_vault.core.liveops import plan_undo
from nms_save_vault.core.liveops.edit import EditRefused, plan_edit
from nms_save_vault.core.liveops.execute import execute
from nms_save_vault.core.organizer.model import LiveRef
from nms_save_vault.core.savepatch import AddMember, PatchRefused

from .conftest import steam_save
from .liveops_support import MAIN, STEAM_ACCOUNT, T0, steam_world, tree_hash

LIVE = LiveRef("steam", STEAM_ACCOUNT)
SLOT1 = ("save.hg", "mf_save.hg", "save2.hg", "mf_save2.hg")
SC = "nms-savecraft"


@pytest.fixture
def savecraft():
    previous = appinfo.set_app("nms-savecraft", "0.1.0")
    try:
        yield
    finally:
        appinfo.set_app(previous.name, previous.version)


def world_with_real_saves(tmp_path):
    """The test world, with slot 1's saves written as the game writes them: JSON and a NUL."""
    world = steam_world(tmp_path)
    for member, play, ts in ((0, 7200, T0 + 100), (1, 7100, T0 + 50)):
        body = b'{"' + K("SaveName").encode() + b'":"Main","m":%d}' % member
        steam_save(world.live, 1, member, "Main", play, MAIN, ts, payload=body + b"\x00")
    world.refresh()
    return world


def _edit(world, what="add units"):
    plan = plan_edit(world.ctx, LIVE.target(1), [AddMember((), "Units", b"4242")], what=what)
    return plan, execute(world.ctx, plan, plan.confirmations)


def test_an_edit_goes_live_through_the_vault_and_names_what_it_made(tmp_path, savecraft):
    world = world_with_real_saves(tmp_path)
    before = {n: (world.live / n).read_bytes() for n in SLOT1}
    plan, outcome = _edit(world)
    assert outcome.status == "done", outcome
    (new_vid,) = plan.made
    root = savejson.decode_save((world.live / "save.hg").read_bytes()).root
    assert root[K("Units")] == 4242

    store = world.ctx.versions
    staged = [e for e in store.entries() if e.kind == "extract" and e.reason == "edit"]
    assert len(staged) == 1 and staged[0].label == "edited by nms-savecraft: add units"
    assert staged[0].origin["made_by"] == "nms-savecraft 0.1.0"
    assert staged[0].app_version == "nms-savecraft 0.1.0"
    assert new_vid in {m.vid for m in staged[0].members}

    ops = [r for r in world.ctx.store.journal_records() if r["t"] == "op"]
    assert [(r["op_id"], r.get("made"), r["app"]) for r in ops] == [
        (outcome.op_id, [new_vid], "nms-savecraft 0.1.0")]
    history = api.open_history(world.vault_root)
    assert history.made_by(SC) == {new_vid}
    old = history.last_before(SC, slot=api.slot_of(world.live, 1))
    assert old is not None and old.change.op_id == outcome.op_id
    assert old.pair[0] == __import__("hashlib").sha256(before["save.hg"]).hexdigest()

    undo = plan_undo(world.ctx, outcome.op_id)
    assert execute(world.ctx, undo, undo.confirmations).status == "done"
    assert {n: (world.live / n).read_bytes() for n in ("save.hg", "save2.hg")} == {
        n: before[n] for n in ("save.hg", "save2.hg")}


def test_nothing_is_written_when_an_edit_cannot_be_made(tmp_path, savecraft):
    world = world_with_real_saves(tmp_path)
    live_before, entries_before = tree_hash(world.live), len(world.ctx.versions.entries())
    with pytest.raises(PatchRefused):
        plan_edit(world.ctx, LIVE.target(1), [AddMember(("Nope",), "Units", b"1")], what="x")
    with pytest.raises(EditRefused, match="change nothing"):
        plan_edit(world.ctx, LIVE.target(1), [], what="x")
    assert tree_hash(world.live) == live_before
    assert len(world.ctx.versions.entries()) == entries_before


def test_an_empty_slot_and_an_xbox_slot_are_refused(tmp_path):
    world = steam_world(tmp_path)
    with pytest.raises(EditRefused, match="no save"):
        plan_edit(world.ctx, LIVE.target(9), [AddMember((), "Units", b"1")], what="x")
    with pytest.raises(EditRefused, match="Steam"):
        plan_edit(world.ctx, LiveRef("xbox", "x").target(1), [], what="x")


def test_a_crashed_edit_finished_by_recovery_still_names_what_it_made(tmp_path, savecraft):
    from nms_save_vault.core.liveops import FaultInjector, SimulatedCrash
    from nms_save_vault.core.liveops.recovery import pending_recovery

    world = world_with_real_saves(tmp_path)
    plan = plan_edit(world.ctx, LIVE.target(1), [AddMember((), "Units", b"7")], what="x")
    world.ctx.faults = FaultInjector({"after_validated"})
    with pytest.raises(SimulatedCrash):
        execute(world.ctx, plan, plan.confirmations)
    world.ctx.faults = None
    world.refresh()
    pending_recovery(world.ctx)
    ops = [r for r in world.ctx.store.journal_records() if r["t"] == "op"]
    assert [r.get("made") for r in ops] == [list(plan.made)]
