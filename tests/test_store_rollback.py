"""Version store B-15: undo the vault upgrade - round trip, refusals, interrupted runs
(D3 4.4, T19, T20)."""
from __future__ import annotations

import hashlib
import json
import shutil
from types import SimpleNamespace

import pytest

from nms_save_vault import cli
from nms_save_vault.core import catalog, operations, safety
from nms_save_vault.core import store as vstore
from nms_save_vault.core.catalog import Vault
from nms_save_vault.core.store import rollback, versionstore

from .store_helpers import steam_live


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache-home"))
    monkeypatch.setattr(safety, "is_game_running", lambda: False)
    monkeypatch.setattr(versionstore.discover, "all_live_paths", lambda *a, **k: [])


@pytest.fixture
def upgraded(tmp_path):
    """An old-layout vault (full backup, snapshot, extract), then upgraded."""
    live = steam_live(tmp_path / "NMS")
    vault = Vault(tmp_path / "vault")
    operations.create_full_backup(vault, live, warnings=[])
    operations.snapshot_live(vault, live, "test")
    operations.extract_slot(vault, live, 2, warnings=[], live_dirs=[live])
    old = {e.id for e in vault.load().entries}
    result = vstore.run_upgrade(vstore.plan_upgrade(vault.root, live_roots=[live]))
    store = vstore.open_store(vault.root, live_roots=[live])
    return vault, live, store, vault.root.parent / result.copy_aside, old


def _tree(folder):
    return {p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(folder.rglob("*")) if p.is_file()}


def test_rollback_round_trip(upgraded):
    vault, live, store, copy, old = upgraded
    copy_before = _tree(copy)
    made = operations.create_full_backup(vault, live, warnings=[])        # in store mode
    snap = operations.snapshot_live(vault, live, "since")
    assert store.has_entry(made.id) and store.has_entry(snap.id)

    plan = rollback.plan_rollback(vault.root, store)
    assert plan.problems == () and plan.copy_ok
    assert {made.id, snap.id} <= {sid for sid, _f, _k in plan.export}
    result = rollback.run_rollback(plan, store)

    assert vstore.vault_mode(vault.root) == "legacy" and not (vault.root / "store").exists()
    moved = vault.root.parent / result.store_moved_to
    assert ".store-rolled-back-" in moved.name and moved.is_dir()
    entries = Vault(vault.root).load().entries
    ids = {e.id for e in entries}
    assert old <= ids and {made.id, snap.id} <= ids                       # the union
    for entry in entries:
        folder = rollback._vault_folder(vault.root, entry)
        assert folder.is_dir() and _tree(folder)
        if entry.id in old:
            assert _tree(folder) == _tree(rollback._vault_folder(copy, entry))
    exported = next(e for e in entries if e.id == made.id)
    assert _tree(rollback._vault_folder(vault.root, exported))
    assert _tree(copy) == copy_before                                     # only read
    log = vault.root / "oplog.jsonl"
    for line in (log.read_text("utf-8").splitlines() if log.exists() else []):
        sid = json.loads(line).get("snapshot_id")
        if sid:
            assert sid in ids


def test_rollback_puts_back_a_changed_old_folder_and_keeps_the_changed_one(upgraded):
    vault, live, store, copy, old = upgraded
    victim = next(e for e in Vault(vault.root).load().entries if e.managed)
    folder = rollback._vault_folder(vault.root, victim)
    target = next(p for p in sorted(folder.rglob("*")) if p.is_file())
    target.write_bytes(b"damaged")
    plan = rollback.plan_rollback(vault.root, store)
    assert victim.id in plan.restore
    rollback.run_rollback(plan, store)
    assert _tree(folder) == _tree(rollback._vault_folder(copy, victim))
    aside = vault.root / ".rollback" / f"replaced-{victim.id}"
    assert (aside / target.relative_to(folder)).read_bytes() == b"damaged"


def test_rollback_refusals_change_nothing(upgraded, monkeypatch):
    vault, live, store, copy, old = upgraded
    before = _tree(vault.root)

    elsewhere = copy.with_name("elsewhere")
    copy.rename(elsewhere)
    plan = rollback.plan_rollback(vault.root, store)
    assert [c for c, _t in plan.problems] == ["copy-missing"]
    with pytest.raises(vstore.RollbackError) as missing:
        rollback.run_rollback(plan, store)
    assert missing.value.code == "copy-missing"

    # low space (the copy is found by the folder the owner gives)
    monkeypatch.setattr(rollback.shutil, "disk_usage", lambda p: SimpleNamespace(free=1))
    plan = rollback.plan_rollback(vault.root, store, copy=elsewhere)
    assert [c for c, _t in plan.problems] == ["space"]
    with pytest.raises(vstore.RollbackError) as low:
        rollback.run_rollback(plan, store)
    assert low.value.code == "space"
    monkeypatch.undo()
    monkeypatch.setattr(safety, "is_game_running", lambda: False)

    # a copy that is not the copy
    victim = next(p for p in sorted(elsewhere.rglob("*"))
                  if p.is_file() and p.name != catalog.PRE_UPGRADE_COPY)
    victim.write_bytes(b"changed")
    plan = rollback.plan_rollback(vault.root, store, copy=elsewhere)
    assert [c for c, _t in plan.problems] == ["copy-mismatch"]
    with pytest.raises(vstore.RollbackError) as bad:
        rollback.run_rollback(plan, store)
    assert bad.value.code == "copy-mismatch"
    assert _tree(vault.root) == before and vstore.vault_mode(vault.root) == "store"


def test_rollback_interrupted_then_finish_or_keep(upgraded):
    vault, live, store, copy, old = upgraded
    made = operations.snapshot_live(vault, live, "since")

    def crash(point):
        if point == "rollback.before_store_move":
            raise OSError(5, "power loss")

    plan = rollback.plan_rollback(vault.root, store)
    with pytest.raises(OSError):
        rollback.run_rollback(plan, store, faults=crash)
    assert rollback.rollback_interrupted(vault.root)
    assert vstore.vault_mode(vault.root) == "upgrading"
    with pytest.raises(vstore.StoreNotUpgraded):
        operations.snapshot_live(vault, live, "blocked")

    rollback.keep_upgrade(vault.root)                                     # Keep the upgrade
    assert not rollback.rollback_interrupted(vault.root)
    assert vstore.vault_mode(vault.root) == "store"
    assert vstore.open_store(vault.root, live_roots=[live]).has_entry(made.id)

    plan = rollback.plan_rollback(vault.root, store)
    with pytest.raises(OSError):
        rollback.run_rollback(plan, store, faults=crash)
    reopened = vstore.VersionStore.open(
        vault.root, lock=versionstore.vault_lock(vault.root), app_version="test",
        live_roots=(), for_rollback=True)
    plan = rollback.plan_rollback(vault.root, reopened)                   # Finish
    assert plan.problems == ()
    rollback.run_rollback(plan, reopened)
    assert vstore.vault_mode(vault.root) == "legacy"
    assert made.id in {e.id for e in Vault(vault.root).load().entries}


def test_the_cli_undoes_the_upgrade(upgraded, monkeypatch, capsys):
    vault, live, _store, _copy, _old = upgraded
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    assert cli.main(["--vault", str(vault.root), "store", "rollback"]) == 0
    assert "Nothing was changed" in capsys.readouterr().out
    assert vstore.vault_mode(vault.root) == "store"
    assert cli.main(["--vault", str(vault.root), "store", "rollback", "--yes"]) == 0
    assert "Upgrade undone" in capsys.readouterr().out
    assert vstore.vault_mode(vault.root) == "legacy"
    assert cli.main(["--vault", str(vault.root), "store", "rollback", "--yes"]) == 1
    assert "Upgrade the vault first" in capsys.readouterr().err
