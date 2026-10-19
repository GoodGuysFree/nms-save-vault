"""Version store B-10: the classic operations in store mode, the mode check and the vault lock
on every vault write, and importing vaults (D3 2.10, T12, T22 phase-2 part)."""
from __future__ import annotations

import hashlib
import json
import shutil

import pytest

from nms_save_vault import cli
from nms_save_vault.core import operations, safety, savedir
from nms_save_vault.core import state as appstate
from nms_save_vault.core import store as vstore
from nms_save_vault.core.catalog import Vault
from nms_save_vault.core.organizer.model import LiveTarget
from nms_save_vault.core.store import classic
from nms_save_vault.core.store.d1 import LiveMember

from .conftest import steam_save
from .store_helpers import BIG, IDENT, STEAM_ACCOUNT, steam_live


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache-home"))
    monkeypatch.setattr(safety, "is_game_running", lambda: False)
    monkeypatch.setattr(vstore.versionstore.discover, "all_live_paths", lambda *a, **k: [])


def _world(root, store_mode: bool):
    live = steam_live(root / "NMS")
    vault = Vault(root / "vault")
    vault.ensure()
    if store_mode:
        vstore.create_store(vault.root)
    return vault, live


def _saves(folder) -> dict[str, str]:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(folder.glob("*.hg"))}


def _play(live, slot=1, member=0, play=9999, extra="changed"):
    steam_save(live, slot, member, "Main", play, IDENT, 1_790_100_000, extra=BIG + extra)


def test_the_cli_backup_message_has_no_empty_path_in_store_mode(tmp_path, capsys):
    vault, live = _world(tmp_path, store_mode=True)
    assert cli.main(["--live", str(live), "--vault", str(vault.root), "backup", "--no-cache"]) == 0
    line = next(ln for ln in capsys.readouterr().out.splitlines()
                if ln.startswith("created full backup"))
    assert line.endswith("in the vault") and " at" not in line.split("slots)")[1]


def test_backups_snapshots_and_extracts_become_store_entries(tmp_path):
    vault, live = _world(tmp_path, store_mode=True)
    full = operations.create_full_backup(vault, live, label="mine", warnings=[])
    extract = operations.extract_slot(vault, live, 2, warnings=[], live_dirs=[live])
    snap = operations.snapshot_live(vault, live, "test")
    store = classic.store_of(vault)
    assert [store.entry(e.id).kind for e in (full, extract, snap)] == ["full", "extract", "snapshot"]
    assert store.entry(full.id).scope["cache"] == "included"
    assert store.entry(extract.id).scope["slots"] == [2]
    assert full.path == "" and vault.entries == []          # nothing in the old catalog
    assert not any((vault.backups_dir).iterdir()) and not any(vault.snapshots_dir.iterdir())
    ops = [(r["op"], r["entry_id"]) for r in vault.read_oplog()]
    assert ops == [("full_backup", full.id), ("extract_slot", extract.id)]


def _data(folder) -> dict[str, str]:
    """The save data files (the metas are stamped when a save is put live)."""
    return {n: h for n, h in _saves(folder).items() if not n.startswith("mf_")}


def test_classic_ops_through_store_entries(tmp_path):
    """T12: the classic operations on a store-mode vault put the saves back, one live
    operation each; a restore never leaves a half slot or deletes one."""
    vault, live = _world(tmp_path, True)
    full = operations.create_full_backup(vault, live, warnings=[])
    extract = operations.extract_slot(vault, live, 2, warnings=[], live_dirs=[live])
    before = _data(live)
    _play(live, 1)
    _play(live, 2, extra="two")
    operations.restore_full(vault, full, live)
    after_full = _data(live)
    _play(live, 2, extra="again")
    operations.restore_entry(vault, extract, live)
    after_extract = _data(live)
    operations.repopulate_slot(vault, live, 1, live, 5)
    operations.undo_last(vault, live)                      # the newest snapshot: before the repopulate
    after_undo = _data(live)
    assert full.path == "" and classic.find_entry(vault, full.id).kind == "full"
    for state in (after_full, after_extract, after_undo):
        assert all(state[n] == h for n, h in before.items())
    assert "save9.hg" in after_undo                        # slot 5 is not emptied (F8)


def test_a_preserve_entry_restores_only_its_slot(tmp_path):
    vault, live = _world(tmp_path, store_mode=True)
    store = classic.store_of(vault)
    loc = vstore.StoreLocator(store, live_dir=lambda t: live)
    vid = hashlib.sha256((live / "save.hg").read_bytes()).hexdigest()
    kept = loc.preserve([LiveMember(LiveTarget("steam", live.name, 1), "A", vid)],
                        op_id="op-1", reason="place")
    original = _saves(live)
    _play(live, 1)
    _play(live, 2, extra="two")
    slot2 = _saves(live)["save3.hg"]
    entry = classic.find_entry(vault, kept[0].ref.key)
    with pytest.raises(operations.OperationError, match="single-slot"):
        operations.restore_full(vault, entry, live)
    operations.restore_entry(vault, entry, live)
    now = _saves(live)
    assert now["save.hg"] == original["save.hg"] and now["save3.hg"] == slot2




def test_imports_in_store_mode(tmp_path):
    vault, live = _world(tmp_path, store_mode=True)
    copy = tmp_path / "hand copy"
    shutil.copytree(live, copy)
    copied = operations.import_backup(vault, copy, copy_into_vault=True, warnings=[])
    assert classic.store_of(vault).entry(copied.id).kind == "imported"
    in_place = operations.import_backup(vault, copy, copy_into_vault=False, warnings=[])
    assert vault.get(in_place.id).kind == "inplace"

    other, other_live = _world(tmp_path / "other", store_mode=False)
    old = operations.create_full_backup(other, other_live, warnings=[])
    result = operations.import_vault(vault, other.root, copy_into_vault=True)
    store = classic.store_of(vault)
    assert result.changed == [old.id] and store.entry(old.id).origin == {"imported_entry": old.id}
    assert store.entry(old.id).created_local == old.created
    assert operations.import_vault(vault, other.root, copy_into_vault=True).changed == []
    vault.save()                        # make it look like a vault to the legacy side too
    with pytest.raises(operations.OperationError, match="Upgrade the vault first"):
        operations.import_vault(other, vault.root, copy_into_vault=True)
    with pytest.raises(operations.OperationError, match="Upgrade the vault first"):
        operations.import_vault(other, vault.root, copy_into_vault=False)


def test_every_vault_write_is_refused_while_upgrading(tmp_path):
    vault, live = _world(tmp_path, store_mode=True)
    (vault.root / "store" / "state" / "upgrade.json").write_text("{}", "ascii")
    before = _saves(live)
    for write in (lambda: operations.snapshot_live(vault, live, "x"),
                  lambda: operations.create_full_backup(vault, live, warnings=[]),
                  lambda: operations.import_backup(vault, live, copy_into_vault=False,
                                                   warnings=[]),
                  ):
        with pytest.raises(vstore.StoreNotUpgraded, match="being upgraded"):
            write()
    with pytest.raises(operations.OperationError, match="Upgrade the vault first"):
        operations.repopulate_slot(vault, live, 1, live, 5)
    assert _saves(live) == before and not vault.catalog_path.exists()


def test_catalog_writes_take_the_vault_lock_in_store_mode(tmp_path, monkeypatch):
    vault, live = _world(tmp_path, store_mode=True)
    lock = vault.root / "organizer" / ".lock"
    seen = []
    real_upsert, real_append = Vault.upsert, Vault.append_oplog

    def held() -> bool:
        try:
            with safety.exclusive_lock(lock, timeout=0):
                return False
        except RuntimeError:
            return True

    monkeypatch.setattr(Vault, "upsert", lambda self, e: seen.append(held()) or real_upsert(self, e))
    monkeypatch.setattr(Vault, "append_oplog",
                        lambda self, r: seen.append(held()) or real_append(self, r))
    operations.import_backup(vault, live, copy_into_vault=False, warnings=[])
    assert seen == [True, True]


def test_the_cli_reads_store_entries(tmp_path, monkeypatch, capsys):
    vault, live = _world(tmp_path, store_mode=True)
    monkeypatch.setattr(appstate, "load", lambda *a, **k: None)
    base = ["--vault", str(vault.root), "--live", str(live)]
    full = operations.create_full_backup(vault, live, label="mine", warnings=[])
    assert cli.main(base + ["list"]) == 0 and full.id in capsys.readouterr().out
    assert cli.main(base + ["show", full.id]) == 0
    assert "(in the version store)" in capsys.readouterr().out
    original = _saves(live)
    _play(live, 1)
    assert cli.main(base + ["restore", full.id]) == 0
    now = _data(live)                                   # slot 2 also gets its second position
    assert all(now[n] == h for n, h in original.items() if not n.startswith("mf_"))
    assert cli.main(base + ["repopulate", "--from", full.id, "--src-slot", "1",
                            "--to-slot", "6", "--yes", "--confirm", "TWIN_LIVE"]) == 0
    assert _saves(live)["save11.hg"] == original["save.hg"]
    assert cli.main(base + ["extract", "2", "--from", full.id]) == 0
    capsys.readouterr()
    store = classic.store_of(vault)
    extract = next(e for e in store.entries() if e.kind == "extract")
    assert extract.source == {"type": "entry", "entry": full.id}
    assert [s.slot for s in savedir.scan(live).occupied_slots] == [1, 2, 6]
    assert json.loads(vault.oplog_path.read_text("utf-8").splitlines()[0])["entry_id"] == full.id
