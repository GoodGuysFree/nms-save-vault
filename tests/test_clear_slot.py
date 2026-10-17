"""Clearing a live slot, and the play-time check that decides whether to warn first.

The point of the warning is that it is the only thing standing between the user and a
deleted save, so the tests pin what it is measured against: the vault's best copy of THAT
playthrough (by the game's own id, wherever it sits -- never by slot number), the live
folder itself never counting as its own backup.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from nms_save_vault.core import operations as ops
from nms_save_vault.core import formats, lz4_block, savedir, slotmap
from nms_save_vault.core.catalog import Vault

from .conftest import steam_save


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
    assert "No copy of the save in slot 1 ('Only copy')" in plan.warning


def test_plan_is_quiet_when_the_vault_matches(make_wgs_account, tmp_path):
    live = make_wgs_account(tmp_path, [("Slot1Auto", '{"a":1}', "Backed up", "", 3600, 1000)])
    vault = _vault(tmp_path)
    ops.create_full_backup(vault, live, label="before clearing")

    plan = ops.plan_clear_slot(vault, live, 1)
    assert plan.backed_up and plan.progress_at_risk == 0
    assert plan.warning == ""


def test_plan_warns_when_play_time_advanced_past_the_backup(make_wgs_account, tmp_path):
    backup = make_wgs_account(tmp_path / "bk", [("Slot1Auto", '{"z":0}', "Old", "", 3600, 1000)])
    live = make_wgs_account(tmp_path / "live", [("Slot1Auto", '{"a":1}', "Now", "", 12600, 2000)])
    vault = _vault(tmp_path)
    ops.import_backup(vault, backup, label="yesterday")

    plan = ops.plan_clear_slot(vault, live, 1)
    assert not plan.backed_up and plan.backup_play_time == 3600
    assert plan.progress_at_risk == 12600            # not identical: all of it is at stake
    assert "2h30m less play time" in plan.warning


def test_the_live_folder_is_never_its_own_backup(make_wgs_account, tmp_path):
    """Catalogued in place, the live folder satisfies "a vault entry holding slot 1" -- by
    pointing at the very save about to be deleted. It must not silence the warning."""
    live = make_wgs_account(tmp_path, [("Slot1Auto", '{"a":1}', "Only copy", "", 3600, 1000)])
    vault = _vault(tmp_path)
    ops.import_backup(vault, live, label="live, in place")

    plan = ops.plan_clear_slot(vault, live, 1)
    assert not plan.backed_up and plan.warning


def test_a_different_save_in_the_same_slot_number_is_not_a_backup(make_wgs_account, tmp_path):
    """The vault's slot 3 holds another playthrough with far more play time. By slot number
    the live save looked backed up (a false "safe"); by playthrough it is not."""
    backup = make_wgs_account(tmp_path / "bk", [("Slot3Auto", '{"a":1}', "Save Y", "", 9000, 1000, 0xBBBB)])
    live = make_wgs_account(tmp_path / "live", [("Slot3Auto", '{"b":2}', "Save X", "", 1000, 2000, 0xAAAA)])
    vault = _vault(tmp_path)
    ops.import_backup(vault, backup, label="other run")

    plan = ops.plan_clear_slot(vault, live, 3)

    assert not plan.backed_up and plan.progress_at_risk == 1000
    assert "No copy of the save in slot 3 ('Save X')" in plan.warning


def test_the_same_playthrough_in_another_slot_is_found(make_wgs_account, tmp_path):
    backup = make_wgs_account(tmp_path / "bk", [
        ("Slot7Auto", '{"a":1}', "Main", "", 4000, 1000, 0xCAFE),
        ("Slot1Auto", '{"z":0}', "Other", "", 99999, 1000, 0xBEEF),
    ])
    live = make_wgs_account(tmp_path / "live", [("Slot1Auto", '{"b":2}', "Main", "", 5000, 2000, 0xCAFE)])
    vault = _vault(tmp_path)
    entry = ops.import_backup(vault, backup, label="moved")

    plan = ops.plan_clear_slot(vault, live, 1)

    assert plan.entry_id == entry.id and plan.backup_play_time == 4000   # shown, as information
    assert not plan.identical and not plan.backed_up and "different branch" in plan.warning


def test_an_identical_copy_is_noted(make_wgs_account, tmp_path):
    live = make_wgs_account(tmp_path / "live", [("Slot2Auto", '{"b":2}', "Main", "", 5000, 2000)])
    vault = _vault(tmp_path)
    ops.import_backup(vault, make_wgs_account(tmp_path / "older", [("Slot2Auto", '{"a":1}', "Main", "", 6000, 1000)]))
    exact = ops.create_full_backup(vault, live, include_cache=False)

    plan = ops.plan_clear_slot(vault, live, 2)

    assert plan.identical and plan.entry_id == exact.id and plan.warning == ""


def test_a_steam_slot_is_matched_by_playthrough_too(tmp_path):
    other = tmp_path / "other"
    steam_save(other, 4, 0, "Save Y", 9000, 0xBBBB, 1000)
    live = tmp_path / "live"
    steam_save(live, 4, 0, "Save X", 1000, 0xAAAA, 2000)
    vault = _vault(tmp_path)
    ops.import_backup(vault, other, copy_into_vault=True)

    plan = ops.plan_clear_slot(vault, live, 4)

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


# --- Steam Cloud: the delete that does not stick -----------------------------


def test_plan_spells_out_the_steam_cloud_procedure(steam_sandbox):
    """Steam puts back a file that is only MISSING locally, so a plain delete is undone on
    the next launch. The plan has to say so before the user believes the slot is gone."""
    live, vault = steam_sandbox
    occupied = [s.slot for s in savedir.scan(live).occupied_slots]
    if not occupied:
        pytest.skip("the live folder has no occupied slots to clear")
    (live / formats.STEAM_AUTOCLOUD).write_text("", encoding="utf-8")

    plan = ops.plan_clear_slot(vault, live, occupied[0])
    assert plan.steam_cloud
    assert "Keep game saves in the Steam Cloud" in plan.cloud_warning


def test_plan_is_silent_about_the_cloud_when_the_folder_is_not_synced(steam_sandbox):
    live, vault = steam_sandbox
    occupied = [s.slot for s in savedir.scan(live).occupied_slots]
    if not occupied:
        pytest.skip("the live folder has no occupied slots to clear")
    (live / formats.STEAM_AUTOCLOUD).unlink(missing_ok=True)

    plan = ops.plan_clear_slot(vault, live, occupied[0])
    assert not plan.steam_cloud and plan.cloud_warning == ""


def test_xbox_plan_carries_no_steam_cloud_warning(make_wgs_account, tmp_path):
    """Xbox deletes propagate: the record stays in containers.index flagged Deleted, which
    is the cloud's instruction to remove it. Nothing to warn about."""
    live = make_wgs_account(tmp_path, [("Slot1Auto", '{"a":1}', "Save", "", 100, 1000)])
    plan = ops.plan_clear_slot(_vault(tmp_path), live, 1)
    assert not plan.steam_cloud and plan.cloud_warning == ""


# --- review: no fallback matching, unreadable vault files, the member the game loads ----------


def test_a_live_save_in_an_unreadable_container_is_noted_and_unverifiable(make_wgs_account, tmp_path):
    live = make_wgs_account(tmp_path / "live", [("Slot1Auto", '{"a":1}', "Voyagers", "", 3000, 2000, 0)])
    savedir.scan_any(live).slots[1].members[0].data_path.write_bytes(lz4_block.HGSAVEV2_HEADER + b"\x00" * 64)

    plan = ops.plan_clear_slot(_vault(tmp_path), live, 1)

    assert plan.unverifiable and not plan.backed_up
    assert any("HGSAVEV2" in n for n in plan.notes)


def test_the_live_folders_xgs_twin_is_never_counted_as_a_copy(make_wgs_account, tmp_path):
    """The xgs mirror holds the very bytes of the live wgs folder. Catalogued (an older
    discover did that), it would answer "backed up" by pointing at the live save itself."""
    live = make_wgs_account(tmp_path / "live", [("Slot1Auto", '{"a":1}', "Voyagers", "", 3000, 2000)])
    twin = tmp_path / "live" / "xgs" / live.name
    member = savedir.scan_any(live).slots[1].members[0]
    (twin / "Slot1Auto").mkdir(parents=True)
    (twin / "Slot1Auto" / "data").write_bytes(Path(member.data_path).read_bytes())
    (twin / "Slot1Auto" / "meta").write_bytes(Path(member.meta_path).read_bytes())
    vault = _vault(tmp_path)
    ops.import_backup(vault, twin, copy_into_vault=False)

    plan = ops.plan_clear_slot(vault, live, 1)

    assert not plan.identical and plan.entry_id is None and "No copy" in plan.warning


def test_a_live_save_whose_id_cannot_be_read_is_noted_like_a_vault_copy(make_wgs_account, tmp_path, monkeypatch):
    live = make_wgs_account(tmp_path / "live", [("Slot1Auto", '{"a":1}', "Voyagers", "", 3000, 2000, 0)])

    def unreadable(*_a, **_k):
        raise OSError("drive unplugged")

    monkeypatch.setattr(ops.playtime, "identity", unreadable)
    plan = ops.plan_clear_slot(_vault(tmp_path), live, 1)

    assert plan.unverifiable and any("drive unplugged" in n for n in plan.notes)


def test_a_save_without_an_id_needs_an_identical_copy(make_wgs_account, tmp_path):
    """Too old to carry a playthrough id: a same-named save in the same slot proves
    nothing; only a byte-identical copy does."""
    live = make_wgs_account(tmp_path / "live", [("Slot1Auto", '{"a":1}', "Voyagers", "", 3000, 2000, 0)])
    lookalike = make_wgs_account(tmp_path / "other", [("Slot1Auto", '{"z":9}', "Voyagers", "", 3000, 1000, 0)])
    vault = _vault(tmp_path)
    ops.import_backup(vault, lookalike, label="same name, other save")

    plan = ops.plan_clear_slot(vault, live, 1)
    assert plan.unverifiable and not plan.backed_up and "cannot be verified" in plan.warning

    ops.create_full_backup(vault, live, include_cache=False)
    plan = ops.plan_clear_slot(vault, live, 1)
    assert plan.backed_up and plan.identical and not plan.unverifiable and plan.warning == ""


def test_an_unreadable_vault_entry_is_noted_and_skipped(make_wgs_account, tmp_path, monkeypatch):
    live = make_wgs_account(tmp_path / "live", [("Slot1Auto", '{"a":1}', "Main", "", 3000, 2000)])
    vault = _vault(tmp_path)
    bad = ops.import_backup(vault, make_wgs_account(tmp_path / "bad", [("Slot1Auto", '{"q":1}', "Main", "", 1, 1)]))
    good = ops.create_full_backup(vault, live, include_cache=False)
    real_scan = savedir.scan_any

    def scan_fails_for_bad(path):
        if Path(path) == Path(bad.path):
            raise OSError("drive unplugged")
        return real_scan(path)

    monkeypatch.setattr(ops.savedir, "scan_any", scan_fails_for_bad)
    plan = ops.plan_clear_slot(vault, live, 1)

    assert plan.backed_up and plan.entry_id == good.id
    assert any(bad.id in n and "drive unplugged" in n for n in plan.notes)


def test_the_copy_is_judged_by_the_member_the_game_loads(make_wgs_account, tmp_path):
    """An exact copy of the OLDER member is not a copy of the save the game loads; a near
    copy of the loaded member with more play time is the better one, and not 'identical'."""
    saves = [
        ("Slot1Auto", '{"a":1}', "Run", "", 100, 1000),       # A: older
        ("Slot1Manual", '{"b":2}', "Run", "", 200, 2000),     # B: newer, the one that loads
    ]
    live = make_wgs_account(tmp_path / "live", saves)
    vault = _vault(tmp_path)
    exact_older = ops.import_backup(vault, make_wgs_account(tmp_path / "a", saves[:1]))
    near_newer = ops.import_backup(vault, make_wgs_account(tmp_path / "b", [
        ("Slot1Manual", '{"c":3}', "Run", "", 190, 1900),
    ]))

    plan = ops.plan_clear_slot(vault, live, 1)

    assert savedir.scan_any(live).slots[1].newest.label == "B"
    assert plan.entry_id == near_newer.id and plan.entry_id != exact_older.id
    assert not plan.identical and plan.live_play_time == 200 and plan.backup_play_time == 190


def test_the_cli_says_when_the_vault_holds_this_very_save(make_wgs_account, tmp_path, capsys):
    from nms_save_vault import cli

    live = make_wgs_account(tmp_path / "live", [("Slot1Auto", '{"a":1}', "Main", "", 3000, 2000)])
    vault = _vault(tmp_path)
    ops.create_full_backup(vault, live, include_cache=False)

    assert cli.main(["--live", str(live), "--vault", str(vault.root), "clear", "1", "--yes", "--force"]) == 0
    out = capsys.readouterr().out
    assert "Best copy in the vault" in out and "an identical copy" in out


# --- re-review: branches, the loaded member, live folders, same-run leftovers ----------------


def test_more_play_time_in_the_vault_is_no_proof_of_safety(make_wgs_account, tmp_path):
    """12h in the vault, 10h live: the live save may be a branch (an older point played on
    differently). Only an identical copy proves it is safe; the 12h copy is information."""
    vault = _vault(tmp_path)
    ops.import_backup(vault, make_wgs_account(tmp_path / "bk", [("Slot1Auto", '{"a":1}', "Run", "", 12 * 3600, 1000)]))
    live = make_wgs_account(tmp_path / "live", [("Slot1Auto", '{"b":2}', "Run", "", 10 * 3600, 2000)])

    plan = ops.plan_clear_slot(vault, live, 1)

    assert not plan.backed_up and plan.backup_play_time == 12 * 3600
    assert "2h00m more play time" in plan.warning and "different branch" in plan.warning


def test_the_plan_compares_the_loaded_members_own_play_time(make_wgs_account, tmp_path):
    live = make_wgs_account(tmp_path / "live", [
        ("Slot1Auto", '{"a":1}', "Run", "", 20000, 1000),     # older, but more play time
        ("Slot1Manual", '{"b":2}', "Run", "", 9000, 2000),    # newer: this one loads
    ])
    plan = ops.plan_clear_slot(_vault(tmp_path), live, 1)
    assert plan.live_play_time == 9000


def test_another_live_folder_is_never_counted_as_a_copy(make_wgs_account, tmp_path):
    saves = [("Slot1Auto", '{"a":1}', "Run", "", 3000, 1000)]
    live = make_wgs_account(tmp_path / "one", saves)
    twin = make_wgs_account(tmp_path / "two", saves)          # identical bytes, but live too
    vault = _vault(tmp_path)
    ops.import_backup(vault, twin, label="the other live folder, in place")

    assert ops.plan_clear_slot(vault, live, 1).identical
    assert not ops.plan_clear_slot(vault, live, 1, other_live_dirs=[twin]).identical
