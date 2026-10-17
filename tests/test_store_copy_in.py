"""Version store B-14: copy-in of hand-made in-place backup folders, only when asked
(D3 4.3, T18)."""
from __future__ import annotations

import hashlib
import shutil

import pytest

from nms_save_vault import cli
from nms_save_vault.core import operations, safety
from nms_save_vault.core import state as appstate
from nms_save_vault.core import store as vstore
from nms_save_vault.core.catalog import Vault
from nms_save_vault.core.organizer.model import StoreRef
from nms_save_vault.core.store import versionstore

from .store_helpers import steam_live


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache-home"))
    monkeypatch.setattr(safety, "is_game_running", lambda: False)
    monkeypatch.setattr(versionstore.discover, "all_live_paths", lambda *a, **k: [])


@pytest.fixture
def world(tmp_path):
    live = steam_live(tmp_path / "NMS")
    vault = Vault(tmp_path / "vault")
    operations.create_full_backup(vault, live, warnings=[])
    hand = tmp_path / "my copy"
    shutil.copytree(live, hand)
    (hand / "save.hg").write_bytes(b"a hand-made copy that differs")
    handmade = operations.import_backup(vault, hand, copy_into_vault=False, warnings=[])
    alias = operations.import_backup(vault, live, copy_into_vault=False, warnings=[])
    vstore.run_upgrade(vstore.plan_upgrade(vault.root, live_roots=[live]))
    store = vstore.open_store(vault.root, live_roots=[live])
    vstore.run_fold(store, live_roots=[live])
    return vault, live, hand, store, handmade.id, alias.id


def _tree(folder):
    return {p.relative_to(folder).as_posix(): (p.stat().st_mtime_ns,
                                               hashlib.sha256(p.read_bytes()).hexdigest())
            for p in sorted(folder.rglob("*")) if p.is_file()}


def test_copy_in(world):
    vault, live, hand, store, handmade, alias = world
    assert [e.id for e in vstore.not_copied_in(store, live_roots=[live])] == [handmade]
    before = _tree(hand)
    catalog_before = vault.catalog_path.read_bytes()
    report = vstore.copy_in(store, [handmade, alias, "no-such-entry"], live_roots=[live])
    assert report.copied == (handmade,) and report.failed == ()
    assert dict(report.skipped) == {alias: "a live save folder, not a backup",
                                    "no-such-entry": "not a folder kept outside the vault"}
    entry = store.entry(handmade)
    assert (entry.kind, entry.origin["inplace_entry"]) == ("imported", handmade)
    assert entry.source == {"type": "folder", "name": "my copy"}
    assert _tree(hand) == before and vault.catalog_path.read_bytes() == catalog_before
    listed = [e for e in vstore.list_vault_entries(Vault(vault.root), store, live_roots=[live])
              if e.id == handmade]
    assert len(listed) == 1 and listed[0].ref == StoreRef(handmade)
    assert listed[0].origin == "copied-in"
    assert vstore.not_copied_in(store, live_roots=[live]) == []
    assert vstore.copy_in(store, [handmade], live_roots=[live]).skipped == (
        (handmade, "already in the store"),)
    status = vstore.store_status(vault.root, store, live_roots=[live])
    assert status.inplace_not_copied == ()


def test_the_cli_copies_in_only_what_it_is_told(world, monkeypatch, capsys):
    vault, live, hand, store, handmade, _alias = world
    monkeypatch.setattr(appstate, "load", lambda *a, **k: None)
    monkeypatch.setattr(versionstore.discover, "all_live_paths", lambda *a, **k: [str(live)])
    assert cli.main(["--vault", str(vault.root), "store", "copy-in"]) == 0
    assert handmade in capsys.readouterr().out and not store.has_entry(handmade)
    assert cli.main(["--vault", str(vault.root), "store", "copy-in", "--all"]) == 0
    assert "1 folder(s)" in capsys.readouterr().out and store.has_entry(handmade)
    assert cli.main(["--vault", str(vault.root), "store", "copy-in"]) == 0
    assert "in the vault already" in capsys.readouterr().out
