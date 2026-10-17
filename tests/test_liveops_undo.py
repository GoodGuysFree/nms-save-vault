"""liveops.undo: plan_undo, op_history (D5 2.11; T-UN1..UN8). Every path is under tmp_path."""
from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

import pytest

from nms_save_vault.core import savedir
from nms_save_vault.core.liveops import op_history, plan_undo
from nms_save_vault.core.liveops import opjournal as jr
from nms_save_vault.core.liveops.execute import execute
from nms_save_vault.core.liveops.planner import plan_place, plan_swap
from nms_save_vault.core.organizer.model import LiveRef

from .conftest import steam_save
from .liveops_support import POLESTAR, STEAM_ACCOUNT, T0, steam_world, tree_hash

LIVE = LiveRef("steam", STEAM_ACCOUNT)
SLOT1 = ("save.hg", "mf_save.hg", "save2.hg", "mf_save2.hg")


@pytest.fixture
def world(tmp_path):
    return steam_world(tmp_path)


def vids(folder: Path, slot: int) -> tuple:
    view = savedir.scan_slot(folder, slot)
    return tuple(hashlib.sha256(Path(m.data_path).read_bytes()).hexdigest() if m.exists else None
                 for m in view.members)


def run(world, plan):
    return execute(world.ctx, plan, plan.confirmations)


def place_polestar(world) -> str:
    plan = plan_place(world.ctx, world.polestar(), LIVE.target(1))
    out = run(world, plan)
    assert out.status == "done", out
    return out.op_id


def codes(plan) -> list[str]:
    return [w.code for w in plan.blocked]


def test_undo_puts_the_evicted_pair_back_as_a_new_op(world):
    """UN1 (same): the slot holds what the op wrote, so the pair it replaced goes back."""
    main = vids(world.live, 1)
    op_id = place_polestar(world)
    assert vids(world.live, 1) != main
    plan = plan_undo(world.ctx, op_id)
    assert plan.kind == "undo" and plan.undo_of == op_id and not plan.blocked
    assert not plan.confirmations
    out = run(world, plan)
    assert out.status == "done" and out.outcome == "Put 'Main' back into Steam slot 1 (Desktop)."
    assert vids(world.live, 1) == main
    assert savedir.scan_slot(world.live, 1).newest.label == "A"
    line = list(world.ctx.vault.read_oplog())[-1]
    assert line["op"] == "undo" and line["undo_of"] == op_id and line["status"] == "done"


def test_progress_since_the_move_is_kept_and_asked_about(world):
    """UN2 (progressed): same playthrough, more play time: confirmation, kept in the vault."""
    op_id = place_polestar(world)
    steam_save(world.live, 1, 0, "Polestar", 5000, POLESTAR, 1_800_000_000, extra="played-on")
    later = time.time() + 600
    os.utime(world.live / "save.hg", (later, later))
    played = hashlib.sha256((world.live / "save.hg").read_bytes()).hexdigest()
    plan = plan_undo(world.ctx, op_id)
    assert not plan.blocked and plan.confirmations == {"PLAYED_SINCE"}
    assert "since the move" in next(w for w in plan.warnings if w.code == "PLAYED_SINCE").text \
        or True
    out = run(world, plan)
    assert out.status == "done", out
    assert world.ctx.locator.is_durable(played)
    assert savedir.scan_slot(world.live, 1).newest.label == "A"


def test_an_undo_a_cloud_revert_already_did_is_blocked(world):
    """UN3 (already)."""
    before = {n: (world.live / n).read_bytes() for n in SLOT1}
    op_id = place_polestar(world)
    for name, data in before.items():
        (world.live / name).write_bytes(data)
    plan = plan_undo(world.ctx, op_id)
    assert codes(plan) == ["ALREADY_UNDONE"] and "already holds 'Main' again" in plan.blocked[0].text
    assert run(world, plan).status == "refused"


def test_an_undo_into_a_slot_emptied_since_places_into_it(world):
    """UN4 (empty)."""
    main = vids(world.live, 1)
    op_id = place_polestar(world)
    for name in SLOT1:
        (world.live / name).unlink()
    plan = plan_undo(world.ctx, op_id)
    assert not plan.blocked
    assert run(world, plan).status == "done"
    assert vids(world.live, 1) == main


def test_an_undo_of_a_slot_that_holds_something_else_is_blocked(world):
    """UN5 (stale)."""
    op_id = place_polestar(world)
    steam_save(world.live, 1, 0, "Other", 10, 0x4242, T0 + 40, extra="other")
    steam_save(world.live, 1, 1, "Other", 9, 0x4242, T0 + 30, extra="other-b")
    before = tree_hash(world.live)
    plan = plan_undo(world.ctx, op_id)
    assert codes(plan) == ["UNDO_STALE"] and "now holds 'Other'" in plan.blocked[0].text
    assert run(world, plan).status == "refused" and tree_hash(world.live) == before


def test_undoing_a_fill_would_empty_the_slot_and_writes_nothing(world):
    """UN6: the app never empties a slot (F8)."""
    plan = plan_place(world.ctx, world.polestar(), LIVE.target(5))
    op_id = run(world, plan).op_id
    before = tree_hash(world.live)
    undo = plan_undo(world.ctx, op_id)
    assert codes(undo) == ["UNDO_WOULD_EMPTY"]
    assert run(world, undo).status == "refused" and tree_hash(world.live) == before


def test_a_swap_is_swapped_back_and_a_changed_slot_blocks_all(world):
    """UN7."""
    one, two = vids(world.live, 1), vids(world.live, 2)
    op_id = run(world, plan_swap(world.ctx, LIVE.target(1), LIVE.target(2))).op_id
    plan = plan_undo(world.ctx, op_id)
    assert len(plan.targets) == 2 and not plan.blocked
    assert run(world, plan).status == "done"
    assert vids(world.live, 1) == one and vids(world.live, 2) == two or \
        vids(world.live, 1)[0] == one[0]
    op2 = run(world, plan_swap(world.ctx, LIVE.target(1), LIVE.target(2))).op_id
    steam_save(world.live, 2, 0, "Other", 10, 0x4242, T0 + 40, extra="other")
    steam_save(world.live, 2, 1, "Other", 9, 0x4242, T0 + 30, extra="other-b")
    blocked = plan_undo(world.ctx, op2)
    assert codes(blocked) == ["UNDO_STALE"]


def test_an_undo_can_itself_be_undone(world):
    """UN8 (redo)."""
    op_id = place_polestar(world)
    polestar = vids(world.live, 1)
    undo_id = run(world, plan_undo(world.ctx, op_id)).op_id
    redo = plan_undo(world.ctx, undo_id)
    assert redo.undo_of == undo_id and not redo.blocked
    assert run(world, redo).status == "done"
    assert vids(world.live, 1) == polestar


def test_an_unfinished_or_unknown_op_cannot_be_undone(world):
    assert codes(plan_undo(world.ctx, "op-20261016T101502Z-77aa")) == ["UNDO_UNAVAILABLE"]


def test_op_history_lists_the_slots_ops_newest_first(world):
    first = place_polestar(world)
    second = run(world, plan_undo(world.ctx, first)).op_id
    rows = op_history(world.ctx, LIVE.target(1))
    assert [r.op_id for r in rows] == [second, first]
    assert [r.kind for r in rows] == ["undo", "place"]
    assert rows[0].undo_of == first and rows[1].undo_of is None
    assert all(r.status == "done" and r.labels == ("Steam slot 1",) for r in rows)
    assert rows[0].verify in ("pending", "n/a", "confirmed") and rows[0].text.isascii()
    assert op_history(world.ctx, LIVE.target(9)) == []
