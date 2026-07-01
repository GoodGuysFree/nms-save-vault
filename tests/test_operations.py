"""Integration tests for the write operations, run entirely in a temp sandbox.

A throwaway "live" folder is built from the real save files (read-only source); the real
save directory is never modified.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from nms_save_vault.core import operations as ops
from nms_save_vault.core import savedir, slotmap
from nms_save_vault.core.catalog import CatalogEntry, Vault


@pytest.fixture
def sandbox(live_save_dir, tmp_path):
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


def _data_bytes(folder: Path, slot: int, member: int = 0) -> bytes:
    fno = slotmap.slot_file_numbers(slot)[member]
    return (folder / slotmap.data_filename(fno)).read_bytes()


def test_full_backup_and_restore_mirror(sandbox):
    live, vault = sandbox
    occupied_before = [s.slot for s in savedir.scan(live).occupied_slots]
    entry = ops.create_full_backup(vault, live, label="test", include_cache=True)
    assert entry.kind == "full" and entry.occupied_slots

    # Wipe one slot, then restore.
    victim = occupied_before[0]
    for fno in slotmap.slot_file_numbers(victim):
        for name in (slotmap.data_filename(fno), slotmap.meta_filename(fno)):
            (live / name).unlink(missing_ok=True)
    assert victim not in [s.slot for s in savedir.scan(live).occupied_slots]

    res = ops.restore_full(vault, entry, live, allow_game_running=True)
    assert res.ok and res.snapshot_id
    assert [s.slot for s in savedir.scan(live).occupied_slots] == occupied_before


def test_repopulate_cross_slot_rekeys_and_preserves_data(sandbox):
    live, vault = sandbox
    view = savedir.scan(live)
    occ = [s.slot for s in view.occupied_slots]
    src_slot, dest_slot = occ[0], occ[1]
    src_data_a = _data_bytes(live, src_slot, 0)
    src_name = view.slots[src_slot].a.save_name

    res = ops.repopulate_slot(vault, live, src_slot, live, dest_slot, allow_game_running=True)
    assert res.ok

    after = savedir.scan(live)
    # data copied verbatim
    assert _data_bytes(live, dest_slot, 0) == src_data_a
    # meta now decrypts with the DEST slot's ordinal and carries the source's name
    assert after.slots[dest_slot].a.valid
    assert after.slots[dest_slot].a.ordinal_used == slotmap.storage_ordinal(
        slotmap.slot_file_numbers(dest_slot)[0]
    )
    assert after.slots[dest_slot].a.save_name == src_name


def test_promote_makes_older_member_newest(sandbox):
    live, vault = sandbox
    view = savedir.scan(live)
    cand = [sv for sv in view.slots.values() if sv.a.valid and sv.b.valid and sv.a.timestamp != sv.b.timestamp]
    if not cand:
        pytest.skip("no slot with two valid, differently-timed members")
    sv = cand[0]
    older = min(sv.members, key=lambda m: m.timestamp)
    member_index = 0 if older.label == "A" else 1

    res = ops.promote_member(vault, live, sv.slot, member_index, allow_game_running=True)
    assert res.ok
    assert savedir.scan(live).slots[sv.slot].newest.label == older.label


def test_extract_then_repopulate_from_extract(sandbox):
    live, vault = sandbox
    view = savedir.scan(live)
    occ = [s.slot for s in view.occupied_slots]
    src_slot, dest_slot = occ[0], occ[1]
    src_name = view.slots[src_slot].a.save_name

    entry = ops.extract_slot(vault, live, src_slot, label="x")
    assert entry.kind == "extract"

    res = ops.repopulate_slot(vault, Path(entry.path), src_slot, live, dest_slot, allow_game_running=True)
    assert res.ok
    assert savedir.scan(live).slots[dest_slot].a.save_name == src_name


def test_undo_restores_previous_slot(sandbox):
    live, vault = sandbox
    view = savedir.scan(live)
    occ = [s.slot for s in view.occupied_slots]
    src, dest = occ[0], occ[1]
    name_src = view.slots[src].display_name
    name_dest_before = view.slots[dest].display_name
    if name_src == name_dest_before:
        pytest.skip("need slots with distinct names")

    ops.repopulate_slot(vault, live, src, live, dest, allow_game_running=True)
    assert savedir.scan(live).slots[dest].display_name == name_src

    ops.undo_last(vault, live, allow_game_running=True)
    assert savedir.scan(live).slots[dest].display_name == name_dest_before


def test_import_inplace(sandbox, live_save_dir):
    _live, vault = sandbox
    entry = ops.import_backup(vault, live_save_dir, label="real", copy_into_vault=False)
    assert entry.kind == "inplace" and not entry.managed
    assert vault.get(entry.id) is not None


def _xbox_dir(tmp_path: Path) -> Path:
    """A minimal folder that ``platform_of`` classifies as Xbox (has containers.index)."""
    d = tmp_path / "wgs_src"
    d.mkdir()
    (d / "containers.index").write_bytes(b"")
    return d


def test_repopulate_from_xbox_source_is_gated(sandbox, tmp_path):
    """Xbox->Steam repopulate is refused as 'coming soon' before touching the live folder
    (and before the XXTEA path that would crash on Xbox plaintext meta)."""
    live, vault = sandbox
    xbox = _xbox_dir(tmp_path)
    with pytest.raises(ops.FeatureNotYetAvailableError):
        ops.repopulate_slot(vault, xbox, 1, live, 1, allow_game_running=True)


def test_restore_full_from_xbox_entry_is_gated(sandbox, tmp_path):
    live, vault = sandbox
    xbox = _xbox_dir(tmp_path)
    entry = CatalogEntry(id="imported-xbox-test", kind="imported", label="", path=str(xbox), created="now")
    with pytest.raises(ops.FeatureNotYetAvailableError):
        ops.restore_full(vault, entry, live, allow_game_running=True)


def test_prune_snapshots_is_chronological_across_platforms(tmp_path):
    """Regression: snapshot ids embed the platform ('snapshot-<platform>-<stamp>'), so a
    naive id sort groups every Steam snapshot before every Xbox one and would prune the wrong
    end -- including a just-created snapshot still referenced by the oplog, silently defeating
    undo. Pruning must be by creation time."""
    vault = Vault(tmp_path / "vault")
    vault.ensure()

    def add(entry_id: str, created: str) -> Path:
        d = vault.snapshots_dir / entry_id
        d.mkdir(parents=True)
        vault.upsert(CatalogEntry(id=entry_id, kind="snapshot", label="", path=str(d), created=created))
        return d

    # 20 older Xbox snapshots + 1 Steam snapshot created most recently (21 > retention of 20).
    oldest = add("snapshot-xbox-20260601-000000", "2026-06-01T00:00:00")
    for day in range(2, 21):
        add(f"snapshot-xbox-202606{day:02d}-000000", f"2026-06-{day:02d}T00:00:00")
    newest = add("snapshot-steam-20260701-120000", "2026-07-01T12:00:00")

    ops._prune_snapshots(vault)

    ids = {e.id for e in vault.entries}
    assert len(ids) == ops.SNAPSHOT_RETENTION
    # The most recent snapshot survives; the single oldest is the one pruned.
    assert newest.name in ids and newest.is_dir()
    assert oldest.name not in ids and not oldest.exists()
