"""Version store B-12: the vault upgrade - plan, copy-aside, verify, commit, refusals,
interrupted runs, a vault that changes meanwhile, and what discovery and imports make of the
copy (D3 4.1, T13-T16, T28)."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading

import pytest

from nms_save_vault import cli
from nms_save_vault.core import catalog, operations, safety
from nms_save_vault.core import state as appstate
from nms_save_vault.core import store as vstore
from nms_save_vault.core.catalog import Vault
from nms_save_vault.core.store import upgrade

from .store_helpers import steam_live


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache-home"))
    monkeypatch.setattr(safety, "is_game_running", lambda: False)
    monkeypatch.setattr(vstore.versionstore.discover, "all_live_paths", lambda *a, **k: [])


@pytest.fixture
def legacy(tmp_path):
    """A vault in the old layout, filled by the classic operations."""
    live = steam_live(tmp_path / "NMS")
    vault = Vault(tmp_path / "vault")
    full = operations.create_full_backup(vault, live, warnings=[])
    snap = operations.snapshot_live(vault, live, "test")
    extract = operations.extract_slot(vault, live, 2, warnings=[], live_dirs=[live])
    return vault, live, (full, snap, extract)


def _tree(folder):
    """relpath -> (size, mtime_ns, sha256), the store and lock files aside."""
    out = {}
    for p in sorted(folder.rglob("*")):
        rel = p.relative_to(folder).as_posix()
        if p.is_file() and not rel.startswith("store/") and not rel.endswith(".lock") \
                and rel != catalog.PRE_UPGRADE_COPY:
            out[rel] = (p.stat().st_size, p.stat().st_mtime_ns,
                        hashlib.sha256(p.read_bytes()).hexdigest())
    return out


def test_upgrade_copy_aside_verified(legacy):
    vault, live, _entries = legacy
    before = _tree(vault.root)
    assert vstore.upgrade_needed(vault.root)
    plan = vstore.plan_upgrade(vault.root, live_roots=[live])
    assert plan.problems == () and plan.copy and plan.files == len(before)
    assert plan.dest.parent == vault.root.parent and ".pre-store-" in plan.dest.name
    stages = []
    result = vstore.run_upgrade(plan, progress=lambda p: stages.append(p.stage))
    assert {"copy", "verify"} <= set(stages)

    copy = vault.root.parent / result.copy_aside
    assert _tree(copy) == before == _tree(vault.root)            # names, bytes and mtimes
    assert (copy / catalog.PRE_UPGRADE_COPY).read_bytes().isascii()
    state = vault.root / "store" / "state"
    migration = json.loads((state / "migration.json").read_text("ascii"))
    lines = sorted(f"{rel}|{size}|{sha}" for rel, (size, _m, sha) in before.items())
    assert migration["copy_aside"]["tree_sha256"] == hashlib.sha256(
        "\n".join(lines).encode()).hexdigest()
    assert migration["copy_aside"]["name"] == result.copy_aside and migration["copy_aside"]["sibling"]
    assert migration["fold"]["state"] == "pending"
    assert str(vault.root.parent) not in (state / "migration.json").read_text("ascii")
    assert vstore.vault_mode(vault.root) == "store" and not (state / "upgrade.json").exists()
    assert not vstore.upgrade_needed(vault.root)
    status = vstore.store_status(vault.root, vstore.open_store(vault.root), live_roots=[live])
    assert status.copy_aside.found and status.copy_aside.files == len(before)
    assert "Vault upgraded" in result.message and result.message.isascii()


def test_an_empty_vault_is_upgraded_without_a_copy(tmp_path):
    vault = Vault(tmp_path / "vault")
    vault.ensure()
    vault.save()
    plan = vstore.plan_upgrade(vault.root, live_roots=[])
    assert not plan.copy
    result = vstore.run_upgrade(plan)
    assert result.copy_aside is None and vstore.vault_mode(vault.root) == "store"
    assert json.loads((vault.root / "store/state/migration.json").read_text("ascii"))["copy_aside"] is None
    assert not list(tmp_path.glob("vault.pre-store-*"))


def test_upgrade_refusals(legacy, tmp_path, monkeypatch):
    vault, live, _entries = legacy
    before = _tree(vault.root)

    def codes(**kw):
        return [c for c, _t in vstore.plan_upgrade(vault.root, live_roots=[live], **kw).problems]

    real_usage = shutil.disk_usage
    monkeypatch.setattr(upgrade.shutil, "disk_usage", lambda p: real_usage(p)._replace(free=1024))
    assert codes() == ["space"]
    monkeypatch.undo()
    monkeypatch.setattr(vstore.versionstore.discover, "all_live_paths", lambda *a, **k: [])
    assert "path-too-long" in codes(dest_parent=tmp_path / ("x" * 230))
    assert codes(dest_parent=vault.root / "backups") == ["bad-destination"]
    assert codes(dest_parent=live) == ["bad-destination"]
    with pytest.raises(vstore.UpgradeError) as refused:
        vstore.run_upgrade(vstore.plan_upgrade(vault.root, dest_parent=live, live_roots=[live]))
    assert refused.value.code == "bad-destination"
    assert _tree(vault.root) == before and not (vault.root / "store").exists()

    script = ("import sys, time\nfrom nms_save_vault.core.safety import exclusive_lock\n"
              "with exclusive_lock(sys.argv[1]):\n    print('held', flush=True)\n    time.sleep(30)\n")
    holder = subprocess.Popen([sys.executable, "-c", script,
                               str(upgrade.upgrade_lock_path(vault.root))],
                              stdout=subprocess.PIPE, text=True, encoding="utf-8")
    try:
        assert holder.stdout.readline().strip() == "held"
        assert codes() == ["in-progress"]
        plan = vstore.plan_upgrade(vault.root, live_roots=[live])
    finally:
        holder.kill()
        holder.wait()
    assert not (vault.root / "store").exists()
    vstore.run_upgrade(vstore.plan_upgrade(vault.root, live_roots=[live]))
    assert codes()[0] == "already-upgraded"


def test_upgrade_crash_and_cancel(legacy, monkeypatch):
    vault, live, _entries = legacy
    before = _tree(vault.root)
    stop = threading.Event()
    stop.set()
    with pytest.raises(vstore.UpgradeError) as cancelled:
        vstore.run_upgrade(vstore.plan_upgrade(vault.root, live_roots=[live]), cancel=stop)
    assert cancelled.value.code == "cancelled" and "not changed" in str(cancelled.value)
    _assert_untouched(vault, before)

    real_copy = upgrade._Run._copy
    calls = []

    def disk_fails(self, rel):
        calls.append(rel)
        if len(calls) == 3:
            raise OSError(5, "Input/output error", rel)
        return real_copy(self, rel)

    monkeypatch.setattr(upgrade._Run, "_copy", disk_fails)
    with pytest.raises(vstore.UpgradeError) as failed:
        vstore.run_upgrade(vstore.plan_upgrade(vault.root, live_roots=[live]))
    assert failed.value.code == "copy-failed"
    monkeypatch.undo()
    monkeypatch.setattr(vstore.versionstore.discover, "all_live_paths", lambda *a, **k: [])
    _assert_untouched(vault, before)

    monkeypatch.setattr(upgrade.safety, "sha256_file", lambda p: "0" * 64)
    with pytest.raises(vstore.UpgradeError) as mismatch:
        vstore.run_upgrade(vstore.plan_upgrade(vault.root, live_roots=[live]))
    assert mismatch.value.code == "copy-mismatch"
    monkeypatch.undo()
    monkeypatch.setattr(vstore.versionstore.discover, "all_live_paths", lambda *a, **k: [])
    _assert_untouched(vault, before)

    # A power loss mid-copy: upgrade.json says "copying", a .partial copy is lying around.
    dest = vault.root.parent / "vault.pre-store-20261017T000000Z"
    partial = dest.with_name(dest.name + ".partial")
    (partial / "backups").mkdir(parents=True)
    (partial / "catalog.json").write_text("{}", "utf-8")
    state = vault.root / "store" / "state"
    state.mkdir(parents=True)
    (state / "upgrade.json").write_text(json.dumps({
        "state": "copying", "partial": str(partial), "dest": str(dest), "store_created": True}),
        "ascii")
    assert vstore.vault_mode(vault.root) == "upgrading"
    with pytest.raises(vstore.StoreNotUpgraded, match="being upgraded"):
        operations.snapshot_live(vault, live, "x")
    cleaned = vstore.cleanup_interrupted_upgrade(vault.root)
    assert "interrupted" in cleaned.message
    _assert_untouched(vault, before)
    assert vstore.cleanup_interrupted_upgrade(vault.root) is None
    vstore.run_upgrade(vstore.plan_upgrade(vault.root, live_roots=[live]))     # runs again
    assert vstore.vault_mode(vault.root) == "store"


def test_an_upgrade_interrupted_after_its_commit_is_finished(legacy):
    vault, live, _entries = legacy
    vstore.run_upgrade(vstore.plan_upgrade(vault.root, live_roots=[live]))
    (vault.root / "store" / "state" / "upgrade.json").write_text('{"state": "copying"}', "ascii")
    assert "finished" in vstore.cleanup_interrupted_upgrade(vault.root).message
    assert vstore.vault_mode(vault.root) == "store"


def _assert_untouched(vault, before):
    assert _tree(vault.root) == before
    assert not (vault.root / "store").exists()
    assert not list(vault.root.parent.glob("vault.pre-store-*"))


def test_upgrade_vault_changes_during_copy(legacy):
    vault, live, _entries = legacy
    added = []

    def another_window_writes(p):
        if p.stage == "copy" and not added:
            (vault.root / "backups" / "late.txt").write_bytes(b"written during the copy")
            added.append(1)

    result = vstore.run_upgrade(vstore.plan_upgrade(vault.root, live_roots=[live]),
                                progress=another_window_writes)
    assert (vault.root.parent / result.copy_aside / "backups" / "late.txt").is_file()
    assert _tree(vault.root.parent / result.copy_aside) == _tree(vault.root)


def test_a_vault_that_keeps_changing_is_refused(legacy):
    vault, live, _entries = legacy
    before_catalog = vault.catalog_path.read_bytes()
    ticks = []

    def always_writing(p):
        if p.stage == "copy":
            ticks.append(1)
            os.utime(vault.catalog_path, ns=(len(ticks) * 10**9, len(ticks) * 10**9))

    with pytest.raises(vstore.UpgradeError) as refused:
        vstore.run_upgrade(vstore.plan_upgrade(vault.root, live_roots=[live]),
                           progress=always_writing)
    assert refused.value.code == "vault-changing"
    assert vault.catalog_path.read_bytes() == before_catalog
    assert not (vault.root / "store").exists()


def test_discovery_skips_vaults_and_copies(legacy, tmp_path):
    vault, live, _entries = legacy
    vstore.run_upgrade(vstore.plan_upgrade(vault.root, live_roots=[live]))
    found = catalog.discover_save_dirs(tmp_path, max_depth=4)
    assert found == [live]                    # not the vault's folders, not the copy's


def test_a_pre_upgrade_copy_is_never_opened_or_imported(legacy, monkeypatch, capsys):
    vault, live, _entries = legacy
    result = vstore.run_upgrade(vstore.plan_upgrade(vault.root, live_roots=[live]))
    copy = vault.root.parent / result.copy_aside
    other = Vault(vault.root.parent / "other")
    other.ensure()
    with pytest.raises(operations.OperationError, match="pre-upgrade copy"):
        operations.import_vault(other, copy, copy_into_vault=True)
    monkeypatch.setattr(appstate, "load", lambda *a, **k: None)
    with pytest.raises(SystemExit) as refused:
        cli.main(["--vault", str(copy), "list"])
    assert "pre-upgrade copy" in str(refused.value.code)
    assert vstore.plan_upgrade(copy).problems[0][0] == "bad-destination"


def test_the_cli_upgrades_only_when_asked(legacy, monkeypatch, capsys):
    vault, live, _entries = legacy
    monkeypatch.setattr(appstate, "load", lambda *a, **k: None)
    assert cli.main(["--vault", str(vault.root), "--live", str(live), "list"]) == 0
    assert vstore.vault_mode(vault.root) == "legacy"            # nothing upgrades by itself
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    assert cli.main(["--vault", str(vault.root), "store", "upgrade"]) == 0
    assert "Nothing was changed" in capsys.readouterr().out
    assert vstore.vault_mode(vault.root) == "legacy" and not (vault.root / "store").exists()
    assert cli.main(["--vault", str(vault.root), "store", "upgrade", "--yes"]) == 0
    assert "Vault upgraded" in capsys.readouterr().out
    assert vstore.vault_mode(vault.root) == "store"
    assert cli.main(["--vault", str(vault.root), "store", "upgrade", "--yes"]) == 1
    assert "already upgraded" in capsys.readouterr().err


@pytest.mark.skipif(os.name != "nt", reason="the 260 character limit is a Windows one")
def test_a_vault_holding_a_path_over_260_characters_is_refused_not_a_crash(legacy):
    vault, live, _entries = legacy
    deep = safety.long_path(vault.backups_dir)
    for _ in range(8):
        deep = deep / ("d" * 40)
    os.makedirs(deep)
    (deep / "file.bin").write_bytes(b"x")
    assert len(str(deep)) > 260
    plan = vstore.plan_upgrade(vault.root, live_roots=[live])
    assert "path-too-long" in [c for c, _t in plan.problems]
    assert any(rel.endswith("file.bin") for rel in upgrade.tree(vault.root))
