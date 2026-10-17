"""Xbox / Game Pass (wgs) write operations, exercised in a sandbox built from the synthetic
``make_wgs_account`` fixture. No real Game Pass install is ever touched, and every operation
snapshots first so ``undo`` can be asserted."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from nms_save_vault.core import operations as ops
from nms_save_vault.core import msstore, safety, savedir
from nms_save_vault.core import store as vstore
from nms_save_vault.core.store import classic as store_classic
from nms_save_vault.core.catalog import Vault

from .conftest import xbox_meta


@pytest.fixture(autouse=True)
def _xbox_app_is_quiet(monkeypatch):
    """The fixture accounts are written just now; the planner would call that a sync in
    progress (D5 2.3.3)."""
    from nms_save_vault.core.liveops import planner

    monkeypatch.setattr(planner, "XBOX_SYNC_SECONDS", 0)


def _plain_vault(tmp_path) -> Vault:
    v = Vault(tmp_path / "vault")
    v.ensure()
    return v


def _vault(tmp_path) -> Vault:
    v = Vault(tmp_path / "vault")
    v.ensure()
    vstore.create_store(v.root)          # live operations need the version store (D3)
    return v


def test_repopulate_xbox_to_xbox(make_wgs_account, tmp_path):
    live = make_wgs_account(tmp_path, [
        ("Slot1Auto", '{"a":1}', "Source A", "", 100, 1000),
        ("Slot1Manual", '{"b":2}', "Source B", "", 90, 900),
    ])
    vault = _vault(tmp_path)
    assert not savedir.scan_any(live).slots[3].occupied

    res = ops.repopulate_slot(vault, live, 1, live, 3, allow_game_running=True)
    assert res.ok and res.op_id
    view = savedir.scan_any(live)
    assert view.slots[3].a.save_name == "Source A"   # Auto  -> member A
    assert view.slots[3].b.save_name == "Source B"   # Manual -> member B
    assert view.slots[1].a.save_name == "Source A"   # source slot untouched
    assert view.xbox_index.sync_state == 2           # index flagged Modified

    ops.undo_last(vault, live, allow_game_running=True)
    assert savedir.scan_any(live).slots[3].occupied      # a restore never empties a slot (F8)


def test_promote_member_xbox(make_wgs_account, tmp_path):
    # Manual (B) is newer than Auto (A) initially (saved at 2000 vs 1000).
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


@pytest.mark.parametrize("meta_len", [296, 360])
def test_promote_xbox_moves_the_meta_time_and_the_index_time(make_wgs_account, tmp_path, meta_len):
    """B is newer by save time although A reached the disk later (index 5000 vs 4000).
    Promoting A must make it newer by both clocks, and change only the save time in its meta."""
    live = make_wgs_account(tmp_path, [
        ("Slot2Auto", '{"a":1}', "Run", "", 100, 5000, 7, 1000),
        ("Slot2Manual", '{"b":2}', "Run", "", 144, 4000, 7, 1044),
    ], meta_len=meta_len)
    vault = _vault(tmp_path)
    before = savedir.scan_any(live).slots[2]
    assert before.newest.label == "B"
    meta_before = Path(before.a.meta_path).read_bytes()
    started = time.time()

    ops.promote_member(vault, live, 2, 0, allow_game_running=True)

    after = savedir.scan_any(live).slots[2]
    assert after.newest.label == "A"
    assert after.a.timestamp >= int(started)   # the save time is stamped now (F5b)
    assert after.a.data_mtime >= int(started)       # the index time is the local write time: now
    meta_after = Path(after.a.meta_path).read_bytes()
    assert meta_after[:0x120] + meta_after[0x124:] == meta_before[:0x120] + meta_before[0x124:]
    assert meta_after != meta_before

    ops.undo_last(vault, live, allow_game_running=True)
    assert savedir.scan_any(live).slots[2].newest.label == "B"


def test_promote_xbox_without_a_meta_time_moves_the_index_time(make_wgs_account, tmp_path):
    """A 280-byte meta has no save time to set, so the index time alone orders the pair; the
    meta is written back unchanged."""
    live = make_wgs_account(tmp_path, [
        ("Slot2Auto", '{"a":1}', "Run", "", 100, 4000),
        ("Slot2Manual", '{"b":2}', "Run", "", 144, 5000),
    ])
    old_a = savedir.scan_any(live).slots[2].a
    waypoint_meta = xbox_meta("Run", play=100, length=280)
    Path(old_a.meta_path).write_bytes(waypoint_meta)
    assert savedir.scan_any(live).slots[2].newest.label == "B"

    ops.promote_member(_vault(tmp_path), live, 2, 0, allow_game_running=True)

    after = savedir.scan_any(live).slots[2]
    assert after.newest.label == "A"
    assert Path(after.a.meta_path).read_bytes() == waypoint_meta


@pytest.mark.parametrize("op", ["snapshot", "backup"])
def test_xbox_copies_keep_empty_folders(make_wgs_account, tmp_path, op):
    """A mirror restore deletes whatever the copy lacks, so an Xbox copy keeps the account
    folder exactly, empty GUID folders included."""
    live = make_wgs_account(tmp_path, [("Slot1Auto", '{"a":1}', "Run", "", 100, 1000)])
    empty = live / ("0" * 31 + "E")
    empty.mkdir()
    vault = _plain_vault(tmp_path)

    if op == "snapshot":
        entry = ops.snapshot_live(vault, live, reason="x")
    else:
        entry = ops.create_full_backup(vault, live, include_cache=False)

    copied = Path(entry.path) / empty.name
    assert copied.is_dir() and not any(copied.iterdir())






def test_xbox_repopulate_keeps_the_order_of_a_mixed_pair(make_wgs_account, tmp_path):
    """A 296-byte and a 280-byte member are ordered by index time, since only one has a meta
    time. Writing the meta time into the destination's index would flip the pair."""
    live = make_wgs_account(tmp_path, [
        ("Slot1Auto", '{"a":1}', "Run", "", 100, 4000, 7, 9000),
        ("Slot1Manual", '{"b":2}', "Run", "", 144, 5000),
    ], meta_len=296)
    b = savedir.scan_any(live).slots[1].b
    Path(b.meta_path).write_bytes(xbox_meta("Run", play=144, length=280))
    assert savedir.scan_any(live).slots[1].newest.label == "B"

    ops.repopulate_slot(_vault(tmp_path), live, 1, live, 3, allow_game_running=True)

    dest = savedir.scan_any(live).slots[3]
    assert dest.newest.label == "B"
    assert dest.b.data_mtime == pytest.approx(dest.a.data_mtime + 1)   # one second apart, now


def test_xbox_writes_never_move_an_index_time_back(make_wgs_account, tmp_path):
    """Records whose index time is ahead of the clock (another machine, a sync) are written
    past it, not back to now: promote past its own and the sibling's time, repopulate past
    both destination records."""
    ahead = time.time() + 100_000
    live = make_wgs_account(tmp_path, [
        ("Slot1Auto", '{"a":1}', "Run", "", 100, 1000),
        ("Slot1Manual", '{"b":2}', "Run", "", 144, 2000),
        ("Slot2Auto", '{"c":3}', "Old", "", 50, ahead, 9, 500),
        ("Slot2Manual", '{"d":4}', "Old", "", 60, ahead + 5, 9, 600),
    ])
    vault = _vault(tmp_path)

    ops.promote_member(vault, live, 2, 0, allow_game_running=True)
    slot2 = savedir.scan_any(live).slots[2]
    assert slot2.a.data_mtime >= ahead + 4.99 and slot2.newest.label == "A"   # not back to now

    ops.repopulate_slot(vault, live, 1, live, 2, allow_game_running=True)
    slot2 = savedir.scan_any(live).slots[2]
    assert min(slot2.a.data_mtime, slot2.b.data_mtime) >= ahead + 4.99
    assert slot2.newest.label == savedir.scan_any(live).slots[1].newest.label == "B"


def test_promote_xbox_refuses_a_meta_layout_it_cannot_write(make_wgs_account, tmp_path):
    """A meta longer than 360 bytes is read, but its save time is never written into: the
    promote stops before the snapshot or any write."""
    live = make_wgs_account(tmp_path, [
        ("Slot2Auto", '{"a":1}', "Run", "", 100, 1000),
        ("Slot2Manual", '{"b":2}', "Run", "", 144, 2000),
    ])
    a = savedir.scan_any(live).slots[2].a
    Path(a.meta_path).write_bytes(xbox_meta("Run", play=100, ts=1000) + bytes(40))
    index_before = (live / "containers.index").read_bytes()
    vault = _vault(tmp_path)

    with pytest.raises(ops.OperationError, match="cannot be rewritten"):
        ops.promote_member(vault, live, 2, 0, allow_game_running=True)

    assert vault.entries == [] and vault.read_oplog() == []
    assert (live / "containers.index").read_bytes() == index_before






def test_an_xbox_extract_does_not_inherit_trailing_index_bytes(make_wgs_account, tmp_path):
    """Bytes after the source index's records belong to that index, not to a subset of it."""
    live = make_wgs_account(tmp_path, [("Slot1Auto", '{"a":1}', "Run", "", 100, 1000)])
    (live / "containers.index").write_bytes((live / "containers.index").read_bytes() + b"\x00\x01")

    entry = ops.extract_slot(_plain_vault(tmp_path), live, 1)

    _header, records, trailer = msstore.split_index((Path(entry.path) / "containers.index").read_bytes())
    assert trailer == b"" and [ident for ident, _rec in records] == ["Slot1Auto"]


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
    assert after.slots[4].occupied              # a slot the snapshot lacks is left, not emptied


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
    """A source slot with only an Auto member fills both positions with it (never a mixed or
    half slot)."""
    live = make_wgs_account(tmp_path, [("Slot1Auto", '{"a":1}', "Only A", "", 1, 100)])
    vault = _vault(tmp_path)
    ops.repopulate_slot(vault, live, 1, live, 5, allow_game_running=True)
    after = savedir.scan_any(live)
    assert after.slots[5].a.save_name == after.slots[5].b.save_name == "Only A"


def test_restore_full_xbox_leaves_extra_slots(make_wgs_account, tmp_path):
    """Restoring a smaller backup leaves the live slots it lacks alone (F8: nothing is
    deleted)."""
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
    assert after.slots[2].a.save_name == "Drop 2" and after.slots[3].a.save_name == "Drop 3"


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
    index_before = (live / "containers.index").read_bytes()

    result = ops.promote_member(vault, live, 1, 0, allow_game_running=True)   # promote A again

    assert savedir.scan_any(live).slots[1].newest.label == "A"
    # Already the newest on every signal: nothing written, nothing snapshotted or logged.
    assert result.ok and result.snapshot_id is None and "already the newest" in result.detail
    assert (live / "containers.index").read_bytes() == index_before
    assert vault.entries == [] and vault.read_oplog() == []


def test_xbox_op_honors_game_running_guard(make_wgs_account, tmp_path, monkeypatch):
    """An Xbox write refuses when the game is detected running and no override is given."""
    live = make_wgs_account(tmp_path, [
        ("Slot1Auto", '{"a":1}', "A", "", 1, 100),
        ("Slot1Manual", '{"b":1}', "B", "", 1, 200),
    ])
    vault = _vault(tmp_path)
    monkeypatch.setattr(safety, "is_game_running", lambda: True)
    with pytest.raises(ops.GameRunningError):
        ops.promote_member(vault, live, 1, 0)   # no allow_game_running


def test_repopulate_invalid_slot_raises(make_wgs_account, tmp_path):
    live = make_wgs_account(tmp_path, [("Slot1Auto", '{"a":1}', "A", "", 1, 100)])
    vault = _vault(tmp_path)
    with pytest.raises(ops.OperationError):
        ops.repopulate_slot(vault, live, 1, live, 99, allow_game_running=True)


def test_extract_slot_xbox_scans_and_repopulates(make_wgs_account, tmp_path):
    """Extracting an Xbox slot yields a self-contained wgs folder that scans back with both
    members and can be repopulated into a live Xbox slot (the original bug: extract raised
    'no saves to extract' on wgs sources)."""
    live = make_wgs_account(tmp_path, [
        ("Slot2Auto", '{"a":1}', "Two A", "", 100, 1000),
        ("Slot2Manual", '{"b":2}', "Two B", "", 90, 900),
    ])
    vault = _vault(tmp_path)

    entry = ops.extract_slot(vault, live, 2, label="aside")
    assert entry.kind == "extract"
    with store_classic.entry_folder(vault, entry) as folder:
        ev = savedir.scan_any(folder)
        assert ev.slots[2].a.save_name == "Two A" and ev.slots[2].b.save_name == "Two B"

        # The extracted-aside slot repopulates back into a different live slot, verbatim.
        src_bytes = ev.slots[2].a.data_path.read_bytes()
        res = ops.repopulate_slot(vault, folder, 2, live, 5, allow_game_running=True)
    assert res.ok
    after = savedir.scan_any(live)
    assert after.slots[5].a.save_name == "Two A" and after.slots[5].b.save_name == "Two B"
    assert after.slots[5].a.data_path.read_bytes() == src_bytes


def test_extract_empty_xbox_slot_raises(make_wgs_account, tmp_path):
    live = make_wgs_account(tmp_path, [("Slot1Auto", '{"a":1}', "A", "", 1, 100)])
    vault = _vault(tmp_path)
    with pytest.raises(ops.OperationError):
        ops.extract_slot(vault, live, 7, label="")


# --- P6: no mixed slots, and the copied pair keeps its order --------------------------------


def test_a_one_member_xbox_source_fills_both_positions(make_wgs_account, tmp_path):
    live = make_wgs_account(tmp_path, [
        ("Slot1Auto", '{"a":1}', "Source", "", 100, 1000),
        ("Slot3Auto", '{"x":1}', "Old", "", 50, 500),
        ("Slot3Manual", '{"y":1}', "Old", "", 60, 600),
    ])
    vault = _vault(tmp_path)

    ops.repopulate_slot(vault, live, 1, live, 3, allow_game_running=True)

    slot = savedir.scan_any(live).slots[3]            # the old pair is gone from the slot ...
    assert slot.a.save_name == slot.b.save_name == "Source"
    assert any(e.kind == "preserve" for e in store_classic.list_entries(vault))   # ... not from the vault
    ops.repopulate_slot(vault, live, 1, live, 2, allow_game_running=True)   # an empty slot: fine
    assert savedir.scan_any(live).slots[2].a.save_name == "Source"




def test_a_one_member_xbox_source_may_join_its_own_playthrough(make_wgs_account, tmp_path):
    """The member left in place is the same run (same playthrough id): allowed, and the
    copied member is the one that loads."""
    live = make_wgs_account(tmp_path, [
        ("Slot1Auto", '{"a":1}', "Run", "", 300, 3000, 0xCAFE, 3000),
        ("Slot3Auto", '{"x":1}', "Run", "", 100, 1000, 0xCAFE, 1000),
        ("Slot3Manual", '{"y":1}', "Run", "", 200, 2000, 0xCAFE, 2000),
    ])
    ops.repopulate_slot(_vault(tmp_path), live, 1, live, 3, allow_game_running=True)

    slot = savedir.scan_any(live).slots[3]
    assert slot.newest.label == "A" and slot.a.info.total_play_time == 300
