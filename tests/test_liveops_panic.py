"""The panic restore (D5 4.3): a whole snapshot back as one live operation, metas verbatim,
slots the snapshot lacks left alone, undoable, recoverable, and the default is this folder's
own newest snapshot (P4)."""
from __future__ import annotations

import pytest

from nms_save_vault.core import meta, savedir
from nms_save_vault.core.liveops import FaultInjector, SimulatedCrash, execute
from nms_save_vault.core.liveops import opjournal as jr
from nms_save_vault.core.liveops.panic import candidates, panic_restore, plan_panic_restore
from nms_save_vault.core.liveops.planner import plan_place
from nms_save_vault.core.liveops.recovery import pending_recovery, resolve_recovery
from nms_save_vault.core.liveops.undo import plan_undo
from nms_save_vault.core.organizer.model import LiveRef

from .conftest import steam_save
from .liveops_support import MAIN, STEAM_ACCOUNT, STEAM_ACCOUNT_2, steam_world, tree_hash

LIVE = LiveRef("steam", STEAM_ACCOUNT)
OTHER = LiveRef("steam", STEAM_ACCOUNT_2)


@pytest.fixture
def world(tmp_path):
    return steam_world(tmp_path)


def _place(world):
    plan = plan_place(world.ctx, world.polestar(), LIVE.target(1))
    out = execute(world.ctx, plan, plan.confirmations)
    assert out.status == "done", out
    world.refresh()
    return out


def test_without_a_snapshot_there_is_nothing_to_restore(world):
    plan = plan_panic_restore(world.ctx, LIVE)
    assert [w.code for w in plan.blocked] == ["NO_SNAPSHOT"]


def test_the_default_is_this_folders_own_newest_snapshot(world):
    _place(world)
    assert candidates(world.ctx, OTHER) == []              # the other account has none (P4)
    (newest, *_rest) = candidates(world.ctx, LIVE)
    plan = plan_panic_restore(world.ctx, LIVE)
    assert not plan.blocked and plan.kind == "panic_restore"
    assert newest.kind == "snapshot" and len(plan.targets) == 2     # slots 1 and 2 were in it


def test_it_puts_the_snapshot_back_with_its_metas_as_recorded(world):
    before = savedir.scan_slot(world.live, 1).members[0]
    before_ts = meta.parse(meta.decrypt((world.live / "mf_save.hg").read_bytes(), 1), 1).timestamp
    _place(world)
    plan, outcome = panic_restore(world.ctx, LIVE)
    assert outcome.status == "done", outcome
    world.refresh()
    after = savedir.scan_slot(world.live, 1).members[0]
    assert after.info.save_name == before.info.save_name == "Main"
    ts = meta.parse(meta.decrypt((world.live / "mf_save.hg").read_bytes(), 1), 1).timestamp
    assert ts == before_ts                                  # not stamped now: a recorded state


def test_slots_the_snapshot_lacks_are_left_alone_and_said(world):
    _place(world)
    steam_save(world.live, 5, 0, "Newer", 10, 0x5555, 1_800_000_000, extra="slot5")
    world.refresh()
    kept = (world.live / "save9.hg").read_bytes()

    plan = plan_panic_restore(world.ctx, LIVE)
    assert any(w.code == "SLOTS_LEFT" and "5" in w.text for w in plan.warnings)
    outcome = execute(world.ctx, plan, plan.confirmations)

    assert outcome.status == "done"
    assert (world.live / "save9.hg").read_bytes() == kept     # F8: nothing emptied or touched


def test_a_panic_restore_is_itself_undoable(world):
    _place(world)
    placed = tree_hash(world.live)["save.hg"][0]
    _plan, outcome = panic_restore(world.ctx, LIVE)
    assert outcome.status == "done" and outcome.undo is not None
    assert tree_hash(world.live)["save.hg"][0] != placed
    world.refresh()

    undo = plan_undo(world.ctx, outcome.op_id)
    assert undo.kind == "panic_restore" and undo.undo_of == outcome.op_id
    back = execute(world.ctx, undo, undo.confirmations)
    assert back.status == "done"
    assert tree_hash(world.live)["save.hg"][0] == placed


def test_a_crash_after_the_check_is_finished_by_recovery_with_the_same_metas(world):
    _place(world)
    plan = plan_panic_restore(world.ctx, LIVE)
    world.ctx.faults = FaultInjector({"after_validated"})
    with pytest.raises(SimulatedCrash):
        execute(world.ctx, plan, plan.confirmations)
    world.ctx.faults = None
    world.refresh()
    (op_id,) = [i for i, r in jr.read_ops(world.ctx).items() if r.kind == "panic_restore"]

    items = pending_recovery(world.ctx)

    # the slots still hold exactly what was written: the op is finished by itself
    assert items == [] and jr.read_ops(world.ctx)[op_id].status == "done"


def test_a_crash_half_way_goes_back_exactly(world):
    _place(world)
    before = tree_hash(world.live)
    plan = plan_panic_restore(world.ctx, LIVE)
    world.ctx.faults = FaultInjector({"swapped:2"})
    with pytest.raises(SimulatedCrash):
        execute(world.ctx, plan, plan.confirmations)
    world.ctx.faults = None
    world.refresh()
    (item,) = pending_recovery(world.ctx)

    res = resolve_recovery(world.ctx, item.op_id, "back")

    assert res.status == "rolled_back" and tree_hash(world.live) == before


def test_another_accounts_backup_can_be_chosen_but_not_a_missing_one(world):
    plan = plan_panic_restore(world.ctx, LIVE, world.backup_id)
    assert not plan.blocked                                  # a backup of the other account: allowed
    plan = plan_panic_restore(world.ctx, LIVE, "no-such-entry")
    assert [w.code for w in plan.blocked] == ["SNAPSHOT_NOT_THIS_FOLDER"]
