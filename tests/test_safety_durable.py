"""A replace is on disk when it returns (GGF 2026-11-28: a power-out must never leave a write
the journal calls done only half on disk; crash-safety audit gaps 5 and 7).

Windows: ``os.replace`` is not written through, and a folder cannot be synced there, so the
rename could still be in memory when the journal's next line reaches the disk: the replace
asks ``MoveFileExW`` to write it through. Elsewhere: the rename is made durable by syncing the
folder, as the live writes already did, now for every atomic write (state.json, the catalog,
the markers and records of the vault)."""
from __future__ import annotations

import os

import pytest

from nms_save_vault.core import safety
from nms_save_vault.core.liveops import steamwrite
from nms_save_vault.core.store import objects, versionstore


@pytest.mark.skipif(os.name != "nt", reason="Windows' MoveFileExW")
def test_a_replace_on_windows_is_written_through(tmp_path, monkeypatch):
    calls = []
    real = safety._move_file_ex

    def move(src, dst, flags):
        calls.append(flags)
        return real(src, dst, flags)
    monkeypatch.setattr(safety, "_move_file_ex", move)
    target = tmp_path / "state.json"
    target.write_bytes(b"old")
    safety.atomic_write_bytes(target, b"new")
    assert target.read_bytes() == b"new"
    assert calls and all(f & safety.MOVEFILE_WRITE_THROUGH and f & safety.MOVEFILE_REPLACE_EXISTING
                         for f in calls)


@pytest.mark.skipif(os.name != "nt", reason="Windows' MoveFileExW")
def test_a_replace_refused_on_windows_is_a_permission_error_and_is_tried_again(tmp_path,
                                                                              monkeypatch):
    tries = []
    real = safety._move_file_ex

    def held(src, dst, flags):
        tries.append(1)
        if len(tries) < 3:
            raise PermissionError(13, "held open", None, 32)
        return real(src, dst, flags)
    monkeypatch.setattr(safety, "_move_file_ex", held)
    monkeypatch.setattr(safety, "REPLACE_PAUSE", 0)
    target = tmp_path / "f"
    safety.atomic_write_bytes(target, b"x")
    assert target.read_bytes() == b"x" and len(tries) == 3


def test_every_atomic_write_syncs_its_folder_after_the_rename(tmp_path, monkeypatch):
    synced = []
    monkeypatch.setattr(safety, "fsync_dir", lambda p: synced.append(os.path.normcase(str(p))))
    target = tmp_path / "sub" / "catalog.json"
    safety.atomic_write_bytes(target, b"x")
    assert os.path.normcase(str(target.parent)) in synced


def test_the_live_writes_and_the_store_commit_use_the_durable_replace():
    """steamwrite's slot swap, the store's objects and its manifest (the commit point)."""
    for fn in (steamwrite._replace, objects.ObjectStore.put,
               versionstore.VersionStore._write_manifest):
        assert "durable_replace" in fn.__code__.co_names, fn.__qualname__
