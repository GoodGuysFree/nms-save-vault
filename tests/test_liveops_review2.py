"""Regression tests of the second review of live operations (D5 2.10-2.12)."""
from __future__ import annotations

import os
import re

from nms_save_vault.core import msstore
from nms_save_vault.core.liveops import opjournal as jr
from nms_save_vault.core.liveops.execute import execute
from nms_save_vault.core.liveops.planner import plan_swap
from nms_save_vault.core.organizer.model import LiveRef

from .liveops_support import T0, XBOX_ACCOUNT, live_source, steam_world


def _xbox_owner(acct, slot):
    """Which original slot's content ('x1' / 'x2') each member of a slot holds."""
    return {re.search(rb"x(\d)", open(m.data_path, "rb").read()).group(0).decode()
            for m in msstore.scan(acct).slots[slot].present_members}


def test_xbox_swap_same_account(tmp_path, make_wgs_account):
    world = steam_world(tmp_path)
    acct = make_wgs_account(tmp_path / "SystemAppData", [
        ("AccountData", '{"acct":1}', "", "", 0, T0 + 0.5),
        ("Slot1Auto", '{"x1a":1}', "Xone", "", 600, T0 + 10.5, 0xA1B2C3D4_00000001),
        ("Slot1Manual", '{"x1m":1}', "Xone", "", 500, T0 + 5.5, 0xA1B2C3D4_00000001),
        ("Slot2Auto", '{"x2a":1}', "Xtwo", "", 700, T0 + 12.5, 0xA1B2C3D4_00000002),
        ("Slot2Manual", '{"x2m":1}', "Xtwo", "", 650, T0 + 6.5, 0xA1B2C3D4_00000002)])
    os.utime(acct / "containers.index", (T0, T0))
    world.sources.append(live_source(acct, platform_name="xbox"))
    world.refresh()
    xbox = LiveRef("xbox", XBOX_ACCOUNT)
    before = (acct / "containers.index").read_bytes()
    plan = plan_swap(world.ctx, xbox.target(1), xbox.target(2))
    outcome = execute(world.ctx, plan, plan.confirmations)
    assert outcome.status == "done", outcome
    assert (acct / "containers.index").read_bytes() != before

    names = lambda slot: _xbox_owner(acct, slot)       # noqa: E731

    assert names(1) == {"x2"} and names(2) == {"x1"}
    from nms_save_vault.core.liveops.undo import plan_undo
    undo = plan_undo(world.ctx, outcome.op_id)       # the undo of a swap is one write as well
    assert not undo.blocked, undo.blocked
    back = execute(world.ctx, undo, undo.confirmations)
    assert back.status == "done", back
    assert names(1) == {"x1"} and names(2) == {"x2"}


def test_an_interrupted_xbox_swap_is_finished_by_recovery(tmp_path, make_wgs_account):
    import pytest as _pytest

    from nms_save_vault.core.liveops import FaultInjector, SimulatedCrash
    from nms_save_vault.core.liveops.recovery import pending_recovery, resolve_recovery

    world = steam_world(tmp_path)
    acct = make_wgs_account(tmp_path / "SystemAppData", [
        ("AccountData", '{"acct":1}', "", "", 0, T0 + 0.5),
        ("Slot1Auto", '{"x1a":1}', "Xone", "", 600, T0 + 10.5, 0xA1B2C3D4_00000001),
        ("Slot1Manual", '{"x1m":1}', "Xone", "", 500, T0 + 5.5, 0xA1B2C3D4_00000001),
        ("Slot2Auto", '{"x2a":1}', "Xtwo", "", 700, T0 + 12.5, 0xA1B2C3D4_00000002),
        ("Slot2Manual", '{"x2m":1}', "Xtwo", "", 650, T0 + 6.5, 0xA1B2C3D4_00000002)])
    os.utime(acct / "containers.index", (T0, T0))
    world.sources.append(live_source(acct, platform_name="xbox"))
    world.refresh()
    xbox = LiveRef("xbox", XBOX_ACCOUNT)
    plan = plan_swap(world.ctx, xbox.target(1), xbox.target(2))
    world.ctx.faults = FaultInjector({"after_index"})
    with _pytest.raises(SimulatedCrash):
        execute(world.ctx, plan, plan.confirmations)
    world.ctx.faults = None
    world.refresh()
    (item,) = pending_recovery(world.ctx)
    res = resolve_recovery(world.ctx, item.op_id, "forward")
    assert res.status == "done", res
    assert _xbox_owner(acct, 1) == {"x2"} and _xbox_owner(acct, 2) == {"x1"}


# --- verification ---------------------------------------------------------------------------

import time as _time

from nms_save_vault.core import savedir
from nms_save_vault.core.liveops import update_verification
from nms_save_vault.core.liveops.planner import plan_place, plan_promote

from .conftest import steam_save
from .liveops_support import MAIN, STEAM_ACCOUNT

LIVE = LiveRef("steam", STEAM_ACCOUNT)
SLOT1 = ("save.hg", "mf_save.hg", "save2.hg", "mf_save2.hg")


def _snap(world):
    return {n: ((world.live / n).read_bytes(), (world.live / n).stat().st_mtime_ns)
            for n in SLOT1}


def _put_back(world, before):
    for name, (data, mtime) in before.items():
        (world.live / name).write_bytes(data)
        os.utime(world.live / name, ns=(mtime, mtime))


def _vstate(world, op_id):
    return jr.read_ops(world.ctx)[op_id].verify_state(LIVE.target(1))


def test_confirmed_while_the_game_is_still_running(tmp_path):
    world = steam_world(tmp_path)
    before = _snap(world)
    plan = plan_place(world.ctx, world.polestar(), LIVE.target(1))
    out = execute(world.ctx, plan, plan.confirmations)
    assert out.status == "done"
    # the game has just started (the refresh sees it running), the slot still holds Polestar
    world.refresh(game_probe=lambda: True)
    world.ctx.clock = lambda: _time.time() + 5
    update_verification(world.ctx)
    assert _vstate(world, out.op_id) in (None, "pending")     # a running game confirms nothing
    # then the cloud copy comes back (a revert during the session)
    _put_back(world, before)
    world.refresh()
    update_verification(world.ctx)
    assert _vstate(world, out.op_id) == "reverted"


def test_a_reverted_promote_is_reverted(tmp_path):
    world = steam_world(tmp_path)
    before = _snap(world)
    assert savedir.scan_slot(world.live, 1).newest.label == "A"
    plan = plan_promote(world.ctx, LIVE.target(1), "B")
    out = execute(world.ctx, plan, plan.confirmations)
    assert out.status == "done", out
    assert savedir.scan_slot(world.live, 1).newest.label == "B"
    _put_back(world, before)                      # Steam Cloud puts the old metas back
    world.refresh()
    assert savedir.scan_slot(world.live, 1).newest.label == "A"   # the promote is undone
    world.ctx.clock = lambda: _time.time() + 3600
    from nms_save_vault.core.liveops import verify
    verify.note_game_state(world.ctx, True)       # a later session
    world.ctx.clock = lambda: _time.time() + 7200
    update_verification(world.ctx)
    assert _vstate(world, out.op_id) == "reverted"


def test_an_older_version_of_the_evicted_save_comes_back(tmp_path):
    world = steam_world(tmp_path)
    plan = plan_place(world.ctx, world.polestar(), LIVE.target(1))
    out = execute(world.ctx, plan, plan.confirmations)
    assert out.status == "done"
    # the cloud's copy of slot 1: an older Main (less play), not the exact evicted pair
    steam_save(world.live, 1, 0, "Main", 7000, MAIN, 1_789_999_000, extra="main-older-a")
    steam_save(world.live, 1, 1, "Main", 6900, MAIN, 1_789_998_000, extra="main-older-b")
    world.refresh()
    update_verification(world.ctx)
    assert _vstate(world, out.op_id) == "reverted"


def test_a_stale_undo_is_replanned_as_an_undo(tmp_path):
    from nms_save_vault.core.liveops.undo import plan_undo

    world = steam_world(tmp_path)
    plan = plan_place(world.ctx, world.polestar(), LIVE.target(1))
    out = execute(world.ctx, plan, plan.confirmations)
    assert out.status == "done"
    undo = plan_undo(world.ctx, out.op_id)
    assert not undo.blocked
    # before the owner confirms, the game starts a new save in slot 1 (another pid)
    steam_save(world.live, 1, 0, "Other", 10, 0x4242, 1_800_000_000, extra="other")
    steam_save(world.live, 1, 1, "Other", 9, 0x4242, 1_799_999_000, extra="other-b")
    world.refresh()
    res = execute(world.ctx, undo, undo.confirmations)
    rp = res.replan
    assert res.status == "stale" and rp is not None
    assert rp.kind == "undo" and rp.undo_of == out.op_id
    assert [w.code for w in rp.blocked] == ["UNDO_STALE"]


def test_back_on_a_failed_fill_empties_a_slot_the_game_played(tmp_path, monkeypatch):
    import pytest as _pytest
    from nms_save_vault.core.liveops import FaultInjector, steamwrite
    from nms_save_vault.core.liveops.recovery import pending_recovery, resolve_recovery

    world = steam_world(tmp_path)
    target = LIVE.target(5)                                  # empty
    assert not savedir.scan_slot(world.live, 5).newest
    plan = plan_place(world.ctx, world.polestar(), target)
    assert plan.kind == "place_into_empty", plan.kind

    def corrupt():
        (world.live / "save9.hg").write_bytes(b"broken")     # validation fails

    real = steamwrite.rollback
    monkeypatch.setattr(steamwrite, "rollback",
                        lambda *a: (_ for _ in ()).throw(OSError(5, "held open")))
    world.ctx.faults = FaultInjector(actions={"swapped:4": corrupt})
    outcome = execute(world.ctx, plan, plan.confirmations)
    assert outcome.status == "failed", outcome
    monkeypatch.setattr(steamwrite, "rollback", real)
    world.ctx.faults = None
    # the owner plays: the game loads slot 5 and saves new progress into it
    steam_save(world.live, 5, 0, "Polestar", 9000, 0x7E57AB1E00000001, 1_800_000_000,
               extra="new-progress-a")
    steam_save(world.live, 5, 1, "Polestar", 8900, 0x7E57AB1E00000001, 1_799_999_000,
               extra="new-progress-b")
    import hashlib as _h
    vid = _h.sha256((world.live / "save9.hg").read_bytes()).hexdigest()
    world.refresh()
    (item,) = pending_recovery(world.ctx)
    assert (item.recommended, item.choices) == ("leave", ("leave",))
    res = resolve_recovery(world.ctx, outcome.op_id, "back")
    assert res.status == "refused"
    left = sorted(p.name for p in world.live.iterdir() if p.name.startswith(("save9", "save10",
                                                                             "mf_save9", "mf_save10")))
    assert len(left) == 4, "slot 5 was emptied"
    assert resolve_recovery(world.ctx, outcome.op_id, "leave").status == "rolled_back"
    assert world.ctx.locator.is_durable(vid)             # what the game wrote is kept
    assert (world.live / "save9.hg").is_file()


def test_an_xbox_validated_op_after_an_upload_is_left_unfinished(tmp_path, make_wgs_account):
    import struct as _struct

    import pytest as _pytest
    from nms_save_vault.core.liveops import FaultInjector, SimulatedCrash
    from nms_save_vault.core.liveops.recovery import pending_recovery

    from .test_liveops_recovery import XBOX, xbox_world

    world, acct, source = xbox_world(tmp_path, make_wgs_account)
    plan = plan_place(world.ctx, source, XBOX.target(1))
    world.ctx.faults = FaultInjector({"after_validated"})
    with _pytest.raises(SimulatedCrash):
        execute(world.ctx, plan, plan.confirmations)
    world.ctx.faults = None
    (op_id,) = jr.read_ops(world.ctx)
    # the Xbox app uploads our two records: Modified -> Synced (nothing else changes)
    index = acct / "containers.index"
    header, records, trailer = msstore.split_index(index.read_bytes())
    out = []
    for ident, rec in records:
        if ident.startswith("Slot1"):
            info = msstore.record_info(rec)
            off = len(rec) - msstore.RECORD_TAIL_LENGTH
            rec = bytearray(rec)
            _struct.pack_into("<i", rec, off + 1, msstore.BLOB_SYNC_SYNCED)
            rec = bytes(rec)
            assert msstore.record_info(rec).sync_state == msstore.BLOB_SYNC_SYNCED, info
        out.append((ident, rec))
    index.write_bytes(msstore.join_index(header, out, trailer))
    world.refresh()
    items = pending_recovery(world.ctx)
    assert not items
    assert jr.read_ops(world.ctx)[op_id].status == "done"


def test_undo_of_a_swap_after_play(tmp_path):
    from nms_save_vault.core.liveops.undo import plan_undo

    from .liveops_support import SECOND

    world = steam_world(tmp_path)
    plan = plan_swap(world.ctx, LIVE.target(1), LIVE.target(2))
    out = execute(world.ctx, plan, plan.confirmations)
    assert out.status == "done", out
    # the owner plays 'Second' (now in slot 1) for an hour
    steam_save(world.live, 1, 0, "Second", 4200, SECOND, 1_800_000_000, extra="second-played")
    played = savedir.scan_slot(world.live, 1).members[0]
    import hashlib as _h
    played_vid = _h.sha256(open(played.data_path, "rb").read()).hexdigest()
    world.refresh()
    undo = plan_undo(world.ctx, out.op_id)
    assert not undo.blocked
    (played_warning,) = [w for w in undo.warnings if w.code == "PLAYED_SINCE"]
    assert "'Second'" in played_warning.text and "Main" not in played_warning.text
    res = execute(world.ctx, undo, undo.confirmations)
    assert res.status == "done", res
    s2 = savedir.scan_slot(world.live, 2).members[0]
    s2_vid = _h.sha256(open(s2.data_path, "rb").read()).hexdigest()
    assert s2_vid == played_vid
