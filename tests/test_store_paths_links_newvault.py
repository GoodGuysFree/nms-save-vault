"""Version store follow-ups: paths over 260 characters, directory junctions and links in a
capture, and a brand-new vault starting in store mode (D3 4.1)."""
from __future__ import annotations

import os
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from nms_save_vault import cli
from nms_save_vault.core import catalog, operations, safety
from nms_save_vault.core import state as appstate
from nms_save_vault.core import store as vstore
from nms_save_vault.core.catalog import Vault
from nms_save_vault.core.store import capture, migrate, rollback, versionstore

from .store_helpers import STEAM_ACCOUNT, new_vault, open_vault_store, steam_live

windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows paths and junctions")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(safety, "is_game_running", lambda: False)
    monkeypatch.setattr(versionstore.discover, "all_live_paths", lambda *a, **k: [])


def _deep(parent):
    folder = parent
    while len(str(folder)) < 300:
        folder = folder / ("d" * 40)
    return folder


def _junction(link, target):
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                            capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        pytest.skip(f"no junction: {result.stdout} {result.stderr}")


# --- long paths ---------------------------------------------------------------------------


@windows_only
def test_a_folder_under_a_long_path_is_walked_hashed_and_kept(tmp_path):
    src = steam_live(tmp_path / "src")
    deep = _deep(tmp_path / "deep") / STEAM_ACCOUNT
    assert len(str(deep)) > 260
    shutil.copytree(capture.long_path(src), capture.long_path(deep))

    expected = capture.walk(src)
    assert capture.walk(deep) == expected                    # same relative paths, no warnings
    assert capture._is_regular(deep / "accountdata.hg")

    store = open_vault_store(new_vault(tmp_path))
    entry = store.add_from_folder(deep, kind="imported", label="deep",
                                  source={"type": "folder", "name": "deep"},
                                  scope=vstore.CaptureScope(cache="include"))
    assert [f.path for f in entry.files] == expected
    migrate.check_folder(store, {f.path: f for f in entry.files}, deep)   # the fold's check

    tree = rollback._tree(deep)
    assert set(tree) == set(expected) and tree == rollback._tree(src)
    assert rollback._tree_bytes(deep) == sum(s for s, _ in tree.values())


@windows_only
def test_a_rollback_hash_of_a_copy_under_a_long_path_does_not_warn(tmp_path, caplog):
    deep = _deep(tmp_path / "deep") / "copy"
    shutil.copytree(capture.long_path(steam_live(tmp_path / "src")), capture.long_path(deep))
    with caplog.at_level("WARNING"):
        rollback._tree_sha(deep, None)
    assert not [r for r in caplog.records if "not a regular file" in r.getMessage()]


# --- links --------------------------------------------------------------------------------


def _folder_with_junction(tmp_path):
    live = steam_live(tmp_path / "live")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "elsewhere.txt").write_bytes(b"not in the folder")
    _junction(live / "linked", outside)
    return live


@windows_only
def test_walk_never_follows_a_junction(tmp_path):
    live = _folder_with_junction(tmp_path)
    links: list[str] = []
    rels = capture.walk(live, links)
    assert links == ["linked"] and not any(r.startswith("linked") for r in rels)
    assert capture.is_link(live / "linked") and not capture.is_link(live / "cache")
    with pytest.raises(vstore.StoreError, match="linked"):
        capture.walk(live, strict=True)


@windows_only
def test_a_live_capture_refuses_a_junction_and_a_folder_capture_skips_it(tmp_path):
    live = _folder_with_junction(tmp_path)
    store = open_vault_store(new_vault(tmp_path))
    with pytest.raises(vstore.StoreError, match="link"):
        store.add_from_live(live, platform="steam", account_key=STEAM_ACCOUNT, kind="full",
                            label="x", scope=vstore.CaptureScope())
    assert not store.entries()
    entry = store.add_from_folder(live, kind="imported", label="old",
                                  source={"type": "folder", "name": "old"},
                                  scope=vstore.CaptureScope(cache="include"))
    assert not any("linked" in f.path or f.path == "elsewhere.txt" for f in entry.files)


@windows_only
def test_the_fold_records_the_links_it_skipped(tmp_path):
    folder = _folder_with_junction(tmp_path)
    store = open_vault_store(new_vault(tmp_path, "v2"))
    entry = SimpleNamespace(id="full-steam-20260101-000000", kind="full", label="old",
                            created="2026-01-01T00:00:00", source=None)
    record = migrate._fold_one(store, entry, folder)
    assert record["status"] == "verified" and record["skipped_links"] == ["linked"]


@pytest.mark.skipif(os.name == "nt", reason="symlinks need a privilege on Windows")
def test_walk_skips_and_strict_refuses_a_symlink(tmp_path):
    live = steam_live(tmp_path / "live")
    (tmp_path / "outside").mkdir()
    (live / "linked").symlink_to(tmp_path / "outside", target_is_directory=True)
    links: list[str] = []
    assert "linked" not in capture.walk(live, links) and links == ["linked"]
    with pytest.raises(vstore.StoreError):
        capture.walk(live, strict=True)


# --- a new vault --------------------------------------------------------------------------


def test_a_new_vault_starts_in_store_mode_at_its_first_write(tmp_path, new_vault_store):
    vault = Vault(tmp_path / "fresh")
    assert versionstore.vault_mode(vault.root) == "legacy"      # nothing yet: nothing created
    vault.ensure()                                               # browsing creates no store
    assert versionstore.vault_mode(vault.root) == "legacy"
    vault.save()
    assert versionstore.vault_mode(vault.root) == "store"
    assert catalog.looks_like_vault_dir(vault.root)
    assert not vstore.upgrade_needed(vault.root)
    vault.save()                                                 # idempotent
    assert versionstore.vault_mode(vault.root) == "store"


def test_the_first_backup_into_a_new_vault_is_a_store_entry(tmp_path, new_vault_store):
    live = steam_live(tmp_path / "NMS")
    vault = Vault(tmp_path / "fresh")
    entry = operations.create_full_backup(vault, live, warnings=[])
    assert versionstore.vault_mode(vault.root) == "store"
    assert not entry.path                                        # a store entry, no folder
    assert not any(list((vault.root / d).iterdir()) for d in ("backups", "snapshots", "extracts"))
    assert open_vault_store(vault.root).has_entry(entry.id)


def test_reading_a_vault_creates_nothing(tmp_path, new_vault_store):
    from nms_save_vault.core.store import classic

    vault = Vault(tmp_path / "missing")
    assert classic.list_entries(vault) == []
    assert not vault.root.exists()


def test_a_vault_with_anything_in_it_stays_old_and_upgrades(tmp_path, new_vault_store, monkeypatch):
    monkeypatch.setattr(versionstore, "NEW_VAULTS_USE_STORE", False)
    vault = Vault(tmp_path / "old")
    operations.create_full_backup(vault, steam_live(tmp_path / "NMS"), warnings=[])
    monkeypatch.setattr(versionstore, "NEW_VAULTS_USE_STORE", True)
    vault.ensure()
    assert versionstore.vault_mode(vault.root) == "legacy" and vstore.upgrade_needed(vault.root)
    junk = tmp_path / "junk"
    (junk / "backups" / "x").mkdir(parents=True)
    Vault(junk).ensure()
    assert versionstore.vault_mode(junk) == "legacy"


def test_a_read_only_vault_is_never_started(tmp_path, new_vault_store):
    root = tmp_path / "ro"
    root.mkdir()
    assert versionstore.start_new_vault(root)                    # an empty folder starts
    other = tmp_path / "ro2"
    other.mkdir()
    from nms_save_vault.core.store import classic
    assert classic.store_of(Vault(other, read_only=True)) is None
    assert versionstore.vault_mode(other) == "legacy"


def test_the_cli_upgrade_of_an_empty_folder_starts_a_store_vault(tmp_path, new_vault_store,
                                                                  monkeypatch, capsys):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    root = tmp_path / "empty"
    root.mkdir()
    assert cli.main(["store", "upgrade", "--vault", str(root), "--yes"]) == 0
    assert versionstore.vault_mode(root) == "store"
    assert "new" in capsys.readouterr().out
