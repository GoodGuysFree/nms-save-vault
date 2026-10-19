"""Play time over a vault that holds a version store: store entries are read from their
manifests, like catalog folders, so the backup counts and the history lookups see them."""
from __future__ import annotations

import pytest

from nms_save_vault.core import operations, playtime, safety
from nms_save_vault.core import store as vstore
from nms_save_vault.core.catalog import Vault

from .store_helpers import IDENT, steam_live


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache-home"))
    monkeypatch.setattr(safety, "is_game_running", lambda: False)
    monkeypatch.setattr(vstore.versionstore.discover, "all_live_paths", lambda *a, **k: [])


def _store_vault(tmp_path):
    live = steam_live(tmp_path / "NMS")
    vault = Vault(tmp_path / "vault")
    vault.ensure()
    vstore.create_store(vault.root)
    return vault, live


def test_store_entries_count_as_backups_of_a_run(tmp_path):
    vault, live = _store_vault(tmp_path)
    first = operations.create_full_backup(vault, live, label="one", warnings=[])
    second = operations.create_full_backup(vault, live, label="two", warnings=[])
    assert vault.entries == []                       # nothing in the old catalog

    report = playtime.collect(vault, [live])

    run = report.find(live, 1)
    assert run is not None and run.backups == 2 and run.identified
    assert run.key == f"{IDENT:016X}"
    assert run.play_time == 7200 and run.name == "Main"
    assert {(playtime.entry_location(e.id), 1) for e in (first, second)} <= run.locations
    assert report.find(live, 2).backups == 2         # the other run, backed up as well
    assert report.folders == 3                       # the live folder and the two entries


def test_a_store_that_cannot_be_opened_leaves_the_live_saves_counted(tmp_path, monkeypatch):
    vault, live = _store_vault(tmp_path)
    operations.create_full_backup(vault, live, label="one", warnings=[])
    from nms_save_vault.core.store import classic

    def broken(*a, **k):
        raise OSError("the drive went away")
    monkeypatch.setattr(classic, "store_of", broken)

    report = playtime.collect(vault, [live])

    assert report.find(live, 1).backups == 0 and report.find(live, 1).copies >= 1
