"""liveops.verify: post-launch verification (D5 2.12; T-VR1..VR6) and the game-seen recorder.
Every path is under tmp_path."""
from __future__ import annotations

import os
import time
from types import SimpleNamespace

import pytest

from nms_save_vault.core import msstore
from nms_save_vault.core.liveops import dismiss_verification, update_verification
from nms_save_vault.core.liveops import opjournal as jr
from nms_save_vault.core.liveops import verify
from nms_save_vault.core.liveops.execute import execute
from nms_save_vault.core.liveops.planner import plan_place
from nms_save_vault.core.organizer.model import LiveRef

from .conftest import steam_save
from .liveops_support import POLESTAR, STEAM_ACCOUNT, steam_world

LIVE = LiveRef("steam", STEAM_ACCOUNT)
TARGET = LIVE.target(1)
SLOT1 = ("save.hg", "mf_save.hg", "save2.hg", "mf_save2.hg")


@pytest.fixture
def world(tmp_path):
    return steam_world(tmp_path)


def place(world):
    """Put Polestar into slot 1; returns the op id and the files the slot held before."""
    stats = {n: ((world.live / n).read_bytes(), (world.live / n).stat().st_mtime_ns)
             for n in SLOT1}
    plan = plan_place(world.ctx, world.polestar(), TARGET)
    out = execute(world.ctx, plan, plan.confirmations)
    assert out.status == "done", out
    return out.op_id, stats


def state(world, op_id) -> str | None:
    return jr.read_ops(world.ctx)[op_id].verify_state(TARGET)


def test_a_cloud_revert_is_an_alert_until_dismissed(world):
    """VR1: the evicted bytes with their old mtimes come back."""
    op_id, before = place(world)
    for name, (data, mtime) in before.items():
        (world.live / name).write_bytes(data)
        os.utime(world.live / name, ns=(mtime, mtime))
    world.refresh()
    (item,) = update_verification(world.ctx)
    assert (item.op_id, item.state, item.label) == (op_id, "reverted", "Steam slot 1 (Desktop)")
    assert item.text.startswith("Steam Cloud put the old save back in Steam slot 1")
    assert "is safe in the vault" in item.text and item.text.isascii()
    assert item.actions == ("place_again", "show_steps", "dismiss")
    assert [i.state for i in update_verification(world.ctx)] == ["reverted"]    # still shown
    dismiss_verification(world.ctx, op_id, TARGET)
    assert update_verification(world.ctx) == []
    assert state(world, op_id) == "reverted"                                    # kept in history


def test_a_revert_on_gog_names_its_cloud_and_offers_the_steps(tmp_path):
    """GOG keeps no cloud marker in the folder, so its cloud is never ruled out."""
    world = steam_world(tmp_path, cloud=False, other_name="DefaultUser")
    target = LiveRef("steam", "DefaultUser").target(1)
    before = {n: ((world.other / n).read_bytes(), (world.other / n).stat().st_mtime_ns)
              for n in SLOT1}
    plan = plan_place(world.ctx, world.polestar(), target)
    assert execute(world.ctx, plan, plan.confirmations).status == "done"
    for name, (data, mtime) in before.items():
        (world.other / name).write_bytes(data)
        os.utime(world.other / name, ns=(mtime, mtime))
    world.refresh()
    (item,) = update_verification(world.ctx)
    assert (item.state, item.label) == ("reverted", "GOG slot 1")
    assert item.text.startswith("Cloud sync put the old save back in GOG slot 1")
    assert item.actions == ("place_again", "show_steps", "dismiss")


def test_playing_on_the_placed_save_confirms_it(world):
    """VR2."""
    op_id, _ = place(world)
    assert [i.state for i in update_verification(world.ctx)] == ["pending"]
    steam_save(world.live, 1, 0, "Polestar", 5000, POLESTAR, 1_800_000_000, extra="played-on")
    later = time.time() + 600
    os.utime(world.live / "save.hg", (later, later))
    world.refresh()
    assert update_verification(world.ctx) == []
    assert state(world, op_id) == "confirmed"
    last = [e for e in jr.read_ops(world.ctx)[op_id].events if e["event"] == "verify"][-1]
    assert last["detail"] == "played"


def test_xbox_records_going_to_synced_count_as_uploaded():
    """VR3 (the evidence rule): only a pair whose every record is Synced is uploaded."""
    synced = SimpleNamespace(present=[SimpleNamespace(sync_state=msstore.BLOB_SYNC_SYNCED)] * 2)
    pending = SimpleNamespace(present=[SimpleNamespace(sync_state=msstore.BLOB_SYNC_SYNCED),
                                       SimpleNamespace(sync_state=msstore.BLOB_SYNC_MODIFIED)])
    xbox, steam = LiveRef("xbox", "x").target(1), TARGET
    assert verify._uploaded(synced, xbox) and not verify._uploaded(pending, xbox)
    assert not verify._uploaded(synced, steam)


def test_another_save_is_replaced_and_an_emptied_slot_is_deleted(world):
    """VR4."""
    op_id, _ = place(world)
    steam_save(world.live, 1, 0, "Other", 10, 0x4242, 1_800_000_000, extra="other")
    steam_save(world.live, 1, 1, "Other", 9, 0x4242, 1_799_999_000, extra="other-b")
    world.refresh()
    assert update_verification(world.ctx) == []                  # replaced: history only
    assert state(world, op_id) == "replaced"

    op2, _ = place(world)
    for name in SLOT1:
        (world.live / name).unlink()
    world.refresh()
    (item,) = update_verification(world.ctx)
    assert (item.op_id, item.state) == (op2, "deleted")
    assert item.text.startswith("Steam slot 1 (Desktop) is empty again.")
    assert item.actions == ("place_again", "dismiss")


def test_a_game_session_after_the_op_confirms_an_unchanged_slot(world):
    """VR5: without a session it stays pending; with one it is confirmed (unchanged)."""
    op_id, _ = place(world)
    (item,) = update_verification(world.ctx)
    assert item.state == "pending" and item.text == "Waiting for the next game launch to confirm."
    assert item.actions == () and state(world, op_id) == "pending"
    world.ctx.clock = lambda: time.time() + 3600
    verify.note_game_state(world.ctx, False)
    verify.note_game_state(world.ctx, None)
    assert verify.last_seen_running(world.ctx) is None
    verify.note_game_state(world.ctx, True)
    assert verify.last_seen_running(world.ctx) is not None
    assert update_verification(world.ctx) == []
    last = [e for e in jr.read_ops(world.ctx)[op_id].events if e["event"] == "verify"][-1]
    assert (last["state"], last["detail"]) == ("confirmed", "unchanged")


def test_every_probe_that_sees_the_game_is_recorded(world):
    ctx = world.refresh(game_probe=lambda: True)
    assert verify.last_seen_running(ctx) is None
    assert ctx.game_probe() is True
    assert verify.last_seen_running(ctx) is not None
    assert verify.game_seen_path(ctx).parent.name == ctx.store.vault_id


def test_a_pending_op_expires_after_thirty_days(world):
    op_id, _ = place(world)
    world.ctx.clock = lambda: time.time() + 31 * 86400
    assert update_verification(world.ctx) == []
    assert state(world, op_id) == "expired"
