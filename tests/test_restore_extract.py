"""Restoring a single-slot extract must not mirror the live folder.

Regression tests for the bug where right-clicking an extract and choosing Restore ran it
through ``restore_full``: the extract holds one slot, mirroring deletes every live file
not in the backup, so the live folder was reduced to that one slot and lost its account
data as well.
"""
from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

from nms_save_vault.core import operations as ops
from nms_save_vault.core import savedir, slotmap
from nms_save_vault.core.catalog import KIND_EXTRACT, SlotSummary, Vault


@pytest.fixture
def sandbox(live_save_dir, tmp_path):
    """A throwaway live folder copied from the real one (read-only source)."""
    live = tmp_path / "live"
    live.mkdir()
    for p in live_save_dir.glob("*.hg"):
        shutil.copy2(p, live / p.name)
    vdf = live_save_dir / "steam_autocloud.vdf"
    if vdf.is_file():
        shutil.copy2(vdf, live / vdf.name)
    vault = Vault(tmp_path / "vault")
    vault.ensure()
    return live, vault


def _fingerprint(folder: Path) -> dict[str, str]:
    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in folder.iterdir()
        if p.is_file()
    }


def _slot_files(slot: int) -> set[str]:
    names = set()
    for fno in slotmap.slot_file_numbers(slot):
        names.add(slotmap.data_filename(fno))
        names.add(slotmap.meta_filename(fno))
    return names


# --- the guard ---------------------------------------------------------------


def test_restore_full_refuses_an_extract(sandbox):
    """The dangerous call is blocked in the core, not merely avoided by the UI."""
    live, vault = sandbox
    slot = savedir.scan(live).occupied_slots[0].slot
    extract = ops.extract_slot(vault, live, slot)

    with pytest.raises(ops.OperationError, match="single-slot extract"):
        ops.restore_full(vault, extract, live, allow_game_running=True)


def test_the_refusal_happens_before_anything_is_touched(sandbox):
    live, vault = sandbox
    slot = savedir.scan(live).occupied_slots[0].slot
    extract = ops.extract_slot(vault, live, slot)
    before = _fingerprint(live)

    with pytest.raises(ops.OperationError):
        ops.restore_full(vault, extract, live, allow_game_running=True)

    assert _fingerprint(live) == before


# --- the right behaviour -----------------------------------------------------


def test_restoring_an_extract_puts_its_slot_back_and_nothing_else(sandbox):
    live, vault = sandbox
    view = savedir.scan(live)
    slot = view.occupied_slots[0].slot
    other = view.occupied_slots[1].slot
    extract = ops.extract_slot(vault, live, slot)
    before = _fingerprint(live)

    # Overwrite the slot with a different one, so a real restore is observable.
    ops.repopulate_slot(vault, live, other, live, slot, allow_game_running=True)
    assert _fingerprint(live) != before

    result = ops.restore_entry(vault, extract, live, allow_game_running=True)
    after = _fingerprint(live)

    assert result.ok
    assert result.op == "restore_extract"
    assert f"slot {slot}" in result.detail
    assert after == before, "restoring the extract must return the folder to its exact prior state"


def test_restoring_an_extract_deletes_nothing(sandbox):
    """The bug's signature: files vanishing, including accountdata.hg."""
    live, vault = sandbox
    slot = savedir.scan(live).occupied_slots[0].slot
    extract = ops.extract_slot(vault, live, slot)
    names_before = set(_fingerprint(live))

    ops.restore_entry(vault, extract, live, allow_game_running=True)

    assert set(_fingerprint(live)) == names_before


def test_restoring_an_extract_leaves_every_other_slot_byte_identical(sandbox):
    live, vault = sandbox
    view = savedir.scan(live)
    slot = view.occupied_slots[0].slot
    extract = ops.extract_slot(vault, live, slot)
    before = _fingerprint(live)

    ops.restore_entry(vault, extract, live, allow_game_running=True)
    after = _fingerprint(live)

    untouched = set(before) - _slot_files(slot)
    assert untouched, "the fixture should have more than the one slot"
    for name in untouched:
        assert after[name] == before[name], f"{name} was modified"


def test_restoring_an_extract_is_undoable(sandbox):
    live, vault = sandbox
    slot = savedir.scan(live).occupied_slots[0].slot
    extract = ops.extract_slot(vault, live, slot)

    result = ops.restore_entry(vault, extract, live, allow_game_running=True)
    assert result.snapshot_id, "a change to the live folder must be undoable"
    undo = ops.undo_last(vault, live, allow_game_running=True)
    assert undo.ok


def test_restore_entry_still_mirrors_a_full_backup(sandbox):
    """The fix must not weaken restoring a real backup."""
    live, vault = sandbox
    occupied = [s.slot for s in savedir.scan(live).occupied_slots]
    entry = ops.create_full_backup(vault, live, label="full")

    victim = occupied[0]
    for name in _slot_files(victim):
        (live / name).unlink(missing_ok=True)
    assert victim not in [s.slot for s in savedir.scan(live).occupied_slots]

    result = ops.restore_entry(vault, entry, live, allow_game_running=True)
    assert result.op == "restore_full"
    assert [s.slot for s in savedir.scan(live).occupied_slots] == occupied


def test_an_empty_extract_is_reported_not_restored(sandbox, tmp_path):
    live, vault = sandbox
    slot = savedir.scan(live).occupied_slots[0].slot
    extract = ops.extract_slot(vault, live, slot)
    for s in extract.slots:
        s.occupied = False

    with pytest.raises(ops.OperationError, match="no saves"):
        ops.restore_entry(vault, extract, live, allow_game_running=True)


def test_a_multi_slot_extract_asks_for_repopulate_rather_than_guessing(sandbox):
    """Extracts are single-slot by construction, but never silently pick a slot."""
    live, vault = sandbox
    view = savedir.scan(live)
    slot = view.occupied_slots[0].slot
    extract = ops.extract_slot(vault, live, slot)
    # An extract caches exactly one slot summary; fabricate a second occupied one.
    extract.slots.append(
        SlotSummary(slot=slot + 1, occupied=True, name="second", newest_label="A", members=[])
    )

    with pytest.raises(ops.OperationError, match="one at a time"):
        ops.restore_entry(vault, extract, live, allow_game_running=True)


def test_extract_entries_are_the_only_kind_diverted(sandbox):
    live, vault = sandbox
    entry = ops.create_full_backup(vault, live, label="full")
    assert entry.kind != KIND_EXTRACT
    result = ops.restore_entry(vault, entry, live, allow_game_running=True)
    assert result.op == "restore_full"


# --- undo messages -----------------------------------------------------------
#
# The old message for every one of these was "no undoable operation found in the op log",
# which reads like the app is broken rather than explaining what happened.


def test_undo_with_nothing_done_says_so(sandbox):
    live, vault = sandbox
    with pytest.raises(ops.OperationError, match="[Nn]othing has been done yet"):
        ops.undo_last(vault, live, allow_game_running=True)


def test_undo_after_only_a_backup_explains_why_there_is_nothing_to_undo(sandbox):
    """Backing up and extracting only add to the vault; they never change live saves."""
    live, vault = sandbox
    ops.create_full_backup(vault, live, label="full")
    ops.extract_slot(vault, live, savedir.scan(live).occupied_slots[0].slot)

    with pytest.raises(ops.OperationError) as excinfo:
        ops.undo_last(vault, live, allow_game_running=True)
    message = str(excinfo.value)
    assert "nothing to undo" in message
    assert "never change your live saves" in message


def test_undo_reports_a_snapshot_that_has_gone_missing(sandbox):
    live, vault = sandbox
    view = savedir.scan(live)
    ops.repopulate_slot(vault, live, view.occupied_slots[0].slot, live,
                        view.occupied_slots[1].slot, allow_game_running=True)
    snap = [e for e in vault.entries if e.kind == "snapshot"][0]
    shutil.rmtree(snap.path)

    with pytest.raises(ops.OperationError, match="folder is gone"):
        ops.undo_last(vault, live, allow_game_running=True)


def test_undo_reports_a_snapshot_dropped_from_the_catalog(sandbox):
    live, vault = sandbox
    view = savedir.scan(live)
    ops.repopulate_slot(vault, live, view.occupied_slots[0].slot, live,
                        view.occupied_slots[1].slot, allow_game_running=True)
    snap = [e for e in vault.entries if e.kind == "snapshot"][0]
    vault.remove(snap.id)

    with pytest.raises(ops.OperationError, match="not in the catalog"):
        ops.undo_last(vault, live, allow_game_running=True)
