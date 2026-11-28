"""A restore's account data is written as one journaled op (GGF 2026-11-28: a power-out during
a write to a live folder must be found at the next start, and rolled back or finished).

Two paths write a Steam folder's account data after its slots are restored: the roll-back the
owner asked for (``restore_accountdata``), and a folder with no account data of its own, which
gets the backup's. Each writes two files; a crash between them was not noticed (audit gaps 1
and 2)."""
from __future__ import annotations

import pytest

from nms_save_vault.core.liveops import FaultInjector, SimulatedCrash, account
from nms_save_vault.core.liveops import opjournal as jr
from nms_save_vault.core.liveops import panic
from nms_save_vault.core.liveops.recovery import pending_recovery, resolve_recovery
from nms_save_vault.core.organizer.model import LiveRef

from .liveops_support import STEAM_ACCOUNT, steam_world

LIVE = LiveRef("steam", STEAM_ACCOUNT)
DATA, META = "accountdata.hg", "mf_accountdata.hg"


@pytest.fixture
def world(tmp_path):
    w = steam_world(tmp_path)
    (w.live / DATA).write_bytes(b"live account data")         # differs from the backup's
    (w.live / META).write_bytes(b"live account meta")
    return w


def _files(folder):
    return {n: (folder / n).read_bytes() for n in (DATA, META) if (folder / n).is_file()}


def _backups(world):
    return panic.account_files(world.ctx, world.backup_id)


def _crash(world, point, write):
    world.ctx.faults = FaultInjector({point})
    with pytest.raises(SimulatedCrash):
        write()
    world.ctx.faults = None
    world.refresh()


def _rollback(world):
    return panic.write_account_files(world.ctx, LIVE, world.backup_id)


def _give_missing(world):
    return panic.write_account_files(world.ctx, LIVE, world.backup_id, only_if_missing=True)


def _ops(world):
    return [r for r in jr.read_ops(world.ctx).values() if r.kind == account.RESTORE_KIND]


def test_the_roll_back_of_the_account_data_is_one_journaled_op(world):
    outcome = _rollback(world)
    assert outcome.status == "done", outcome
    assert _files(world.live) == _backups(world)
    (rec,) = _ops(world)
    assert rec.status == "done"


def test_a_roll_back_cut_between_the_two_files_is_found_and_put_back(world):
    before = _files(world.live)
    _crash(world, "account_data_written", lambda: _rollback(world))
    assert _files(world.live) != before                         # torn: the data file only
    (item,) = pending_recovery(world.ctx)
    assert item.kind == account.RESTORE_KIND and item.recommended == "back"
    assert resolve_recovery(world.ctx, item.op_id, "back").status == "rolled_back"
    assert _files(world.live) == before and pending_recovery(world.ctx) == []


def test_a_roll_back_cut_after_both_files_is_finished_by_itself(world):
    _crash(world, "account_meta_written", lambda: _rollback(world))
    assert pending_recovery(world.ctx) == []
    assert _files(world.live) == _backups(world)
    (rec,) = _ops(world)
    assert rec.status == "done"


def test_a_folder_with_no_account_data_gets_the_backups_as_one_journaled_op(world):
    for name in (DATA, META):
        (world.live / name).unlink()
    assert _give_missing(world).status == "done"
    assert _files(world.live) == _backups(world)
    (rec,) = _ops(world)
    assert rec.status == "done"


def test_a_folder_with_account_data_keeps_it_when_only_missing_files_are_given(world):
    before = _files(world.live)
    assert _give_missing(world).status == "done"
    assert _files(world.live) == before and _ops(world) == []


def test_giving_missing_account_data_cut_half_way_is_put_back_to_none(world):
    for name in (DATA, META):
        (world.live / name).unlink()
    _crash(world, "account_data_written", lambda: _give_missing(world))
    assert set(_files(world.live)) == {DATA}                    # one file without the other
    (item,) = pending_recovery(world.ctx)
    assert item.recommended == "back"
    assert resolve_recovery(world.ctx, item.op_id, "back").status == "rolled_back"
    assert _files(world.live) == {} and pending_recovery(world.ctx) == []


def test_an_older_core_only_leaves_a_stopped_account_restore(world):
    """Releases up to 0.5.1 read the shared journal too: a kind they do not know reads as a
    record they cannot use, which they only offer to leave."""
    from nms_save_vault.core.liveops import recovery
    _crash(world, "account_data_written", lambda: _rollback(world))
    (rec,) = _ops(world)
    op = recovery._open(world.ctx, rec)
    assert op.damaged and op.state == "other"
