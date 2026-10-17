"""Version store: the third review's fixes (B-10..B-13) - the upgrade's ownership of what it
removes, its re-check under the lock, start-up cleanup, the vault lock on every classic write,
a fold that goes on past a failed entry and refuses a read-only store, and one store per
operation that protects the configured live folders."""
from __future__ import annotations

import json
import shutil

import pytest

from nms_save_vault import cli
from nms_save_vault.core import operations, safety
from nms_save_vault.core import state as appstate
from nms_save_vault.core import store as vstore
from nms_save_vault.core.catalog import Vault
from nms_save_vault.core.state import AppState, Source
from nms_save_vault.core.store import classic, migrate, upgrade, versionstore

from .store_helpers import steam_live


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache-home"))
    monkeypatch.setattr(safety, "is_game_running", lambda: False)
    monkeypatch.setattr(versionstore.discover, "all_live_paths", lambda *a, **k: [])
    monkeypatch.setattr(appstate, "load", lambda *a, **k: None)


@pytest.fixture
def legacy(tmp_path):
    live = steam_live(tmp_path / "NMS")
    vault = Vault(tmp_path / "vault")
    operations.create_full_backup(vault, live, warnings=[])
    operations.snapshot_live(vault, live, "test")
    return vault, live


def _plan(vault, live):
    return vstore.plan_upgrade(vault.root, live_roots=[live])


def _lock_held(vault) -> bool:
    try:
        with safety.exclusive_lock(upgrade.upgrade_lock_path(vault.root), timeout=0):
            return False
    except (safety.LockTimeout, RuntimeError):
        return True


def test_a_store_folder_without_format_is_not_an_old_layout(legacy):
    vault, live = legacy
    (vault.root / "store" / "objects" / "ab").mkdir(parents=True)
    (vault.root / "store" / "objects" / "ab" / "something").write_bytes(b"not ours")
    plan = _plan(vault, live)
    assert plan.problems == (("damaged", "store folder present without FORMAT - run fsck"),)
    with pytest.raises(vstore.UpgradeError):
        vstore.run_upgrade(plan)
    (vault.root / "store" / "state").mkdir()
    (vault.root / "store" / "state" / "upgrade.json").write_text('{"state": "copying"}', "ascii")
    assert "Nothing was removed" in vstore.cleanup_interrupted_upgrade(vault.root).message
    assert (vault.root / "store" / "objects" / "ab" / "something").read_bytes() == b"not ours"


def test_a_failure_after_the_rename_removes_the_renamed_copy(legacy, monkeypatch):
    vault, live = legacy
    plan = _plan(vault, live)

    def the_note_cannot_be_written(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(upgrade, "_note_text", the_note_cannot_be_written)
    with pytest.raises(vstore.UpgradeError):
        vstore.run_upgrade(plan)
    assert not plan.dest.exists() and not list(vault.root.parent.glob("*.partial"))
    assert not (vault.root / "store").exists()

    # A power loss right after the rename: upgrade.json names both folders.
    plan.dest.mkdir()
    (plan.dest / "catalog.json").write_text("{}", "utf-8")
    state = vault.root / "store" / "state"
    state.mkdir(parents=True)
    (state / "upgrade.json").write_text(json.dumps({
        "state": "copying", "dest": str(plan.dest), "store_created": True,
        "partial": str(plan.dest.with_name(plan.dest.name + ".partial"))}), "ascii")
    assert "interrupted" in vstore.cleanup_interrupted_upgrade(vault.root).message
    assert not plan.dest.exists() and not (vault.root / "store").exists()


def test_the_cli_cleans_up_an_interrupted_upgrade_when_it_opens_the_vault(legacy, capsys):
    vault, live = legacy
    dest = vault.root.parent / "vault.pre-store-20261017T000000Z"
    partial = dest.with_name(dest.name + ".partial")
    partial.mkdir()
    state = vault.root / "store" / "state"
    state.mkdir(parents=True)
    (state / "upgrade.json").write_text(json.dumps({
        "state": "copying", "dest": str(dest), "partial": str(partial),
        "store_created": True}), "ascii")
    assert cli.main(["--vault", str(vault.root), "--live", str(live), "list"]) == 0
    assert "interrupted" in capsys.readouterr().err
    assert not partial.exists() and vstore.vault_mode(vault.root) == "legacy"


def test_the_run_checks_the_plan_again_under_its_lock(legacy, monkeypatch):
    vault, live = legacy
    plan = _plan(vault, live)
    (vault.root / "backups" / "late.txt").write_bytes(b"written after the plan")
    with pytest.raises(vstore.UpgradeError) as changed:
        vstore.run_upgrade(plan)
    assert changed.value.code == "vault-changing" and not (vault.root / "store").exists()
    plan = _plan(vault, live)
    real_usage = shutil.disk_usage
    monkeypatch.setattr(upgrade.shutil, "disk_usage",
                        lambda p: real_usage(p)._replace(free=1024))
    with pytest.raises(vstore.UpgradeError) as space:
        vstore.run_upgrade(plan)
    assert space.value.code == "space" and not (vault.root / "store").exists()


def test_only_a_vault_is_upgraded(tmp_path):
    missing = tmp_path / "no vault here"
    assert [c for c, _t in vstore.plan_upgrade(missing).problems] == ["not-a-vault"]
    with pytest.raises(vstore.UpgradeError):
        vstore.run_upgrade(vstore.plan_upgrade(missing))
    assert not missing.exists()
    plain = tmp_path / "plain folder"
    plain.mkdir()
    assert [c for c, _t in vstore.plan_upgrade(plain).problems] == ["not-a-vault"]
    assert list(plain.iterdir()) == []


def test_classic_writes_take_the_vault_lock_in_the_old_layout_too(legacy, monkeypatch):
    vault, live = legacy
    lock = vault.root / "organizer" / ".lock"
    seen = []
    real_upsert = Vault.upsert

    def held() -> bool:
        try:
            with safety.exclusive_lock(lock, timeout=0):
                return False
        except RuntimeError:
            return True

    monkeypatch.setattr(Vault, "upsert", lambda self, e: seen.append(held()) or real_upsert(self, e))
    operations.import_backup(vault, live, copy_into_vault=False, warnings=[])
    assert seen == [True] and vstore.vault_mode(vault.root) == "legacy"


def test_a_failed_entry_never_stops_the_fold(legacy, monkeypatch):
    vault, live = legacy
    vstore.run_upgrade(_plan(vault, live))
    store = vstore.open_store(vault.root, live_roots=[live])
    real = migrate._fold_one

    def a_bug_on_the_first(store_, entry, folder):
        if entry.kind == "full":
            raise RuntimeError("unexpected")
        return real(store_, entry, folder)

    monkeypatch.setattr(migrate, "_fold_one", a_bug_on_the_first)
    report = vstore.run_fold(store, live_roots=[live])
    assert [why for _k, why in report.failed] == ["unexpected"] and report.verified == 1


def test_the_fold_refuses_a_store_it_cannot_write(legacy):
    vault, live = legacy
    vstore.run_upgrade(_plan(vault, live))
    migration = vault.root / "store" / "state" / "migration.json"
    before = migration.read_bytes()
    store = vstore.VersionStore.open(vault.root, lock=vstore.vault_lock(vault.root),
                                     app_version="test", live_roots=[live], read_only=True)
    with pytest.raises(vstore.StoreUnavailable):
        vstore.run_fold(store, live_roots=[live])
    assert migration.read_bytes() == before


def test_cleanup_happens_while_the_upgrade_lock_is_held(legacy, monkeypatch):
    vault, live = legacy
    held_during = []
    real_remove = upgrade._remove_unfinished

    def watching(*a, **k):
        held_during.append(_lock_held(vault))
        return real_remove(*a, **k)

    monkeypatch.setattr(upgrade, "_remove_unfinished", watching)
    stop = __import__("threading").Event()
    stop.set()
    with pytest.raises(vstore.UpgradeError):
        vstore.run_upgrade(_plan(vault, live), cancel=stop)
    state = vault.root / "store" / "state"
    state.mkdir(parents=True)
    (state / "upgrade.json").write_text(json.dumps({"state": "copying", "store_created": True}),
                                        "ascii")
    vstore.cleanup_interrupted_upgrade(vault.root)
    assert held_during == [True, True]


def test_one_store_per_operation_and_the_configured_live_folders(legacy, tmp_path, monkeypatch):
    vault, live = legacy
    vstore.run_upgrade(_plan(vault, live))
    full = operations.create_full_backup(vault, live, warnings=[])
    opened = []
    real_open = vstore.VersionStore.open.__func__

    def counting(cls, *a, **k):
        opened.append(1)
        return real_open(cls, *a, **k)

    monkeypatch.setattr(vstore.VersionStore, "open", classmethod(counting))
    fresh = Vault(vault.root)
    operations.restore_full(fresh, classic.find_entry(fresh, full.id), live)
    assert opened == [1]                         # snapshot, materialize, oplog: one store
    configured = tmp_path / "configured live"
    state = AppState(sources=[Source(id="s", platform="steam", account="1",
                                     path=str(configured))])
    monkeypatch.setattr(versionstore.discover, "all_live_paths", _all_live_paths)
    monkeypatch.setattr(appstate, "load", lambda *a, **k: state)
    store = classic.store_of(Vault(vault.root))
    assert configured.resolve() in store.live_folders()


def _all_live_paths(state, ms_dirs=None, nms_root=None):
    """discover.all_live_paths without the machine's own live folders."""
    return [s.path for s in state.sources] if state is not None else []
