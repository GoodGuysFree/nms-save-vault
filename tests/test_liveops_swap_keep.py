"""liveops: swap, keep a copy and the empty-slot steps (D5 2.4, 2.4.1, 2.4.2; T-F3, T-K1,
T-E1)."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from nms_save_vault.core import savedir
from nms_save_vault.core.liveops import opjournal as jr
from nms_save_vault.core.liveops.execute import execute
from nms_save_vault.core.liveops.guide import empty_slot_guide
from nms_save_vault.core.liveops.planner import plan_keep, plan_place, plan_swap
from nms_save_vault.core.organizer.model import LiveRef

from .liveops_support import STEAM_ACCOUNT, STEAM_ACCOUNT_2, steam_world, tree_hash

LIVE = LiveRef("steam", STEAM_ACCOUNT)
OTHER = LiveRef("steam", STEAM_ACCOUNT_2)


@pytest.fixture
def world(tmp_path):
    return steam_world(tmp_path)


def vids(folder: Path, slot: int) -> tuple:
    view = savedir.scan_slot(folder, slot)
    return tuple(_sha(Path(m.data_path)) if m.exists else None for m in view.members)


def test_a_swap_exchanges_two_slots_in_one_op(world):
    """T-F3: each slot gets the other's pair (a one-member pair fills both positions), one
    op id, one D1 record per slot."""
    one, two = vids(world.live, 1), vids(world.live, 2)
    plan = plan_swap(world.ctx, LIVE.target(1), LIVE.target(2))
    assert plan.kind == "swap" and not plan.blocked and not plan.confirmations
    outcome = execute(world.ctx, plan, plan.confirmations)
    assert outcome.status == "done", outcome
    assert outcome.outcome == "Swapped Steam slot 1 (Desktop) and Steam slot 2 (Desktop)."
    assert vids(world.live, 2) == one
    assert vids(world.live, 1) == (two[0], two[0])                # Second, both positions
    assert savedir.scan_slot(world.live, 2).newest.label == "A"
    ops = [r for r in world.ctx.store.journal_records() if r.get("op_id") == outcome.op_id]
    assert sorted(r["target"]["slot"] for r in ops) == [1, 2]
    line = list(world.ctx.vault.read_oplog())[-1]
    assert line["op"] == "swap" and len(line["targets"]) == 2


def test_a_swap_between_accounts_asks_once(world):
    plan = plan_swap(world.ctx, LIVE.target(1), OTHER.target(3))
    assert plan.confirmations == {"OTHER_ACCOUNT"}
    one, three = vids(world.live, 1), vids(world.other, 3)
    assert execute(world.ctx, plan, plan.confirmations).status == "done"
    assert (vids(world.live, 1), vids(world.other, 3)) == (three, one)


def test_a_swap_with_an_empty_side_is_blocked(world):
    before = tree_hash(world.live)
    plan = plan_swap(world.ctx, LIVE.target(1), LIVE.target(5))
    assert [w.code for w in plan.blocked] == ["SWAP_EMPTY_SIDE"]
    assert plan.blocked[0].text.startswith("Steam slot 5 (Desktop) is empty")
    assert execute(world.ctx, plan, plan.confirmations).status == "refused"
    assert tree_hash(world.live) == before and not jr.ops_path(world.ctx).exists()


@pytest.mark.parametrize("game", [False, True])
def test_keep_a_copy_writes_nothing_live(world, game):
    """T-K1: an extract entry holding the slot's versions; the live tree is untouched; an
    oplog line; no D1 op record; the game may run."""
    before = tree_hash(world.live)
    world.ctx.game_probe = lambda: game
    plan = plan_keep(world.ctx, LIVE.target(1))
    assert plan.kind == "keep" and [w.code for w in plan.warnings] == ["NOT_IN_VAULT"]
    outcome = execute(world.ctx, plan, plan.confirmations)
    assert outcome.status == "done" and outcome.outcome == \
        "'Main' from Steam slot 1 (Desktop) is in the vault."
    assert tree_hash(world.live) == before
    line = list(world.ctx.vault.read_oplog())[-1]
    entry = world.ctx.versions.entry(line["entry_id"])
    assert (line["op"], entry.kind) == ("keep", "extract")
    assert {m.vid for m in entry.members} == set(vids(world.live, 1))
    assert all(world.ctx.locator.is_durable(v) for v in vids(world.live, 1))
    assert not [r for r in world.ctx.store.journal_records() if r.get("op_id") == outcome.op_id]
    assert [e["event"] for e in jr.read_ops(world.ctx)[outcome.op_id].events] == [
        "preparing", "done"]
    world.refresh()
    assert not plan_keep(world.ctx, LIVE.target(1)).warnings          # in the vault now


def test_keep_an_empty_slot_or_a_changed_one(world):
    assert [w.code for w in plan_keep(world.ctx, LIVE.target(5)).blocked] == ["NOTHING_TO_KEEP"]
    plan = plan_keep(world.ctx, LIVE.target(1))
    os.utime(world.live / "save.hg", (1, 1))              # the game touched it since
    outcome = execute(world.ctx, plan, ())
    assert outcome.status == "stale" and outcome.replan is not None


@pytest.mark.parametrize("cloud", [True, False])
def test_the_empty_slot_steps_follow_the_cloud_mode(tmp_path, cloud):
    """T-E1: the refill warning only where a cloud can refill; the four files named."""
    world = steam_world(tmp_path, cloud=cloud)
    guide = empty_slot_guide(world.ctx, LIVE.target(7))
    assert (guide.cloud_mode, guide.occupied) == (
        "steam-cloud" if cloud else "steam-local", False)
    assert (guide.refill is not None) == cloud
    joined = " ".join(guide.steps)
    assert "save13.hg, mf_save13.hg, save14.hg, mf_save14.hg" in joined
    assert len(guide.steps) == (5 if cloud else 2)
    full = empty_slot_guide(world.ctx, LIVE.target(1))
    assert full.occupied and not full.durable
    if cloud:
        assert full.refill.code == "EMPTY_SLOT_MAY_REFILL" and full.refill.severity == "info"
    assert all(s.isascii() for s in guide.steps)


def test_the_xbox_steps(world, make_wgs_account):
    from .liveops_support import T0, XBOX_ACCOUNT, live_source

    acct = make_wgs_account(world.tmp / "SystemAppData",
                            [("Slot1Auto", '{"a":1}', "X", "", 60, T0 + 5.5)])
    world.sources.append(live_source(acct, platform_name="xbox"))
    world.refresh()
    guide = empty_slot_guide(world.ctx, LiveRef("xbox", XBOX_ACCOUNT).target(2))
    assert guide.refill is not None and "load menu" in " ".join(guide.steps)


def test_no_plan_deletes_a_live_save_file(world, monkeypatch):
    """Part of T-NODEL: a place, a swap and a keep remove no live save file (only the op's
    own temp files)."""

    removed = []
    real_remove, real_unlink = os.remove, Path.unlink
    monkeypatch.setattr(os, "remove", lambda p, *a, **k: (removed.append(str(p)),
                                                          real_remove(p, *a, **k))[1])
    monkeypatch.setattr(Path, "unlink", lambda self, *a, **k: (removed.append(str(self)),
                                                               real_unlink(self, *a, **k))[1])
    for make in (lambda: plan_place(world.ctx, world.polestar(), LIVE.target(1)),
                 lambda: plan_swap(world.ctx, LIVE.target(1), LIVE.target(2)),
                 lambda: plan_keep(world.ctx, LIVE.target(2))):
        plan = make()
        assert execute(world.ctx, plan, plan.confirmations).status == "done"
        world.refresh()
    live = [p for p in removed if Path(p).parent in (world.live, world.other)]
    assert all(Path(p).name.startswith(".tmp_") for p in live), live


def _sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
