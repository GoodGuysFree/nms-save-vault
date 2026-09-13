"""Clearing a live slot, and the play-time check that decides whether to warn first.

The point of the warning is that it is the only thing standing between the user and a
deleted save, so the tests pin what it is measured against: the newest copy of THAT slot
in the vault, the live folder itself never counting as its own backup.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from nms_save_vault.core import operations as ops
from nms_save_vault.core import savedir, slotmap
from nms_save_vault.core.catalog import Vault


def _vault(tmp_path) -> Vault:
    v = Vault(tmp_path / "vault")
    v.ensure()
    return v


# --- the plan / warning ------------------------------------------------------


def test_plan_warns_when_the_vault_has_no_copy(make_wgs_account, tmp_path):
    live = make_wgs_account(tmp_path, [("Slot1Auto", '{"a":1}', "Only copy", "", 3600, 1000)])
    plan = ops.plan_clear_slot(_vault(tmp_path), live, 1)

    assert plan.occupied and not plan.backed_up
    assert plan.live_play_time == 3600
    assert plan.progress_at_risk == 3600
    assert "No copy of slot 1" in plan.warning


def test_plan_is_quiet_when_the_vault_matches(make_wgs_account, tmp_path):
    live = make_wgs_account(tmp_path, [("Slot1Auto", '{"a":1}', "Backed up", "", 3600, 1000)])
    vault = _vault(tmp_path)
    ops.create_full_backup(vault, live, label="before clearing")

    plan = ops.plan_clear_slot(vault, live, 1)
    assert plan.backed_up and plan.progress_at_risk == 0
    assert plan.warning == ""


def test_plan_warns_when_play_time_advanced_past_the_backup(make_wgs_account, tmp_path):
    backup = make_wgs_account(tmp_path / "bk", [("Slot1Auto", '{"a":1}', "Old", "", 3600, 1000)])
    live = make_wgs_account(tmp_path / "live", [("Slot1Auto", '{"a":1}', "Now", "", 12600, 2000)])
    vault = _vault(tmp_path)
    ops.import_backup(vault, backup, label="yesterday")

    plan = ops.plan_clear_slot(vault, live, 1)
    assert plan.backed_up and plan.backup_play_time == 3600
    assert plan.progress_at_risk == 9000            # 2h30 further on than the backup
    assert "2h30m more play time" in plan.warning


def test_the_live_folder_is_never_its_own_backup(make_wgs_account, tmp_path):
    """Catalogued in place, the live folder satisfies "a vault entry holding slot 1" -- by
    pointing at the very save about to be deleted. It must not silence the warning."""
    live = make_wgs_account(tmp_path, [("Slot1Auto", '{"a":1}', "Only copy", "", 3600, 1000)])
    vault = _vault(tmp_path)
    ops.import_backup(vault, live, label="live, in place")

    plan = ops.plan_clear_slot(vault, live, 1)
    assert not plan.backed_up and plan.warning


def test_plan_of_an_empty_slot_says_nothing_to_lose(make_wgs_account, tmp_path):
    live = make_wgs_account(tmp_path, [("Slot1Auto", '{"a":1}', "Save", "", 3600, 1000)])
    plan = ops.plan_clear_slot(_vault(tmp_path), live, 5)
    assert not plan.occupied and plan.warning == ""


def test_format_duration():
    assert ops.format_duration(0) == "0m"
    assert ops.format_duration(3540) == "59m"
    assert ops.format_duration(9000) == "2h30m"


# --- clearing: Xbox / wgs ----------------------------------------------------


def test_clear_slot_xbox_empties_it_and_undo_puts_it_back(make_wgs_account, tmp_path):
    live = make_wgs_account(tmp_path, [
        ("Slot2Auto", '{"a":1}', "Auto save", "", 100, 1000),
        ("Slot2Manual", '{"b":2}', "Manual save", "", 100, 2000),
        ("Slot3Auto", '{"c":3}', "Neighbour", "", 100, 1000),
    ])
    vault = _vault(tmp_path)

    res = ops.clear_slot(vault, live, 2, allow_game_running=True)
    assert res.ok and res.snapshot_id
    view = savedir.scan_any(live)
    assert not view.slots[2].occupied
    assert view.slots[3].occupied                    # neighbour untouched
    assert view.xbox_index.sync_state == 2           # index flagged Modified

    ops.undo_last(vault, live, allow_game_running=True)
    after = savedir.scan_any(live)
    assert after.slots[2].a.save_name == "Auto save"
    assert after.slots[2].b.save_name == "Manual save"


def test_cleared_xbox_slot_can_be_refilled(make_wgs_account, tmp_path):
    """A cleared record stays in containers.index flagged Deleted; writing into it again
    has to un-delete it rather than leave the Xbox app queueing a cloud removal."""
    live = make_wgs_account(tmp_path, [
        ("Slot1Auto", '{"a":1}', "Keeper", "", 100, 1000),
        ("Slot2Auto", '{"b":2}', "Doomed", "", 100, 1000),
    ])
    vault = _vault(tmp_path)
    ops.clear_slot(vault, live, 2, allow_game_running=True)

    ops.repopulate_slot(vault, live, 1, live, 2, allow_game_running=True)
    assert savedir.scan_any(live).slots[2].a.save_name == "Keeper"


def test_clearing_an_empty_slot_is_refused(make_wgs_account, tmp_path):
    live = make_wgs_account(tmp_path, [("Slot1Auto", '{"a":1}', "Save", "", 100, 1000)])
    with pytest.raises(ops.OperationError, match="already empty"):
        ops.clear_slot(_vault(tmp_path), live, 4, allow_game_running=True)


def test_clearing_an_out_of_range_slot_is_refused(make_wgs_account, tmp_path):
    live = make_wgs_account(tmp_path, [("Slot1Auto", '{"a":1}', "Save", "", 100, 1000)])
    with pytest.raises(ops.OperationError, match="out of range"):
        ops.clear_slot(_vault(tmp_path), live, 99, allow_game_running=True)


# --- clearing: Steam ---------------------------------------------------------


@pytest.fixture
def steam_sandbox(live_save_dir, tmp_path):
    live = tmp_path / "live"
    live.mkdir()
    for p in live_save_dir.glob("*.hg"):
        shutil.copy2(p, live / p.name)
    return live, _vault(tmp_path)


def test_clear_slot_steam_deletes_both_members_and_undo_restores(steam_sandbox):
    live, vault = steam_sandbox
    occupied = [s.slot for s in savedir.scan(live).occupied_slots]
    if not occupied:
        pytest.skip("the live folder has no occupied slots to clear")
    victim = occupied[0]

    res = ops.clear_slot(vault, live, victim, allow_game_running=True)
    assert res.ok and res.snapshot_id
    for fno in slotmap.slot_file_numbers(victim):
        assert not (live / slotmap.data_filename(fno)).exists()
        assert not (live / slotmap.meta_filename(fno)).exists()
    assert [s.slot for s in savedir.scan(live).occupied_slots] == occupied[1:]

    ops.undo_last(vault, live, allow_game_running=True)
    assert [s.slot for s in savedir.scan(live).occupied_slots] == occupied


def test_clear_slot_steam_sweeps_up_a_half_present_slot(steam_sandbox):
    """A slot whose data file is gone reads as unoccupied, but its meta is still on disk.
    Clearing has to take that with it, not call the slot empty and leave the stray."""
    live, vault = steam_sandbox
    occupied = [s.slot for s in savedir.scan(live).occupied_slots]
    if not occupied:
        pytest.skip("the live folder has no occupied slots to clear")
    victim = occupied[0]
    fno = slotmap.slot_file_numbers(victim)[0]
    (live / slotmap.data_filename(fno)).unlink()
    stray = live / slotmap.meta_filename(fno)
    assert stray.is_file()

    ops.clear_slot(vault, live, victim, allow_game_running=True)
    assert not stray.exists()


def test_clear_slot_logs_an_undoable_operation(make_wgs_account, tmp_path):
    live = make_wgs_account(tmp_path, [("Slot1Auto", '{"a":1}', "Save", "", 100, 1000)])
    vault = _vault(tmp_path)
    res = ops.clear_slot(vault, live, 1, allow_game_running=True)

    record = vault.read_oplog()[-1]
    assert record["op"] == "clear_slot" and record["snapshot_id"] == res.snapshot_id
    assert Path(vault.get(res.snapshot_id).path).is_dir()
