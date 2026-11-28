"""A backup cut off by a power-out in a vault not yet upgraded is found and removed, and the
owner is told, at the next start (GGF 2026-11-28 rule 2; crash-safety audit gap 6).

The catalog lists an entry only once its folder is complete, so a listed entry is always
whole; a folder cut off before that was left behind unlisted and never found. Each new folder
now gets a marker beside it before it is made (``<id>.incomplete``), taken away once the
catalog lists it."""
from __future__ import annotations

from pathlib import Path

import pytest

from nms_save_vault.core import operations as ops
from nms_save_vault.core.catalog import Vault

from .conftest import steam_save


@pytest.fixture
def legacy(tmp_path):
    live = tmp_path / "NMS" / "st_76561198000000001"
    steam_save(live, 1, 0, "Main", 100, 0x11, 1_700_000_000)
    vault = Vault(tmp_path / "vault")
    vault.ensure()
    return vault, live


def _markers(vault):
    return sorted(p.name for d in (vault.backups_dir, vault.snapshots_dir, vault.extracts_dir)
                  for p in d.glob("*" + ops.INCOMPLETE))


def test_a_folder_being_written_is_marked_until_the_catalog_lists_it(legacy, monkeypatch):
    vault, live = legacy
    seen = []
    real = ops._verify_copy

    def verify(*a, **k):
        seen.extend(_markers(vault))
        return real(*a, **k)
    monkeypatch.setattr(ops, "_verify_copy", verify)
    entry = ops.create_full_backup(vault, live, include_cache=False)
    assert seen == [entry.id + ops.INCOMPLETE]
    assert _markers(vault) == [] and Path(entry.path).is_dir()


def test_a_cut_off_backup_is_removed_at_the_next_start_and_said(legacy):
    vault, live = legacy
    kept = ops.create_full_backup(vault, live, include_cache=False)
    cut = vault.backups_dir / "full-cut-off"                     # as a power-out leaves it
    (vault.backups_dir / ("full-cut-off" + ops.INCOMPLETE)).write_bytes(b"")
    cut.mkdir()
    (cut / "save.hg").write_bytes(b"half")
    notice = ops.incomplete_notice(vault.root)
    assert "full-cut-off" in notice and "removed" in notice
    assert not cut.exists() and _markers(vault) == []
    assert Path(kept.path).is_dir() and Vault(vault.root).get(kept.id) is not None


def test_a_marker_left_after_the_catalog_listed_its_folder_only_goes(legacy):
    vault, live = legacy
    entry = ops.create_full_backup(vault, live, include_cache=False)
    (Path(entry.path).parent / (entry.id + ops.INCOMPLETE)).write_bytes(b"")
    assert ops.incomplete_notice(vault.root) == ""
    assert Path(entry.path).is_dir() and _markers(vault) == []


def test_nothing_to_remove_says_nothing(legacy):
    vault, _live = legacy
    assert ops.incomplete_notice(vault.root) == ""
