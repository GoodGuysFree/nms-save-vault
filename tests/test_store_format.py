"""Version store B-1: FORMAT, vault mode, opening with the compatibility checks, temp sweep."""
from __future__ import annotations

import json
import os
import pathlib
import time
from contextlib import contextmanager

import pytest

from nms_save_vault.core import safety
from nms_save_vault.core import store as vstore
from nms_save_vault.core.store import versionstore

from .store_helpers import FIXED_NOW, new_vault, open_vault_store


def _edit_format(vault, **changes):
    path = vault / "store" / "FORMAT"
    d = json.loads(path.read_text("ascii"))
    d.update(changes)
    path.write_text(json.dumps(d), "ascii")


def test_vault_mode_follows_format_and_upgrade_marker(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    assert vstore.vault_mode(vault) == "legacy"
    vstore.create_store(vault)
    assert vstore.vault_mode(vault) == "store"
    (vault / "store" / "state" / "upgrade.json").write_text("{}", "ascii")
    assert vstore.vault_mode(vault) == "upgrading"


def test_create_store_lays_out_folders_and_writes_format(tmp_path):
    vault = new_vault(tmp_path)
    root = vault / "store"
    for name in ("objects", "entries", "quarantine", "tmp", "state"):
        assert (root / name).is_dir()
    d = json.loads((root / "FORMAT").read_bytes().decode("ascii"))
    assert d["format"] == "nmsvault-store"
    assert (d["version"], d["read_compat"], d["write_compat"]) == (1, 1, 1)
    assert (d["hash"], d["fanout"], d["inline_max"]) == ("sha256", 2, 8192)
    assert d["created_utc"] == "2026-10-16T11:15:00Z"
    assert len(d["store_id"]) == 32
    assert str(tmp_path) not in (root / "FORMAT").read_text("ascii")
    with pytest.raises(vstore.StoreUnavailable):
        vstore.create_store(vault)


def test_open_reads_the_format(tmp_path):
    vault = new_vault(tmp_path)
    store = open_vault_store(vault)
    assert store.store_id == json.loads((vault / "store" / "FORMAT").read_text("ascii"))["store_id"]
    assert not store.read_only
    assert store.inline_max == vstore.INLINE_MAX


def test_open_refuses_a_legacy_or_upgrading_vault(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    with pytest.raises(vstore.StoreNotUpgraded, match="Upgrade the vault first"):
        open_vault_store(vault)
    vstore.create_store(vault)
    (vault / "store" / "state" / "upgrade.json").write_text("{}", "ascii")
    with pytest.raises(vstore.StoreNotUpgraded, match="being upgraded"):
        open_vault_store(vault)


def test_open_of_a_missing_vault_or_a_bad_format_is_unavailable(tmp_path):
    with pytest.raises(vstore.StoreUnavailable):
        open_vault_store(tmp_path / "gone")
    vault = new_vault(tmp_path)
    (vault / "store" / "FORMAT").write_text("{not json", "ascii")
    with pytest.raises(vstore.StoreUnavailable):
        open_vault_store(vault)
    (vault / "store" / "FORMAT").write_text(json.dumps({"format": "nmsvault-store"}), "ascii")
    with pytest.raises(vstore.StoreUnavailable):
        open_vault_store(vault)


def test_a_store_that_needs_a_newer_app_to_read_is_refused(tmp_path):
    vault = new_vault(tmp_path)
    _edit_format(vault, version=2, read_compat=2, write_compat=2)
    with pytest.raises(vstore.StoreTooNew):
        open_vault_store(vault)


def test_an_unknown_layout_is_refused(tmp_path):
    vault = new_vault(tmp_path)
    _edit_format(vault, fanout=3)
    with pytest.raises(vstore.StoreTooNew):
        open_vault_store(vault)


def test_a_store_that_needs_a_newer_app_to_write_opens_read_only(tmp_path):
    vault = new_vault(tmp_path)
    _edit_format(vault, version=2, write_compat=2)
    store = open_vault_store(vault)
    assert store.read_only and store.read_only_reason == "too-new"
    assert store.warnings
    with pytest.raises(vstore.StoreTooNew):
        store._require_writable()


def test_an_unwritable_medium_opens_read_only(tmp_path, monkeypatch):
    vault = new_vault(tmp_path)
    real = pathlib.Path.write_bytes

    def refuse_probe(self, data):
        if self.name.startswith("probe."):
            raise PermissionError(13, "read-only file system")
        return real(self, data)

    monkeypatch.setattr(pathlib.Path, "write_bytes", refuse_probe)
    store = open_vault_store(vault)
    assert store.read_only_reason == "medium"
    with pytest.raises(vstore.StoreUnavailable):
        store._require_writable()


def test_no_file_locking_opens_read_only_but_a_busy_lock_does_not(tmp_path):
    vault = new_vault(tmp_path)

    @contextmanager
    def unlocked():
        yield safety.LockHandle(vault / "x", warning="file locking is not supported")

    assert open_vault_store(vault, lock=unlocked).read_only_reason == "no-lock"

    @contextmanager
    def busy():
        raise safety.LockTimeout("timed out")
        yield

    assert not open_vault_store(vault, lock=busy).read_only


def test_writes_recheck_the_mode(tmp_path):
    vault = new_vault(tmp_path)
    store = open_vault_store(vault)
    store._require_writable()
    (vault / "store" / "state" / "upgrade.json").write_text("{}", "ascii")
    with pytest.raises(vstore.StoreNotUpgraded):
        store._require_writable()


def test_open_sweeps_only_old_own_temp_items(tmp_path):
    vault = new_vault(tmp_path)
    tmp = vault / "store" / "tmp"
    old = time.time() - 2 * 3600
    old_part = tmp / "ab.1.cd.part"
    young_part = tmp / "ef.1.gh.part"
    old_mat = tmp / "mat-full-steam-1-abcd"
    foreign = tmp / "notes.txt"
    for p in (old_part, young_part, foreign):
        p.write_bytes(b"x")
    old_mat.mkdir()
    (old_mat / "save.hg").write_bytes(b"x")
    for p in (old_part, foreign, old_mat):
        os.utime(p, (old, old))
    open_vault_store(vault)
    assert not old_part.exists() and not old_mat.exists()
    assert young_part.exists() and foreign.exists()


def test_open_store_uses_the_organizer_vault_lock(tmp_path):
    vault = new_vault(tmp_path)
    store = vstore.open_store(vault, clock=lambda: FIXED_NOW)
    assert (vault / "organizer" / ".lock").is_file()
    assert store.app_version
    with versionstore.vault_lock(vault)():
        with pytest.raises(RuntimeError, match="not re-entrant"):
            store._probe_writable()
