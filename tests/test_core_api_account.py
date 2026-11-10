"""Unlocks written to a Steam account's account data (build item 14 B; GGF chose option A,
2026-11-10): ids added to the account's Unlocked* lists, written as NMS Save Vault writes a
save (the game closed, one write at a time, a whole-folder safety snapshot first, journaled
in the shared op journal as its own kind, checked), so a write that stops part-way is put
back from the snapshot. Made-up account files only."""
from __future__ import annotations

import hashlib
import json
import struct

import pytest

from nms_save_vault.core import api, formats, meta, safety
from nms_save_vault.core.liveops import opjournal, recovery
from nms_save_vault.core.testing import obfuscated

from .test_core_api_write import _session, savecraft, world  # noqa: F401 - fixtures

DATA, META = formats.ACCOUNT_DATA_NAME, formats.ACCOUNT_META_NAME
T_META = 1_790_000_000


def account_text(lists: dict) -> bytes:
    root = obfuscated({"Version": 4234, "UserSettingsData": {
        "UnlockedTitles": [], "UnlockedSpecials": [], "UnlockedSeasonRewards": [],
        "UnlockedTwitchRewards": [], **lists}})
    return json.dumps(root, separators=(",", ":")).encode("ascii") + b"\x00"


def account_meta(size: int, ts: int = T_META) -> bytes:
    plain = bytearray(formats.META_LEN_WORLDS_II)
    struct.pack_into("<I", plain, formats.OFF_HEADER, formats.META_HEADER)
    struct.pack_into("<I", plain, formats.OFF_META_FORMAT, formats.META_FORMAT_3)
    for off in (formats.OFF_SIZE_DECOMPRESSED, formats.OFF_SIZE_DISK, 0x54):
        struct.pack_into("<I", plain, off, size)
    struct.pack_into("<I", plain, formats.OFF_TIMESTAMP, ts)
    return meta.encrypt(bytes(plain), formats.ACCOUNT_STORAGE_ORDINAL)


def give_account(folder, **lists):
    data = account_text({k: [f"^{v}" for v in vs] for k, vs in lists.items()})
    (folder / DATA).write_bytes(data)
    (folder / META).write_bytes(account_meta(len(data)))
    return data


def unlocked(folder, name: str) -> list:
    root = api.read_account(folder)
    return api.get(root, "UserSettingsData", name)


def meta_of(folder) -> meta.MetaInfo:
    plain = meta.decrypt((folder / META).read_bytes(), formats.ACCOUNT_STORAGE_ORDINAL)
    return meta.parse(plain, formats.ACCOUNT_STORAGE_ORDINAL)


def files(folder) -> dict:
    return {n: (folder / n).read_bytes() for n in (DATA, META)}


def test_unlocks_are_added_to_the_account_and_its_meta_tells_the_new_size(world):
    give_account(world.live, UnlockedSeasonRewards=["S1"], UnlockedTwitchRewards=["T1"])
    session = _session(world)
    plan = session.plan_unlock(world.live, {"UnlockedSeasonRewards": ["^S2", "^S1"],
                                            "UnlockedTwitchRewards": ["^T9"]},
                               what="unlock 2 rewards")
    assert not plan.blocked and plan.adds == (("UnlockedSeasonRewards", "^S2"),
                                              ("UnlockedTwitchRewards", "^T9"))
    done = session.write_account(plan, plan.confirmations)
    assert done.status == "done", done.text
    assert unlocked(world.live, "UnlockedSeasonRewards") == ["^S1", "^S2"]
    assert unlocked(world.live, "UnlockedTwitchRewards") == ["^T1", "^T9"]
    data = (world.live / DATA).read_bytes()
    assert data.endswith(b"}\x00") and data.count(b"\x00") == 1       # as the game writes it
    info = meta_of(world.live)
    assert info.valid and info.size_decompressed == info.size_disk == len(data)
    assert info.timestamp > T_META
    rec = opjournal.read_ops(session._now())[done.op_id]
    assert (rec.kind, rec.status, rec.targets) == ("account_edit", "done", ())


def test_the_account_as_it_was_is_kept_in_the_vault_first(world):
    before = give_account(world.live, UnlockedSeasonRewards=["S1"])
    session = _session(world)
    plan = session.plan_unlock(world.live, {"UnlockedSeasonRewards": ["^S2"]}, what="x")
    done = session.write_account(plan, plan.confirmations)
    from nms_save_vault.core.liveops.panic import account_files
    rec = opjournal.read_ops(session._now())[done.op_id]
    (target,) = rec.last("writing")["targets"]
    assert account_files(session._now(), target["snapshot_id"])[DATA] == before


@pytest.mark.parametrize("unlocks, code", [
    ({"UnlockedSeasonRewards": ["^S1"]}, "NOTHING_TO_UNLOCK"),       # unlocked already
    ({"UnlockedPlatformRewards": ["^P1"]}, "ACCOUNT_LIST_MISSING"),  # the file keeps none
])
def test_nothing_new_or_a_list_the_file_lacks_is_not_written(world, unlocks, code):
    give_account(world.live, UnlockedSeasonRewards=["S1"])
    plan = _session(world).plan_unlock(world.live, unlocks, what="x")
    assert [b.code for b in plan.blocked] == [code]


@pytest.mark.parametrize("unlocks", [{"KnownSpecials": ["^A"]}, {"UnlockedSpecials": ["a\"b"]},
                                     {"UnlockedSpecials": [7]}, {"UnlockedSpecials": ["x" * 80]}])
def test_only_ids_into_the_account_s_unlock_lists_are_taken(world, unlocks):
    give_account(world.live)
    with pytest.raises(ValueError):
        _session(world).plan_unlock(world.live, unlocks, what="x")


def test_an_account_changed_since_the_plan_is_not_written(world):
    give_account(world.live, UnlockedSeasonRewards=["S1"])
    session = _session(world)
    plan = session.plan_unlock(world.live, {"UnlockedSeasonRewards": ["^S2"]}, what="x")
    now = give_account(world.live, UnlockedSeasonRewards=["S1", "S7"])     # the game wrote it
    result = session.write_account(plan, plan.confirmations)
    assert result.status == "stale" and (world.live / DATA).read_bytes() == now


def test_nothing_is_written_while_the_game_runs_or_cannot_be_told(world, monkeypatch):
    give_account(world.live, UnlockedSeasonRewards=["S1"])
    session = _session(world)
    plan = session.plan_unlock(world.live, {"UnlockedSeasonRewards": ["^S2"]}, what="x")
    before = files(world.live)
    for running, code in ((True, "GAME_RUNNING"), (None, "GAME_STATE_UNKNOWN")):
        monkeypatch.setattr(safety, "is_game_running", lambda *a, r=running, **k: r)
        result = session.write_account(plan, {"GAME_STATE_UNKNOWN", "GAME_RUNNING"})
        assert (result.status, result.code) == ("refused", code)
        assert files(world.live) == before


def test_a_write_that_stopped_between_the_two_files_is_put_back_from_the_snapshot(world):
    from nms_save_vault.core import testing
    give_account(world.live, UnlockedSeasonRewards=["S1"])
    before = files(world.live)
    session = _session(world)
    plan = session.plan_unlock(world.live, {"UnlockedSeasonRewards": ["^S2"]}, what="x")
    with testing.crash_during(session, "account_data_written"):
        with pytest.raises(testing.SimulatedCrash):
            session.write_account(plan, plan.confirmations)
    assert files(world.live) != before                                   # torn: data only
    later = _session(world)
    (item,) = later.interrupted()
    assert item.recommended == "back" and set(item.choices) == {"back", "leave"}
    assert later.resolve(item.op_id, "back").status == "rolled_back"
    assert files(world.live) == before and later.interrupted() == []


def test_a_write_that_stopped_after_both_files_is_finished_by_itself(world):
    from nms_save_vault.core import testing
    give_account(world.live, UnlockedSeasonRewards=["S1"])
    session = _session(world)
    plan = session.plan_unlock(world.live, {"UnlockedSeasonRewards": ["^S2"]}, what="x")
    with testing.crash_during(session, "account_meta_written"):
        with pytest.raises(testing.SimulatedCrash):
            session.write_account(plan, plan.confirmations)
    later = _session(world)
    assert later.interrupted() == []
    assert unlocked(world.live, "UnlockedSeasonRewards") == ["^S1", "^S2"]
    rec = next(r for r in opjournal.read_ops(later._now()).values() if r.kind == "account_edit")
    assert rec.status == "done"


def test_an_older_core_only_leaves_a_stopped_account_write(world):
    """NMS Save Vault 0.3.9 reads the shared journal too. Its recovery reads every target of
    a write as a save slot's: an account write's has none, so it sees a record it cannot use
    and offers only to leave it (never to put a slot back or finish it)."""
    from nms_save_vault.core import testing
    give_account(world.live, UnlockedSeasonRewards=["S1"])
    session = _session(world)
    plan = session.plan_unlock(world.live, {"UnlockedSeasonRewards": ["^S2"]}, what="x")
    with testing.crash_during(session, "account_data_written"):
        with pytest.raises(testing.SimulatedCrash):
            session.write_account(plan, plan.confirmations)
    ctx = _session(world)._now()
    rec = next(r for r in opjournal.read_ops(ctx).values() if r.kind == "account_edit")
    op = recovery._open(ctx, rec)                       # the generic path an older core takes
    assert op.damaged and op.state == "other"


def test_an_xbox_or_missing_account_file_is_not_written(world):
    (world.live / DATA).unlink()
    plan = _session(world).plan_unlock(world.live, {"UnlockedSpecials": ["^A"]}, what="x")
    assert [b.code for b in plan.blocked] == ["NO_ACCOUNT_DATA"]


def test_the_journal_records_each_file_before_and_after_by_its_hash_only(world):
    before = give_account(world.live, UnlockedSeasonRewards=["S1"])
    session = _session(world)
    plan = session.plan_unlock(world.live, {"UnlockedSeasonRewards": ["^S2"]}, what="x")
    session.write_account(plan, plan.confirmations)
    text = (world.vault_root / "organizer" / opjournal.OPS).read_text("ascii")
    for data in (before, (world.live / DATA).read_bytes()):
        assert hashlib.sha256(data).hexdigest() in text
    assert "S1" not in text                             # never what the account holds
