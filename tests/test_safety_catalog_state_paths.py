"""Whole-program safety review (A): a stale Vault never drops another writer's entries, a crafted
id never leaves the vault, a damaged state.json / catalog.json never stops the app or gets
overwritten, a vault path over 260 characters works, copies are synced before they count."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from nms_save_vault.core import catalog, operations, safety
from nms_save_vault.core import state as appstate
from nms_save_vault.core import store as vstore
from nms_save_vault.core.catalog import OperationError, Vault
from nms_save_vault.core.store import rollback, versionstore

from .store_helpers import steam_live


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(safety, "is_game_running", lambda: False)
    monkeypatch.setattr(versionstore.discover, "all_live_paths", lambda *a, **k: [])


# --- M1: no lost updates -------------------------------------------------------------------


def test_two_windows_on_one_legacy_vault_keep_each_others_entries(tmp_path):
    live = steam_live(tmp_path / "NMS")
    root = tmp_path / "vault"
    first = operations.create_full_backup(Vault(root), live, warnings=[])
    gui = Vault(root)
    gui.load()                                       # loaded once, at start
    cli = Vault(root).load()
    by_cli = operations.create_full_backup(cli, live, warnings=[])
    assert by_cli.id in {e.id for e in gui.entries}  # the open window sees it without a reload
    by_gui = operations.create_full_backup(gui, live, warnings=[])
    ids = {e.id for e in Vault(root).load().entries}
    assert {first.id, by_cli.id, by_gui.id} <= ids


def test_a_backup_after_the_undo_of_the_upgrade_keeps_what_the_undo_exported(tmp_path):
    live = steam_live(tmp_path / "NMS")
    vault = Vault(tmp_path / "vault")
    operations.create_full_backup(vault, live, warnings=[])
    vstore.run_upgrade(vstore.plan_upgrade(vault.root, live_roots=[live]))
    store = vstore.open_store(vault.root, live_roots=[live])
    made = operations.create_full_backup(Vault(vault.root), live, warnings=[])   # a store entry
    gui = Vault(vault.root)
    gui.load()
    plan = rollback.plan_rollback(vault.root, store)
    assert plan.problems == ()
    rollback.run_rollback(plan, store)
    operations.create_full_backup(gui, live, warnings=[])        # the open window writes next
    ids = {e.id for e in Vault(vault.root).load().entries}
    assert made.id in ids and (vault.root / "backups" / made.id).is_dir()


def test_remove_applies_only_its_change_to_the_current_file(tmp_path):
    live = steam_live(tmp_path / "NMS")
    root = tmp_path / "vault"
    a = operations.snapshot_live(Vault(root), live, "a")
    stale = Vault(root).load()
    b = operations.create_full_backup(Vault(root), live, warnings=[])
    stale.remove(a.id)
    assert [e.id for e in Vault(root).load().entries] == [b.id]


# --- m1: a crafted id ------------------------------------------------------------------------


def _crafted_source(base: Path, live: Path, bad_id: str) -> Path:
    src = base / "src" / "vault"
    folder = src / "backups" / bad_id
    shutil.copytree(live, folder)
    entry = {"id": bad_id, "kind": "full", "label": "x", "path": str(folder),
             "created": "2026-10-01T10:00:00", "source": live.name, "managed": True,
             "slots": [], "note": ""}
    src.mkdir(parents=True, exist_ok=True)
    (src / "catalog.json").write_text(json.dumps({"version": 1, "entries": [entry]}), "utf-8")
    return src


def test_a_legacy_import_refuses_an_id_that_climbs_out_of_the_vault(tmp_path):
    live = steam_live(tmp_path / "NMS")
    target = Vault(tmp_path / "dst" / "vault")
    operations.create_full_backup(target, live, warnings=[])
    src = _crafted_source(tmp_path, live, "..\\..\\escaped")
    result = operations.import_vault(target, src, copy_into_vault=True)
    assert not (tmp_path / "dst" / "escaped").exists()
    assert any("not a valid backup id" in w for w in result.warnings) and not result.changed


def test_a_store_import_skips_a_bad_id_instead_of_aborting(tmp_path):
    live = steam_live(tmp_path / "NMS")
    root = tmp_path / "dst" / "vault"
    root.mkdir(parents=True)
    vstore.create_store(root)
    result = operations.import_vault(
        Vault(root), _crafted_source(tmp_path, live, "..\\..\\escaped"), copy_into_vault=True)
    assert not result.changed and any("not a valid backup id" in w for w in result.warnings)


# --- m2: damaged files ----------------------------------------------------------------------


@pytest.mark.parametrize("text", ['{"vault": "x",', "null", '{"sources": [{"id": "a"}]}', "[1]"])
def test_a_damaged_state_json_is_put_aside_and_the_app_starts_from_defaults(tmp_path, text):
    p = tmp_path / "state.json"
    p.write_text(text, "utf-8")
    appstate.take_notices()
    assert appstate.load(p) is None
    aside = list(tmp_path.glob("state.json.corrupt-*"))
    assert len(aside) == 1 and aside[0].read_text("utf-8") == text and not p.exists()
    notes = appstate.take_notices()
    assert len(notes) == 1 and "state.json" in notes[0] and notes[0].isascii()


def test_state_save_syncs_before_it_replaces(tmp_path, monkeypatch):
    synced = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (synced.append(fd), real(fd))[1])
    p = appstate.save(appstate.AppState(), tmp_path / "state.json")
    assert synced and appstate.load(p) is not None
    assert not list(tmp_path.glob("state.json.tmp*"))


def test_a_damaged_catalog_is_reported_plainly_and_never_overwritten(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    for text in ('{"entries": [', '{"entries": [{"id": "x"}]}', "[]"):
        (root / "catalog.json").write_text(text, "utf-8")
        with pytest.raises(OperationError, match="catalog.json") as caught:
            Vault(root).load()
        assert "--vault" in str(caught.value)
        assert (root / "catalog.json").read_text("utf-8") == text
    unreadable = Vault(root, read_only=True)
    unreadable.mark_unreadable()                    # what the GUI does: empty, read-only
    assert unreadable.entries == []
    entry = catalog.CatalogEntry(id="x", kind="full", label="", path="",
                                 created="2026-01-01T00:00:00")
    with pytest.raises(PermissionError):
        unreadable.upsert(entry)
    assert (root / "catalog.json").read_text("utf-8") == "[]"


def test_a_vault_that_is_a_file_is_an_operation_error(tmp_path):
    notafolder = tmp_path / "vault_is_a_file"
    notafolder.write_text("x")
    with pytest.raises(OperationError, match="cannot be reached or created"):
        Vault(notafolder).ensure()
    with pytest.raises(OperationError, match="--vault"):
        Vault(notafolder / "inside").ensure()


# --- m6: long vault paths --------------------------------------------------------------------


@pytest.mark.skipif(os.name != "nt", reason="the 260 character limit is Windows'")
def test_a_vault_under_a_path_over_260_characters_stores_and_restores(tmp_path, new_vault_store):
    live = steam_live(tmp_path / "NMS")
    deep = tmp_path
    while len(str(deep)) < 230:
        deep = deep / ("v" * 40)
    root = deep / "vault"
    vault = Vault(safety.long_path(root))
    vault.ensure()                                   # a new vault: store mode, under a long path
    assert versionstore.vault_mode(vault.root) == "store"
    entry = operations.create_full_backup(vault, live, warnings=[])
    store = versionstore.open_store(vault.root, live_roots=[live])
    assert store.has_entry(entry.id)
    with store.materialized(entry.id) as folder:
        assert (folder / "accountdata.hg").read_bytes() == (live / "accountdata.hg").read_bytes()
    out = safety.long_path(deep / "out")
    store.materialize(entry.id, out)
    assert (out / "accountdata.hg").is_file()


# --- m7: synced before it counts -------------------------------------------------------------


def test_a_legacy_copy_is_synced_file_by_file_and_the_put_back_folder_too(tmp_path, monkeypatch):
    synced = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (synced.append(fd), real(fd))[1])
    src = tmp_path / "a.bin"
    src.write_bytes(b"data")
    safety.copy_and_hash(src, tmp_path / "b.bin")
    assert len(synced) == 1
    synced.clear()
    rollback._copy_synced(src, tmp_path / "c.bin")
    assert len(synced) == 1 and (tmp_path / "c.bin").read_bytes() == b"data"
    folders = []
    monkeypatch.setattr(safety, "fsync_dir", lambda p: folders.append(Path(p)))
    (tmp_path / "t" / "sub").mkdir(parents=True)
    safety.fsync_tree(tmp_path / "t")
    assert tmp_path / "t" in folders and tmp_path / "t" / "sub" in folders and tmp_path in folders
