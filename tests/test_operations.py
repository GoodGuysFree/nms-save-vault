"""Integration tests for the write operations, run entirely in a temp sandbox.

A throwaway "live" folder is built from the real save files (read-only source); the real
save directory is never modified.
"""
from __future__ import annotations

import logging
import os
import shutil
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from nms_save_vault.core import operations as ops
from nms_save_vault.core import store as vstore
from nms_save_vault.core.store import classic as store_classic
from nms_save_vault.core import catalog, formats, savedir, slotmap
from nms_save_vault.core.catalog import CatalogEntry, Vault

from .conftest import steam_save


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
    vstore.create_store(vault.root)          # live operations need the version store (D3)
    return live, vault


def _vault(tmp_path: Path) -> Vault:
    """A vault in store mode: live operations need the version store (D3)."""
    vault = Vault(tmp_path / "vault")
    vault.ensure()
    vstore.create_store(vault.root)
    return vault


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
    assert res.ok and res.op_id           # one live operation, undoable per slot
    assert [s.slot for s in savedir.scan(live).occupied_slots] == occupied_before


def test_back_up_folders_backs_up_each_folder_once_and_goes_on_past_a_failure(tmp_path, monkeypatch):
    first, second, empty = tmp_path / "st_1", tmp_path / "st_2", tmp_path / "st_3"
    steam_save(first, 1, 0, "Alpha", 3600, 0x11, 1_700_000_000)
    steam_save(first, 2, 0, "Beta", 7200, 0x12, 1_700_000_100)
    steam_save(second, 1, 0, "Gamma", 60, 0x21, 1_700_000_000)
    empty.mkdir()
    vault = _vault(tmp_path)
    real = ops.create_full_backup

    def flaky(vault, folder, **kw):
        if folder == second:
            raise ops.SourceChangedError("save.hg", vanished=False, live=True)
        return real(vault, folder, **kw)

    monkeypatch.setattr(ops, "create_full_backup", flaky)
    done = ops.back_up_folders(vault, [first, empty, second, first],
                               label_for=lambda f: f"label-{f.name}")
    assert [d.folder for d in done] == [first, empty, second]
    assert done[0].entry is not None and done[0].entry.label == "label-st_1"
    assert sorted(s.slot for s in done[0].entry.occupied_slots) == [1, 2]
    assert done[1].entry is None and done[1].skipped == "it holds no saves"
    assert done[2].entry is None and "changed while it was being copied" in done[2].error
    assert [e.id for e in store_classic.list_entries(vault)] == [done[0].entry.id]


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

    with store_classic.entry_folder(vault, entry) as folder:
        res = ops.repopulate_slot(vault, folder, src_slot, live, dest_slot, allow_game_running=True)
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
    """Xbox->Steam repopulate is refused (use the Organizer) before touching the live folder
    (and before the XXTEA path that would crash on Xbox plaintext meta)."""
    live, vault = sandbox
    xbox = _xbox_dir(tmp_path)
    with pytest.raises(ops.FeatureNotYetAvailableError, match="Organizer tab"):
        ops.repopulate_slot(vault, xbox, 1, live, 1, allow_game_running=True)


def test_restore_full_from_xbox_entry_is_gated(sandbox, tmp_path):
    live, vault = sandbox
    xbox = _xbox_dir(tmp_path)
    entry = CatalogEntry(id="imported-xbox-test", kind="imported", label="", path=str(xbox), created="now")
    with pytest.raises(ops.FeatureNotYetAvailableError):
        ops.restore_full(vault, entry, live, allow_game_running=True)


def _mk_vault_entry(vault: Vault, subdir: Path, entry_id: str, kind: str, created: str,
                    *, managed: bool = True, payload: bytes = b"x") -> Path:
    """Add a managed catalog entry to ``vault`` with a real data folder under ``subdir``."""
    folder = subdir / entry_id
    folder.mkdir(parents=True)
    (folder / "save.hg").write_bytes(payload)
    vault.upsert(CatalogEntry(id=entry_id, kind=kind, label=entry_id, path=str(folder),
                              created=created, managed=managed))
    return folder


def _source_vault(tmp_path: Path) -> Vault:
    """A synthetic 'copied' vault: one full backup + one snapshot (both managed)."""
    src = Vault(tmp_path / "src")
    src.ensure()
    _mk_vault_entry(src, src.backups_dir, "full-steam-20260601-000000", "full", "2026-06-01T00:00:00", payload=b"full")
    _mk_vault_entry(src, src.snapshots_dir, "snapshot-steam-20260602-000000", "snapshot", "2026-06-02T00:00:00", payload=b"snap")
    return src


def test_import_vault_copy_brings_files_in_and_is_idempotent(tmp_path):
    src = _source_vault(tmp_path)
    cur = Vault(tmp_path / "cur")
    cur.ensure()

    res = ops.import_vault(cur, src.root, copy_into_vault=True)
    assert res.ok and sorted(res.changed) == ["full-steam-20260601-000000", "snapshot-steam-20260602-000000"]

    # Files were copied into THIS vault, entries re-pointed and still managed.
    assert (cur.backups_dir / "full-steam-20260601-000000" / "save.hg").read_bytes() == b"full"
    assert (cur.snapshots_dir / "snapshot-steam-20260602-000000" / "save.hg").read_bytes() == b"snap"
    full = cur.get("full-steam-20260601-000000")
    assert Path(full.path) == cur.backups_dir / "full-steam-20260601-000000" and full.managed

    # Idempotent: a second import adds nothing.
    res2 = ops.import_vault(cur, src.root, copy_into_vault=True)
    assert res2.changed == []
    assert {e.id for e in cur.entries} == {"full-steam-20260601-000000", "snapshot-steam-20260602-000000"}


def test_import_vault_in_place_references_source_and_is_unmanaged(tmp_path):
    src = _source_vault(tmp_path)
    cur = Vault(tmp_path / "cur")
    cur.ensure()

    res = ops.import_vault(cur, src.root, copy_into_vault=False)
    assert res.ok and len(res.changed) == 2

    snap = cur.get("snapshot-steam-20260602-000000")
    # Points back at the source vault, and is NOT managed (its files belong to that vault).
    assert Path(snap.path) == src.snapshots_dir / "snapshot-steam-20260602-000000"
    assert snap.managed is False
    # Nothing was copied into the current vault.
    assert not (cur.snapshots_dir / "snapshot-steam-20260602-000000").exists()


def test_import_vault_preview_and_guards(tmp_path):
    src = _source_vault(tmp_path)
    cur = Vault(tmp_path / "cur")
    cur.ensure()

    pv = ops.preview_vault_import(cur, src.root)
    assert pv["total"] == 2 and len(pv["new"]) == 2 and pv["existing"] == []

    # After importing, preview reports them all as existing.
    ops.import_vault(cur, src.root, copy_into_vault=True)
    pv2 = ops.preview_vault_import(cur, src.root)
    assert len(pv2["new"]) == 0 and len(pv2["existing"]) == 2

    # Not a vault, and importing a vault into itself, are both refused.
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(ops.OperationError):
        ops.import_vault(cur, plain, copy_into_vault=True)
    with pytest.raises(ops.OperationError):
        ops.import_vault(cur, cur.root, copy_into_vault=True)




def _steam_pair(live: Path, a_file_time: float, b_file_time: float) -> None:
    """Slot 1: A saved at 1000, B at 2000 (meta times), with the given file times."""
    steam_save(live, 1, 0, "Run", 3600, 0x1234, 1000)
    steam_save(live, 1, 1, "Run", 3700, 0x1234, 2000)
    for member, when in ((0, a_file_time), (1, b_file_time)):
        fno = slotmap.slot_file_numbers(1)[member]
        for name in (slotmap.data_filename(fno), slotmap.meta_filename(fno)):
            os.utime(live / name, (when, when))














def test_snapshot_live_never_deletes_snapshots(make_wgs_account, tmp_path):
    """Every auto-snapshot is kept: some versions exist only in a snapshot, so deleting one
    loses that version for good. 25 is more than the 20 the old pruning kept."""
    steam = tmp_path / "steam_live"
    steam_save(steam, 1, 0, "Steam Run", 3600, 0x1234, 1_700_000_000)
    xbox = make_wgs_account(tmp_path / "xbox_live", [
        ("Slot1Auto", "x", "Xbox Run", "s", 3600, 1_700_000_000),
    ])
    vault = Vault(tmp_path / "vault")

    taken: list[str] = []
    for i in range(25):
        snap = ops.snapshot_live(vault, steam if i % 2 == 0 else xbox, reason=f"op {i}")
        taken.append(snap.id)
        assert {e.id for e in vault.entries} == set(taken)

    reloaded = Vault(vault.root).load()
    assert sorted(e.id for e in reloaded.entries) == sorted(taken)
    for entry in reloaded.entries:
        assert entry.kind == "snapshot" and entry.managed
        assert savedir.scan_any(entry.path).occupied_slots, f"{entry.id} lost its save files"


def test_failed_snapshot_leaves_no_orphan_folder_or_entry(tmp_path, monkeypatch):
    """A copy that dies part-way removes its own half-written folder, records nothing, and
    leaves every earlier snapshot alone."""
    live = tmp_path / "live"
    steam_save(live, 1, 0, "Steam Run", 3600, 0x1234, 1_700_000_000)
    vault = Vault(tmp_path / "vault")
    kept = ops.snapshot_live(vault, live, reason="good")

    real_copy = ops.safety.copy_and_hash
    copies = []

    def copy_then_fail(src, dst, *args, **kwargs):
        if copies:
            raise OSError("disk full")
        copies.append(src)
        return real_copy(src, dst, *args, **kwargs)

    monkeypatch.setattr(ops.safety, "copy_and_hash", copy_then_fail)
    with pytest.raises(OSError, match="disk full"):
        ops.snapshot_live(vault, live, reason="dies part-way")
    monkeypatch.undo()

    assert copies, "the failure must happen after something was written"
    assert [p.name for p in vault.snapshots_dir.iterdir()] == [kept.id]
    assert [e.id for e in Vault(vault.root).load().entries] == [kept.id]
    assert savedir.scan_any(kept.path).occupied_slots


class _BlockedCatalog:
    """Make the catalog write really fail: a folder stands where catalog.json is swapped in.
    On exit the file is put back exactly as it was."""

    def __init__(self, vault: Vault):
        self.path = vault.catalog_path

    def __enter__(self):
        self.saved = self.path.read_bytes()
        self.path.unlink()
        self.path.mkdir()
        (self.path / "keep").write_bytes(b"x")
        return self

    def __exit__(self, *exc):
        shutil.rmtree(self.path)
        self.path.write_bytes(self.saved)


def test_a_snapshot_the_catalog_cannot_record_is_removed(tmp_path):
    """The catalog write really fails: the copied snapshot goes, and the in-memory catalog
    does not keep an entry the file never got."""
    live = tmp_path / "live"
    steam_save(live, 1, 0, "Steam Run", 3600, 0x1234, 1_700_000_000)
    vault = Vault(tmp_path / "vault")
    kept = ops.snapshot_live(vault, live, reason="good")

    with _BlockedCatalog(vault), pytest.raises(OSError):
        ops.snapshot_live(vault, live, reason="cannot be catalogued")

    assert [e.id for e in vault.entries] == [kept.id]
    assert [p.name for p in vault.snapshots_dir.iterdir()] == [kept.id]
    assert [e.id for e in Vault(vault.root).load().entries] == [kept.id]


class FrozenClock(datetime):
    """``datetime`` whose now() is fixed, so the next entry id is known in advance."""

    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 17, 12, 0, 0)


def test_a_snapshot_never_reuses_a_leftover_folder(tmp_path, monkeypatch):
    """A folder left behind by an earlier failure may hold anything, so a new snapshot goes
    into a folder of its own and the leftover is not touched."""
    monkeypatch.setattr(catalog, "datetime", FrozenClock)
    live = tmp_path / "live"
    steam_save(live, 1, 0, "Steam Run", 3600, 0x1234, 1_700_000_000)
    vault = Vault(tmp_path / "vault")
    vault.ensure()
    leftover = vault.snapshots_dir / "snapshot-steam-20261017-120000"
    leftover.mkdir()
    (leftover / "save.hg").write_bytes(b"half")

    snap = ops.snapshot_live(vault, live, reason="after a crash")

    assert snap.id == "snapshot-steam-20261017-120000-2"
    assert [(p.name, p.read_bytes()) for p in leftover.iterdir()] == [("save.hg", b"half")]
    assert savedir.scan_any(snap.path).occupied_slots


def test_an_extract_never_reuses_a_leftover_folder(tmp_path, monkeypatch):
    """Same protection for the other managed kinds: an extract that found a leftover folder
    under its id would have mixed into it, and on 'no saves' deleted it."""
    monkeypatch.setattr(catalog, "datetime", FrozenClock)
    live = tmp_path / "live"
    steam_save(live, 1, 0, "Steam Run", 3600, 0x1234, 1_700_000_000)
    vault = Vault(tmp_path / "vault")
    vault.ensure()
    leftover = vault.extracts_dir / "extract-steam-20261017-120000"
    leftover.mkdir()
    (leftover / "save.hg").write_bytes(b"half")

    entry = ops.extract_slot(vault, live, 1)

    assert entry.id == "extract-steam-20261017-120000-2"
    assert [(p.name, p.read_bytes()) for p in leftover.iterdir()] == [("save.hg", b"half")]


def test_a_failed_cleanup_is_logged_and_the_original_error_raised(tmp_path, monkeypatch, caplog):
    live = tmp_path / "live"
    steam_save(live, 1, 0, "Steam Run", 3600, 0x1234, 1_700_000_000)
    vault = Vault(tmp_path / "vault")

    def copy_fails(*args, **kwargs):
        raise OSError("disk full")

    def rmtree_fails(path, *args, **kwargs):
        raise PermissionError("folder is locked")

    monkeypatch.setattr(ops.safety, "copy_and_hash", copy_fails)
    monkeypatch.setattr(ops.shutil, "rmtree", rmtree_fails)
    with caplog.at_level(logging.WARNING, logger=ops.__name__):
        with pytest.raises(OSError, match="disk full"):
            ops.snapshot_live(vault, live, reason="dies")
    monkeypatch.undo()

    [left] = list(vault.snapshots_dir.iterdir())
    [record] = [r for r in caplog.records if r.name == ops.__name__]
    assert record.levelno == logging.WARNING
    assert str(left) in record.getMessage() and "folder is locked" in record.getMessage()


def test_a_snapshot_is_refused_when_the_vault_drive_is_too_full(tmp_path, monkeypatch):
    live = tmp_path / "live"
    steam_save(live, 1, 0, "Steam Run", 3600, 0x1234, 1_700_000_000)
    vault = Vault(tmp_path / "vault")
    needed = sum(p.stat().st_size for p in live.glob("*.hg")) + ops.SPACE_MARGIN

    monkeypatch.setattr(ops.shutil, "disk_usage", lambda path: SimpleNamespace(free=needed - 1))
    with pytest.raises(ops.OperationError, match="free space"):
        ops.snapshot_live(vault, live, reason="no room")
    assert list(vault.snapshots_dir.iterdir()) == [] and vault.entries == []

    monkeypatch.setattr(ops.shutil, "disk_usage", lambda path: SimpleNamespace(free=needed))
    assert ops.snapshot_live(vault, live, reason="just enough room").kind == "snapshot"


_MANAGED_COPIES = {
    "full backup": lambda vault, live: ops.create_full_backup(vault, live, label="x"),
    "extract": lambda vault, live: ops.extract_slot(vault, live, 1),
    "copied import": lambda vault, live: ops.import_backup(vault, live, copy_into_vault=True),
}


@pytest.mark.parametrize("make", list(_MANAGED_COPIES.values()), ids=list(_MANAGED_COPIES))
def test_a_failed_vault_copy_leaves_nothing_behind(tmp_path, monkeypatch, make):
    """Every managed copy goes through the same guard: a copy that dies part-way leaves no
    folder and no catalog entry."""
    live = tmp_path / "live"
    steam_save(live, 1, 0, "Run", 3600, 0x1234, 1_700_000_000)
    vault = Vault(tmp_path / "vault")
    real_copy = ops.safety.copy_and_hash
    wrote = []

    def copy_then_fail(src, dst, *args, **kwargs):
        if wrote:
            raise OSError("disk full")
        wrote.append(dst)
        return real_copy(src, dst, *args, **kwargs)

    monkeypatch.setattr(ops.safety, "copy_and_hash", copy_then_fail)
    with pytest.raises(OSError, match="disk full"):
        make(vault, live)
    monkeypatch.undo()

    assert wrote, "the failure must happen after something was written"
    assert [p for d in (vault.backups_dir, vault.extracts_dir) for p in d.iterdir()] == []
    assert vault.entries == [] and Vault(vault.root).load().entries == []


_EVERY_COPY = [lambda vault, live: ops.snapshot_live(vault, live, reason="x"), *_MANAGED_COPIES.values()]
_EVERY_COPY_IDS = ["snapshot", *_MANAGED_COPIES]


def _flip_last_byte(path: Path) -> None:
    data = bytearray(path.read_bytes())
    data[-1] ^= 0xFF
    path.write_bytes(bytes(data))


@pytest.mark.parametrize("make", _EVERY_COPY, ids=_EVERY_COPY_IDS)
def test_a_corrupted_vault_copy_is_caught_and_removed(tmp_path, monkeypatch, make):
    """Every copy is checked against the sha256 of what was read from its source; a copy
    that came out different is removed and nothing is catalogued."""
    live = tmp_path / "live"
    steam_save(live, 1, 0, "Run", 3600, 0x1234, 1_700_000_000)
    vault = Vault(tmp_path / "vault")
    real_copy = ops.safety.copy_and_hash

    def copy_corrupting(src, dst, *args, **kwargs):
        digest = real_copy(src, dst, *args, **kwargs)
        _flip_last_byte(Path(dst))
        return digest

    monkeypatch.setattr(ops.safety, "copy_and_hash", copy_corrupting)
    with pytest.raises(ops.ValidationError, match="does not match the original"):
        make(vault, live)
    monkeypatch.undo()

    folders = (vault.snapshots_dir, vault.backups_dir, vault.extracts_dir)
    assert [p for d in folders for p in d.iterdir()] == []
    assert vault.entries == [] and Vault(vault.root).load().entries == []


@pytest.mark.parametrize("make", list(_MANAGED_COPIES.values()), ids=list(_MANAGED_COPIES))
def test_every_vault_copy_checks_free_space(tmp_path, monkeypatch, make):
    live = tmp_path / "live"
    steam_save(live, 1, 0, "Run", 3600, 0x1234, 1_700_000_000)
    vault = Vault(tmp_path / "vault")
    monkeypatch.setattr(ops.shutil, "disk_usage", lambda path: SimpleNamespace(free=ops.SPACE_MARGIN))

    with pytest.raises(ops.OperationError, match="free space"):
        make(vault, live)
    assert vault.entries == []


def test_a_save_file_vanishing_before_the_copy_is_reported_plainly(tmp_path, monkeypatch):
    """Listed, then gone before it could be measured: a clear OperationError, and no vault
    folder was ever made."""
    live = tmp_path / "live"
    steam_save(live, 1, 0, "Run", 3600, 0x1234, 1_700_000_000)
    vault = Vault(tmp_path / "vault")
    listed = ops._save_state_files(live) + [live / "save9.hg"]
    monkeypatch.setattr(ops, "_save_state_files", lambda src: listed)

    with pytest.raises(ops.OperationError, match="save9.hg disappeared"):
        ops.snapshot_live(vault, live, reason="x")
    assert list(vault.snapshots_dir.iterdir()) == [] and vault.entries == []


def test_a_save_file_vanishing_during_the_copy_is_reported_plainly(tmp_path, monkeypatch):
    live = tmp_path / "live"
    steam_save(live, 1, 0, "Run", 3600, 0x1234, 1_700_000_000)
    vault = Vault(tmp_path / "vault")

    def game_deletes_it(src, dst, *args, **kwargs):
        Path(src).unlink()
        raise FileNotFoundError(2, "No such file or directory", str(src))

    monkeypatch.setattr(ops.safety, "copy_and_hash", game_deletes_it)
    with pytest.raises(ops.OperationError, match="disappeared while it was being copied"):
        ops.snapshot_live(vault, live, reason="x")
    monkeypatch.undo()
    assert list(vault.snapshots_dir.iterdir()) == [] and vault.entries == []


@pytest.mark.parametrize(("make", "advice"), [
    (lambda vault, live: ops.snapshot_live(vault, live, reason="x"), "Close the game"),
    (_MANAGED_COPIES["copied import"], "source folder is still there"),
], ids=["live folder", "other folder"])
def test_a_source_that_changes_while_it_is_read_is_caught(tmp_path, monkeypatch, make, advice):
    """The copy itself is consistent with what was read, but the source moved meanwhile:
    caught without reading the source twice, with advice that fits where it came from."""
    live = tmp_path / "live"
    steam_save(live, 1, 0, "Run", 3600, 0x1234, 1_700_000_000)
    vault = Vault(tmp_path / "vault")
    real_copy = ops.safety.copy_and_hash

    def copy_then_source_changes(src, dst, *args, **kwargs):
        digest = real_copy(src, dst, *args, **kwargs)
        with open(src, "ab") as f:                 # the game writes the save meanwhile
            f.write(b"x")
        return digest

    monkeypatch.setattr(ops.safety, "copy_and_hash", copy_then_source_changes)
    with pytest.raises(ops.SourceChangedError, match=f"changed while it was being copied.*{advice}"):
        make(vault, live)
    monkeypatch.undo()
    assert vault.entries == []


def test_a_catalog_removal_that_cannot_be_saved_is_not_half_done(tmp_path):
    """Vault.remove mirrors upsert: if the catalog file cannot be written, the entry stays
    in memory as it stays in the file."""
    live = tmp_path / "live"
    steam_save(live, 1, 0, "Run", 3600, 0x1234, 1_700_000_000)
    vault = Vault(tmp_path / "vault")
    kept = ops.snapshot_live(vault, live, reason="x")

    with _BlockedCatalog(vault), pytest.raises(OSError):
        vault.remove(kept.id)

    assert [e.id for e in vault.entries] == [kept.id]
    assert [e.id for e in Vault(vault.root).load().entries] == [kept.id]


# --- Steam Cloud: mtimes on writes into the live folder ----------------------
#
# Steam compares each local file against its own manifest. A file written with the
# backup's original mtime looks OLDER than the cloud copy, and Steam can overwrite the
# restore on the next launch -- so under Cloud the live file carries the write time.


def _touch_slot(folder: Path, slot: int, when: float) -> None:
    for fno in slotmap.slot_file_numbers(slot):
        for name in (slotmap.data_filename(fno), slotmap.meta_filename(fno)):
            p = folder / name
            if p.is_file():
                os.utime(p, (when, when))


def test_repopulate_under_cloud_stamps_the_write_time(sandbox):
    live, vault = sandbox
    occ = [s.slot for s in savedir.scan(live).occupied_slots]
    src_slot, dest_slot = occ[0], occ[1]
    old = time.time() - 30 * 86400
    _touch_slot(live, src_slot, old)
    (live / formats.STEAM_AUTOCLOUD).write_text("", encoding="utf-8")

    started = time.time()
    ops.repopulate_slot(vault, live, src_slot, live, dest_slot, allow_game_running=True)

    fno = slotmap.slot_file_numbers(dest_slot)[0]
    assert (live / slotmap.data_filename(fno)).stat().st_mtime >= started - 1




def test_restore_under_cloud_stamps_the_write_time(sandbox):
    live, vault = sandbox
    victim = [s.slot for s in savedir.scan(live).occupied_slots][0]
    _touch_slot(live, victim, time.time() - 30 * 86400)
    entry = ops.create_full_backup(vault, live, label="aged")
    (live / formats.STEAM_AUTOCLOUD).write_text("", encoding="utf-8")

    started = time.time()
    ops.restore_full(vault, entry, live, allow_game_running=True)

    fno = slotmap.slot_file_numbers(victim)[0]
    assert (live / slotmap.data_filename(fno)).stat().st_mtime >= started - 1


# --- P6: no mixed slots, and the copied pair keeps its order --------------------------------


def _steam_slot(folder: Path, slot: int, members: tuple[int, ...], name: str, ident: int, base: int) -> None:
    for m in members:
        steam_save(folder, slot, m, name, 100 + m, ident, base + m)




def test_a_two_member_steam_source_replaces_both_and_keeps_its_order(tmp_path):
    """Under Steam Cloud the file times are the write times: the source's newer member is
    written last, so both clocks still load it."""
    live = tmp_path / "live"
    for m, ts in ((0, 2000), (1, 1000)):                    # here A is the newer
        steam_save(live, 1, m, "Source", 100 + m, 0x1, ts)
    _steam_slot(live, 3, (0, 1), "Old", 0x3, 500)
    (live / formats.STEAM_AUTOCLOUD).write_text("cloud", "utf-8")
    vault = _vault(tmp_path)

    ops.repopulate_slot(vault, live, 1, live, 3, allow_game_running=True)

    dest = savedir.scan(live).slots[3]
    assert dest.a.save_name == dest.b.save_name == "Source"
    assert dest.newest.label == "A" and dest.a.data_mtime - dest.b.data_mtime >= 1   # no tie


def test_a_one_member_steam_source_may_join_its_own_playthrough(tmp_path):
    live = tmp_path / "live"
    steam_save(live, 1, 0, "Run", 300, 0xCAFE, 3000)
    steam_save(live, 3, 0, "Run", 100, 0xCAFE, 1000)
    steam_save(live, 3, 1, "Run", 200, 0xCAFE, 2000)

    ops.repopulate_slot(_vault(tmp_path), live, 1, live, 3, allow_game_running=True)

    slot = savedir.scan(live).slots[3]
    assert slot.newest.label == "A" and slot.a.info.total_play_time == 300


# --- the classic operations are live operations (D5 4.1) ---------------------------------------


def test_promote_that_would_change_nothing_writes_nothing(tmp_path):
    live = tmp_path / "live"
    steam_save(live, 1, 0, "Run", 3600, 0x1234, 1000)
    steam_save(live, 1, 1, "Run", 3700, 0x1234, 2000)
    vault = _vault(tmp_path)
    ops.promote_member(vault, live, 1, 0, allow_game_running=True)        # A loads now
    before = {p.name: (p.read_bytes(), p.stat().st_mtime) for p in live.iterdir()}

    result = ops.promote_member(vault, live, 1, 0, allow_game_running=True)

    assert result.ok and result.op_id is None and "already the newest" in result.detail
    assert {p.name: (p.read_bytes(), p.stat().st_mtime) for p in live.iterdir()} == before


def test_promote_stamps_the_write_time_and_keeps_the_old_pair_in_the_vault(tmp_path):
    live = tmp_path / "live"
    steam_save(live, 1, 0, "Run", 3600, 0x1234, 1000)
    steam_save(live, 1, 1, "Run", 3700, 0x1234, 2000)
    started = int(time.time())

    result = ops.promote_member(_vault(tmp_path), live, 1, 0, allow_game_running=True)

    a = savedir.scan(live).slots[1].a
    assert result.op_id and savedir.scan(live).slots[1].newest.label == "A"
    assert a.timestamp >= started and a.data_mtime >= started


def test_repopulate_stamps_the_write_time_even_without_cloud(sandbox):
    live, vault = sandbox
    occ = [s.slot for s in savedir.scan(live).occupied_slots]
    src_slot, dest_slot = occ[0], occ[1]
    _touch_slot(live, src_slot, time.time() - 30 * 86400)
    (live / formats.STEAM_AUTOCLOUD).unlink(missing_ok=True)
    started = time.time()

    ops.repopulate_slot(vault, live, src_slot, live, dest_slot, allow_game_running=True)

    fno = slotmap.slot_file_numbers(dest_slot)[0]
    assert (live / slotmap.data_filename(fno)).stat().st_mtime >= started - 1


def test_a_one_member_steam_source_fills_both_positions_and_keeps_the_old_pair(tmp_path):
    """The old member is never left beside the copy (P6): both positions get the copy, and
    what the slot held is in the vault."""
    live = tmp_path / "live"
    _steam_slot(live, 1, (0,), "Source", 0x1, 1000)
    _steam_slot(live, 3, (0, 1), "Old", 0x3, 500)
    vault = _vault(tmp_path)

    result = ops.repopulate_slot(vault, live, 1, live, 3, allow_game_running=True)

    dest = savedir.scan(live).slots[3]
    assert dest.a.save_name == dest.b.save_name == "Source" and result.op_id
    assert any(e.kind == "preserve" for e in store_classic.list_entries(vault))
