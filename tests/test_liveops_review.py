"""liveops: the review of execute, recovery and the planner (D5 2.7-2.9), one test per
finding. Every path is under tmp_path."""
from __future__ import annotations

import hashlib
import sys
from types import SimpleNamespace

import pytest

from nms_save_vault.core import operations
from nms_save_vault.core.liveops import FaultInjector, SimulatedCrash
from nms_save_vault.core.liveops import opjournal as jr
from nms_save_vault.core.liveops import planner, recovery, slots, steamwrite
from nms_save_vault.core.liveops.execute import execute
from nms_save_vault.core.liveops.planner import plan_place, plan_swap
from nms_save_vault.core.liveops.recovery import pending_recovery, resolve_recovery
from nms_save_vault.core.organizer import recorder
from nms_save_vault.core.organizer.model import LiveRef

from .conftest import steam_save
from .liveops_support import STEAM_ACCOUNT, T0, XBOX_ACCOUNT, steam_world, tree_hash

ex = sys.modules["nms_save_vault.core.liveops.execute"]     # the package's name is the function
LIVE = LiveRef("steam", STEAM_ACCOUNT)


@pytest.fixture
def world(tmp_path):
    return steam_world(tmp_path)


def _sha(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def crash(world, plan, point: str) -> str:
    world.ctx.faults = FaultInjector({point})
    with pytest.raises(SimulatedCrash):
        execute(world.ctx, plan, plan.confirmations)
    world.ctx.faults = None
    world.refresh()
    (op_id,) = jr.read_ops(world.ctx)
    return op_id


def status(world, op_id) -> str:
    return jr.read_ops(world.ctx)[op_id].status


def failed_op(world, monkeypatch, action):
    """An op whose rollback failed (status 'failed'); ``action`` runs after the swap."""
    plan = plan_place(world.ctx, world.polestar(), LIVE.target(1))
    real = steamwrite.rollback
    monkeypatch.setattr(steamwrite, "rollback",
                        lambda *a: (_ for _ in ()).throw(OSError(5, "held open")))
    world.ctx.faults = FaultInjector(actions={"swapped:4": action})
    outcome = execute(world.ctx, plan, plan.confirmations)
    assert outcome.code == "ROLLBACK_FAILED"
    monkeypatch.setattr(steamwrite, "rollback", real)
    world.ctx.faults = None
    world.refresh()
    return outcome.op_id


# 1 -----------------------------------------------------------------------------------------


def test_back_is_not_offered_over_new_game_progress_and_leave_keeps_it(world, monkeypatch):
    played = {}

    def progress():
        steam_save(world.live, 1, 0, "Played", 9000, 0x1111, T0 + 900, extra="new-progress")
        played["vid"] = _sha(world.live / "save.hg")

    op_id = failed_op(
        world, monkeypatch, lambda: (world.live / "save.hg").write_bytes(b"broken"))
    progress()                                   # the owner plays after the failure
    world.refresh()
    assert not world.ctx.locator.is_durable(played["vid"])
    assert resolve_recovery(world.ctx, op_id, "back").status == "refused"
    assert _sha(world.live / "save.hg") == played["vid"]          # the game's save stays
    assert resolve_recovery(world.ctx, op_id, "leave").status == "rolled_back"
    assert world.ctx.locator.is_durable(played["vid"])


# 2 -----------------------------------------------------------------------------------------


def test_a_target_the_game_wrote_is_not_rolled_back_with_the_others(world):
    """The second slot of a swap changes under the op: the first is put back, the second
    keeps what the game wrote."""
    before = tree_hash(world.live)
    seen = []

    def game_writes():
        seen.append(1)
        if len(seen) == 1:                    # after the first target's last swap
            (world.live / "save3.hg").write_bytes(b"the game saved here")

    plan = plan_swap(world.ctx, LIVE.target(1), LIVE.target(2))
    world.ctx.faults = FaultInjector(actions={"swapped:4": game_writes})
    outcome = execute(world.ctx, plan, plan.confirmations)
    assert outcome.status == "rolled_back", outcome
    assert (world.live / "save3.hg").read_bytes() == b"the game saved here"
    for name in ("save.hg", "mf_save.hg", "save2.hg", "mf_save2.hg"):
        assert tree_hash(world.live)[name] == before[name]


# 3 -----------------------------------------------------------------------------------------


def test_a_member_that_appeared_after_the_snapshot_abandons_the_op(world):
    def appears():
        steam_save(world.live, 2, 1, "Late", 100, 0x2222, T0 + 20, extra="late")

    plan = plan_place(world.ctx, world.polestar(), LIVE.target(2))
    world.ctx.faults = FaultInjector(actions={"after_snapshot": appears})
    outcome = execute(world.ctx, plan, plan.confirmations)
    assert outcome.status == "stale"
    assert (world.live / "save4.hg").is_file()
    (rec,) = jr.read_ops(world.ctx).values()
    assert rec.status == "abandoned"


# 4 -----------------------------------------------------------------------------------------


def test_recovery_with_an_unknown_game_state_asks_for_a_confirmation(world):
    before = tree_hash(world.live)
    plan = plan_place(world.ctx, world.polestar(), LIVE.target(1))
    op_id = crash(world, plan, "swapped:2")
    world.ctx.game_probe = lambda: None
    out = resolve_recovery(world.ctx, op_id, "back")
    assert out.status == "refused" and out.code == "GAME_STATE_UNKNOWN"
    out = resolve_recovery(world.ctx, op_id, "back", {"GAME_STATE_UNKNOWN"})
    assert out.status == "rolled_back" and tree_hash(world.live) == before


# 5 -----------------------------------------------------------------------------------------


def test_the_whole_folder_undo_restores_the_snapshot_the_live_operation_took(world):
    before = tree_hash(world.live)
    plan = plan_place(world.ctx, world.polestar(), LIVE.target(1))
    out = execute(world.ctx, plan, plan.confirmations)
    assert out.status == "done"
    snapshot = jr.read_ops(world.ctx)[out.op_id].last("writing")["targets"][0]["snapshot_id"]

    assert operations.plan_undo(world.ctx.vault, world.live).snapshot_id == snapshot
    operations.undo_last(world.ctx.vault, world.live, allow_game_running=True)

    assert tree_hash(world.live)["save.hg"][0] == before["save.hg"][0]   # slot 1 is Main again


# 6 -----------------------------------------------------------------------------------------


def test_a_validated_op_is_only_finished_while_the_slot_is_still_what_was_written(world):
    plan = plan_place(world.ctx, world.polestar(), LIVE.target(1))
    op_id = crash(world, plan, "after_validated")
    (world.live / "save.hg").write_bytes(b"played on after the crash")
    (item,) = pending_recovery(world.ctx)
    assert item.state == "superseded" and item.choices == ("leave",)
    assert status(world, op_id) == "validated"


# 7 -----------------------------------------------------------------------------------------


def test_an_op_whose_cleanup_failed_stays_open(world, monkeypatch):
    plan = plan_place(world.ctx, world.polestar(), LIVE.target(1))
    op_id = crash(world, plan, "after_writing")
    monkeypatch.setattr(recovery, "rollback_target",
                        lambda *a: (_ for _ in ()).throw(OSError(5, "held open")))
    (item,) = pending_recovery(world.ctx)
    assert item.op_id == op_id and item.state == "failed"
    assert status(world, op_id) == "writing"


# 8 -----------------------------------------------------------------------------------------


def test_a_failed_finalize_still_reports_success(world, monkeypatch):
    plan = plan_place(world.ctx, world.polestar(), LIVE.target(1))
    monkeypatch.setattr(ex, "finalize", lambda wt: (_ for _ in ()).throw(OSError(5, "busy")))
    out = execute(world.ctx, plan, plan.confirmations)
    assert out.status == "done" and [w.code for w in out.warnings] == ["RECORDING_PENDING"]
    (op_id,) = jr.read_ops(world.ctx)
    assert status(world, op_id) == "validated"
    assert pending_recovery(world.ctx) == [] and status(world, op_id) == "done"


def test_a_failed_done_event_still_reports_success(world, monkeypatch):
    real = jr.append

    def append(ctx, op_id, event, payload=None):
        if event == "done":
            raise OSError(5, "journal busy")
        return real(ctx, op_id, event, payload)

    plan = plan_place(world.ctx, world.polestar(), LIVE.target(1))
    monkeypatch.setattr(jr, "append", append)
    out = execute(world.ctx, plan, plan.confirmations)
    assert out.status == "done" and [w.code for w in out.warnings] == ["RECORDING_PENDING"]
    monkeypatch.setattr(jr, "append", real)
    (op_id,) = jr.read_ops(world.ctx)
    assert pending_recovery(world.ctx) == [] and status(world, op_id) == "done"


# 9 -----------------------------------------------------------------------------------------


def test_nothing_written_means_no_snapshot_read_and_no_rollback_failure(world, monkeypatch):
    from nms_save_vault.core.liveops.errors import WriteError

    before = tree_hash(world.live)
    plan = plan_place(world.ctx, world.polestar(), LIVE.target(1))
    monkeypatch.setattr(steamwrite, "stage",
                        lambda *a: (_ for _ in ()).throw(WriteError("disk full")))
    reads = []
    real = world.ctx.versions.read_file
    monkeypatch.setattr(world.ctx.versions, "read_file",
                        lambda *a: reads.append(a) or real(*a))
    out = execute(world.ctx, plan, plan.confirmations)
    assert out.status == "rolled_back" and out.code == "ROLLED_BACK"
    assert reads == [] and tree_hash(world.live) == before


# 10 ----------------------------------------------------------------------------------------


def test_the_apps_own_last_write_is_not_a_sync_in_progress(world):
    now = 1_792_500_000.0
    world.ctx.clock = lambda: now
    ref = LiveRef("xbox", XBOX_ACCOUNT)
    fake = SimpleNamespace(index_mtime=now - 1, account=SimpleNamespace(account=ref))
    assert planner._sync_busy(world.ctx, fake)                    # nobody of ours wrote it
    jr.append(world.ctx, "op-20261016T101502Z-77aa", "preparing",
              {"kind": "place", "targets": [jr.target_json(ref.target(1))]})
    assert not planner._sync_busy(world.ctx, fake)                # ours, just now
    later = SimpleNamespace(index_mtime=now + 30, account=fake.account)
    world.ctx.clock = lambda: now + 31
    assert planner._sync_busy(world.ctx, later)                   # something wrote it since


# extras ------------------------------------------------------------------------------------


def test_a_prepare_failure_closes_the_op_as_abandoned(world, monkeypatch):
    before = tree_hash(world.live)
    plan = plan_place(world.ctx, world.polestar(), LIVE.target(1))
    monkeypatch.setattr(ex, "_snapshot", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    out = execute(world.ctx, plan, plan.confirmations)
    assert out.status == "refused" and out.code == "PREPARE_FAILED"
    (rec,) = jr.read_ops(world.ctx).values()
    assert rec.status == "abandoned" and tree_hash(world.live) == before


def test_an_unreadable_journal_is_unknown_not_nothing_pending(world, monkeypatch):
    plan = plan_place(world.ctx, world.polestar(), LIVE.target(1))
    monkeypatch.setattr(jr, "open_ops", lambda ctx: (_ for _ in ()).throw(OSError(5, "no")))
    assert planner.recovery_pending(world.ctx, [LIVE]).code == "RECOVERY_UNKNOWN"
    out = execute(world.ctx, plan, plan.confirmations)
    assert out.status == "refused" and out.code == "RECOVERY_UNKNOWN"


def test_an_op_reads_each_account_at_most_twice(world, monkeypatch):
    calls = []
    real = ex.read_account
    monkeypatch.setattr(ex, "read_account", lambda *a: calls.append(1) or real(*a))
    plan = plan_place(world.ctx, world.polestar(), LIVE.target(1))
    assert execute(world.ctx, plan, plan.confirmations).status == "done"
    assert len(calls) <= 2


def test_the_scanner_renaming_is_shared():
    assert slots._renamed is recorder._renamed
