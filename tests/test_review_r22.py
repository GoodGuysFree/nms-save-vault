"""Review 22's findings (build item 14 B, account-data unlocks), each a test that failed before
its fix. Made-up account files only."""
from __future__ import annotations

import struct
from pathlib import Path

import pytest

from nms_save_vault.core import api, formats, meta, safety, testing
from nms_save_vault.core.liveops import opjournal
from nms_save_vault.core.organizer import store as orgstore

from .test_core_api_account import DATA, META, files, give_account
from .test_core_api_write import UNITS, _crashed_edit, _session, _slot, _switch_off
from .test_core_api_write import savecraft, world  # noqa: F401 - fixtures


def _torn_account_write(world):
    """An account write that stopped between its two files (the data written, not the meta)."""
    give_account(world.live, UnlockedSeasonRewards=["S1"])
    before = files(world.live)
    session = _session(world)
    plan = session.plan_unlock(world.live, {"UnlockedSeasonRewards": ["^S2"]}, what="x")
    with testing.crash_during(session, "account_data_written"):
        with pytest.raises(testing.SimulatedCrash):
            session.write_account(plan, plan.confirmations)
    assert files(world.live) != before
    return before


# R22-1 ---------------------------------------------------------------------------------------
def test_r22_1_a_torn_account_write_in_a_folder_switched_off_waits_instead_of_being_left(
        world):
    """A slot write that stopped in a folder switched off since waits (no choices, "none":
    test_an_interrupted_edit_in_a_folder_switched_off_since_waits_for_it). The account write
    is offered only "leave" (recommended): Savecraft's open_session then makes the user
    settle it, and leaving ends the op with the account file torn for good."""
    before = _torn_account_write(world)
    _switch_off(world, world.live, writable=False)
    session = _session(world)
    (item,) = session.interrupted()
    try:
        assert (item.recommended, item.choices) == ("none", ())
    finally:
        if "leave" in item.choices:               # what the user is pushed to do
            session.resolve(item.op_id, "leave")
            world.own.unlink()                    # switched on again
            again = _session(world)
            assert files(world.live) != before and again.interrupted() == []


# R22-2 ---------------------------------------------------------------------------------------
def test_r22_2_no_save_write_starts_on_a_folder_whose_account_write_waits(world):
    """api.Interrupted: "until it is finished or put back, no write starts on that save
    folder". recovery_pending reads an op's slot targets only; an account_edit has none."""
    _torn_account_write(world)
    session = _session(world)
    assert session.interrupted()                          # it waits
    with pytest.raises(api.EditRefused, match="interrupted"):
        session.plan_edit(_slot(world), UNITS, what="y")


# R22-3 ---------------------------------------------------------------------------------------
def test_r22_3_no_account_write_starts_on_a_folder_whose_save_write_waits(world,
                                                                               monkeypatch):
    """The account write checks only for open account writes (_open_on), not for a slot
    write of the same folder that stopped part-way."""
    give_account(world.live, UnlockedSeasonRewards=["S1"])
    session = _crashed_edit(world, monkeypatch)
    session.refresh()
    assert session.interrupted()                          # the slot write waits
    before = files(world.live)
    plan = session.plan_unlock(world.live, {"UnlockedSeasonRewards": ["^S2"]}, what="x")
    result = session.write_account(plan, plan.confirmations) if not plan.blocked else None
    assert result is None or (result.status, result.code) == ("refused", "RECOVERY_PENDING")
    assert files(world.live) == before


# R22-4 ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("error", [orgstore.StoreBusy, lambda: orgstore.StoreReadOnly("ro")])
def test_r22_4_a_busy_vault_journal_is_a_refusal_not_an_exception(world, monkeypatch,
                                                                         error):
    """execute() turns StoreBusy/StoreReadOnly from the 'preparing' append into a BUSY
    refusal; write_unlock catches only OSError there, so write_account raises (and Savecraft
    catches neither: the Tk callback dies)."""
    give_account(world.live, UnlockedSeasonRewards=["S1"])
    session = _session(world)
    plan = session.plan_unlock(world.live, {"UnlockedSeasonRewards": ["^S2"]}, what="x")
    real = opjournal.append

    def busy(ctx, op_id, event, payload=None):
        if event == "preparing":
            raise error()
        return real(ctx, op_id, event, payload)

    monkeypatch.setattr(opjournal, "append", busy)
    before = files(world.live)
    result = session.write_account(plan, plan.confirmations)
    assert result.status == "refused"
    assert files(world.live) == before


# R22-5 ---------------------------------------------------------------------------------------
def _vanilla_meta(size: int) -> bytes:
    plain = bytearray(formats.META_LEN_VANILLA)
    struct.pack_into("<I", plain, formats.OFF_HEADER, formats.META_HEADER)
    struct.pack_into("<I", plain, formats.OFF_META_FORMAT, formats.META_FORMAT_1)
    struct.pack_into("<I", plain, formats.OFF_SIZE_DECOMPRESSED, size)
    struct.pack_into("<I", plain, formats.OFF_SIZE_DISK, size)
    return meta.encrypt(bytes(plain), formats.ACCOUNT_STORAGE_ORDINAL)


@pytest.mark.parametrize("bad_meta", ["empty", "cut", "vanilla"])
def test_r22_5_an_odd_account_meta_is_a_refusal_not_an_exception(world, bad_meta):
    """plan_unlock never reads the meta, so the plan is not blocked; _built then decrypts it
    and stamps 0x164: an empty or cut (3-byte) meta raises IndexError, a 104-byte one
    struct.error - neither caught (only ValueError, KeyError, ... are), so
    write_account raises instead of returning a refusal."""
    data = give_account(world.live, UnlockedSeasonRewards=["S1"])
    (world.live / META).write_bytes({"empty": b"", "cut": b"\x01\x02\x03",
                                     "vanilla": _vanilla_meta(len(data))}[bad_meta])
    session = _session(world)
    plan = session.plan_unlock(world.live, {"UnlockedSeasonRewards": ["^S2"]}, what="x")
    before = files(world.live)
    if not plan.blocked:
        result = session.write_account(plan, plan.confirmations)
        assert result.status == "refused"
    assert files(world.live) == before


# R22-9 ---------------------------------------------------------------------------------------
def test_r22_9_an_xbox_unlock_is_refused_with_an_unlock_s_reason(tmp_path,
                                                                      make_wgs_account):
    """plan_unlock blocks Xbox with ACCOUNTDATA_XBOX, whose text is the panic restore's:
    "Restoring an Xbox account's data is not available." - shown for an unlock."""
    from nms_save_vault.core import msstore
    from nms_save_vault.core.liveops import account
    from nms_save_vault.core.organizer.model import LiveRef

    from .liveops_support import T0, XBOX_ACCOUNT, live_source, steam_world

    world = steam_world(tmp_path)
    acct = make_wgs_account(tmp_path / "SystemAppData", [
        ("AccountData", '{"acct":1}', "", "", 0, T0 + 0.5),
        ("Slot1Auto", '{"x1a":1}', "Xmain", "", 600, T0 + 10.5)])
    (acct.parent.parent / "xgs" / acct.name).mkdir(parents=True)
    msstore.mirror_xgs(acct)
    world.sources.append(live_source(acct, platform_name="xbox"))
    world.refresh()
    plan = account.plan_unlock(world.ctx, LiveRef("xbox", XBOX_ACCOUNT),
                               {"UnlockedSpecials": ["^A"]}, what="x")
    (why,) = plan.blocked
    assert "restor" not in why.text.lower(), why.text


# R22-6 ---------------------------------------------------------------------------------------
def test_r22_6_a_failed_account_write_the_game_wrote_over_recommends_only_leave(
        world, monkeypatch):
    """A write whose put-back failed ('failed'), then the game rewrote the account: the files
    are someone else's ('other'). _account_pending checks the status before the state and
    recommends "back", which _account_resolve then refuses (SLOT_CHANGED); a slot op in the
    same case is "superseded", leave only."""
    give_account(world.live, UnlockedSeasonRewards=["S1"])
    session = _session(world)
    plan = session.plan_unlock(world.live, {"UnlockedSeasonRewards": ["^S2"]}, what="x")
    real = safety.atomic_write_bytes
    failed = {"meta": False}

    def flaky(path, data, *a, **k):
        p = Path(path)
        if p.parent == world.live and p.name == META and not failed["meta"]:
            failed["meta"] = True
            raise OSError("the disk went away")
        if p.parent == world.live and p.name == DATA and failed["meta"]:
            raise OSError("the disk went away")                 # the put-back fails too
        return real(path, data, *a, **k)

    monkeypatch.setattr(safety, "atomic_write_bytes", flaky)
    out = session.write_account(plan, plan.confirmations)
    monkeypatch.setattr(safety, "atomic_write_bytes", real)
    assert out.status == "failed"
    give_account(world.live, UnlockedSeasonRewards=["S1", "S7"])      # the game wrote it since
    later = _session(world)
    (item,) = later.interrupted()
    result = later.resolve(item.op_id, item.recommended)
    assert result.status in ("done", "rolled_back"), (item.recommended, item.choices, result)
