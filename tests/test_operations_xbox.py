"""Xbox / Game Pass (wgs) write operations, exercised in a sandbox built from the synthetic
``make_wgs_account`` fixture. No real Game Pass install is ever touched, and every operation
snapshots first so ``undo`` can be asserted."""
from __future__ import annotations

import pytest

from nms_save_vault.core import operations as ops
from nms_save_vault.core import safety, savedir
from nms_save_vault.core.catalog import Vault


def _vault(tmp_path) -> Vault:
    v = Vault(tmp_path / "vault")
    v.ensure()
    return v


def test_repopulate_xbox_to_xbox(make_wgs_account, tmp_path):
    live = make_wgs_account(tmp_path, [
        ("Slot1Auto", '{"a":1}', "Source A", "", 100, 1000),
        ("Slot1Manual", '{"b":2}', "Source B", "", 90, 900),
    ])
    vault = _vault(tmp_path)
    assert not savedir.scan_any(live).slots[3].occupied

    res = ops.repopulate_slot(vault, live, 1, live, 3, allow_game_running=True)
    assert res.ok and res.snapshot_id
    view = savedir.scan_any(live)
    assert view.slots[3].a.save_name == "Source A"   # Auto  -> member A
    assert view.slots[3].b.save_name == "Source B"   # Manual -> member B
    assert view.slots[1].a.save_name == "Source A"   # source slot untouched
    assert view.xbox_index.sync_state == 2           # index flagged Modified

    ops.undo_last(vault, live, allow_game_running=True)
    assert not savedir.scan_any(live).slots[3].occupied


def test_promote_member_xbox(make_wgs_account, tmp_path):
    # Manual (B) is newer than Auto (A) initially (FILETIME 2000 vs 1000).
    live = make_wgs_account(tmp_path, [
        ("Slot2Auto", '{"a":1}', "Auto save", "", 100, 1000),
        ("Slot2Manual", '{"b":2}', "Manual save", "", 100, 2000),
    ])
    vault = _vault(tmp_path)
    assert savedir.scan_any(live).slots[2].newest.label == "B"

    res = ops.promote_member(vault, live, 2, 0, allow_game_running=True)  # promote A
    assert res.ok
    assert savedir.scan_any(live).slots[2].newest.label == "A"

    ops.undo_last(vault, live, allow_game_running=True)
    assert savedir.scan_any(live).slots[2].newest.label == "B"


def test_restore_full_xbox(make_wgs_account, tmp_path):
    live = make_wgs_account(tmp_path / "live", [("Slot1Auto", '{"x":1}', "Live Save", "", 5, 100)])
    backup = make_wgs_account(tmp_path / "bk", [
        ("Slot1Auto", '{"y":1}', "Backup A", "", 50, 500),
        ("Slot4Manual", '{"z":1}', "Backup D", "", 60, 600),
    ])
    vault = _vault(tmp_path)
    entry = ops.import_backup(vault, backup, label="bk")

    res = ops.restore_full(vault, entry, live, allow_game_running=True)
    assert res.ok
    view = savedir.scan_any(live)
    assert view.slots[1].a.save_name == "Backup A"
    assert view.slots[4].b.save_name == "Backup D"

    ops.undo_last(vault, live, allow_game_running=True)
    after = savedir.scan_any(live)
    assert after.slots[1].a.save_name == "Live Save"
    assert not after.slots[4].occupied


def test_xbox_repopulate_data_is_verbatim_and_leaves_others(make_wgs_account, tmp_path):
    """The moved save's data blob is byte-identical to the source, and a bystander slot's
    data is untouched by the wholesale containers.index rewrite."""
    live = make_wgs_account(tmp_path, [
        ("Slot1Auto", '{"k":7}', "Src", "", 1, 100),
        ("Slot2Auto", '{"bystndr":1}', "Bystander", "", 2, 200),
    ])
    vault = _vault(tmp_path)
    before = savedir.scan_any(live)
    src_bytes = before.slots[1].a.data_path.read_bytes()
    bystander_bytes = before.slots[2].a.data_path.read_bytes()

    ops.repopulate_slot(vault, live, 1, live, 6, allow_game_running=True)
    after = savedir.scan_any(live)
    assert after.slots[6].a.data_path.read_bytes() == src_bytes
    assert after.slots[2].a.data_path.read_bytes() == bystander_bytes   # bystander intact


def test_xbox_repopulate_single_member_source(make_wgs_account, tmp_path):
    """A source slot with only an Auto member copies just member A and warns about Manual."""
    live = make_wgs_account(tmp_path, [("Slot1Auto", '{"a":1}', "Only A", "", 1, 100)])
    vault = _vault(tmp_path)
    res = ops.repopulate_slot(vault, live, 1, live, 5, allow_game_running=True)
    after = savedir.scan_any(live)
    assert after.slots[5].a.save_name == "Only A"
    assert not after.slots[5].b.exists
    assert any("1B" in w for w in res.warnings)


def test_restore_full_xbox_deletes_extra_slots(make_wgs_account, tmp_path):
    """Restoring a smaller backup removes live slots not in it; undo brings them all back."""
    live = make_wgs_account(tmp_path / "live", [
        ("Slot1Auto", '{"a":1}', "Keep", "", 1, 100),
        ("Slot2Auto", '{"b":1}', "Drop 2", "", 1, 100),
        ("Slot3Auto", '{"c":1}', "Drop 3", "", 1, 100),
    ])
    backup = make_wgs_account(tmp_path / "bk", [("Slot1Auto", '{"a":1}', "Keep", "", 1, 100)])
    vault = _vault(tmp_path)
    entry = ops.import_backup(vault, backup, label="bk")

    ops.restore_full(vault, entry, live, allow_game_running=True)
    after = savedir.scan_any(live)
    assert after.slots[1].a.save_name == "Keep"
    assert not after.slots[2].occupied and not after.slots[3].occupied   # extras deleted

    ops.undo_last(vault, live, allow_game_running=True)
    back = savedir.scan_any(live)
    assert back.slots[2].a.save_name == "Drop 2" and back.slots[3].a.save_name == "Drop 3"


def test_restore_full_xbox_onto_itself_is_noop(make_wgs_account, tmp_path):
    """Restoring an in-place-imported live folder onto itself must not wipe it."""
    live = make_wgs_account(tmp_path, [("Slot1Auto", '{"a":1}', "Self", "", 1, 100)])
    vault = _vault(tmp_path)
    entry = ops.import_backup(vault, live, copy_into_vault=False)   # entry.path == live
    res = ops.restore_full(vault, entry, live, allow_game_running=True)
    assert res.ok
    assert savedir.scan_any(live).slots[1].a.save_name == "Self"   # still intact


def test_promote_already_newest_xbox(make_wgs_account, tmp_path):
    """Promoting the member that is already newest keeps it newest (idempotent-ish)."""
    live = make_wgs_account(tmp_path, [
        ("Slot1Auto", '{"a":1}', "A newer", "", 1, 2000),
        ("Slot1Manual", '{"b":1}', "B older", "", 1, 1000),
    ])
    vault = _vault(tmp_path)
    assert savedir.scan_any(live).slots[1].newest.label == "A"
    ops.promote_member(vault, live, 1, 0, allow_game_running=True)   # promote A again
    assert savedir.scan_any(live).slots[1].newest.label == "A"


def test_xbox_op_honors_game_running_guard(make_wgs_account, tmp_path, monkeypatch):
    """An Xbox write refuses when the game is detected running and no override is given."""
    live = make_wgs_account(tmp_path, [("Slot1Auto", '{"a":1}', "A", "", 1, 100)])
    vault = _vault(tmp_path)
    monkeypatch.setattr(safety, "is_game_running", lambda: True)
    with pytest.raises(ops.GameRunningError):
        ops.promote_member(vault, live, 1, 0)   # no allow_game_running


def test_repopulate_invalid_slot_raises(make_wgs_account, tmp_path):
    live = make_wgs_account(tmp_path, [("Slot1Auto", '{"a":1}', "A", "", 1, 100)])
    vault = _vault(tmp_path)
    with pytest.raises(ops.OperationError):
        ops.repopulate_slot(vault, live, 1, live, 99, allow_game_running=True)
