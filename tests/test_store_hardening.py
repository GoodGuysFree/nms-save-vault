"""Version store: the second review's fixes (B-7..B-11) - failures that must surface as the
right error, one validity rule, kept creation times, older manifests, configured live
sources, and an index that is not re-listed on every lookup."""
from __future__ import annotations

import hashlib
import os
import shutil
import time
from datetime import datetime, timezone

import pytest

from nms_save_vault.core import discover
from nms_save_vault.core import store as vstore
from nms_save_vault.core.catalog import Vault
from nms_save_vault.core.organizer.model import LiveTarget
from nms_save_vault.core.state import AppState, Source
from nms_save_vault.core.store import StoreEntry, capture, versionstore
from nms_save_vault.core.store import objects as objects_module
from nms_save_vault.core.store.d1 import LiveMember, PreserveFailed, VaultLocator, VersionUnavailable

from .store_helpers import FIXED_NOW, STEAM_ACCOUNT, new_vault, open_vault_store, steam_live
from .test_store_manifest import _entry, _reseal

SNAP = vstore.CaptureScope()


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _target(slot: int) -> LiveTarget:
    return LiveTarget("steam", STEAM_ACCOUNT, slot)


def _setup(tmp_path, **kw):
    vault = new_vault(tmp_path)
    live = steam_live(tmp_path / "live")
    store = open_vault_store(vault, **kw)
    return vault, live, store, vstore.StoreLocator(store, live_dir=lambda t: live)


def _snap(store, live):
    return store.add_from_live(live, platform="steam", account_key=STEAM_ACCOUNT,
                               kind="snapshot", label="t", scope=SNAP)


class _OtherReader:
    """A stand-in for D1's folder reader that always has the bytes."""

    def __init__(self, opened):
        self.opened = opened

    def locate(self, vid):
        return [self.opened.location]

    def open(self, vid, prefer=None):
        return self.opened

    def is_durable(self, vid):
        return True

    def preserve(self, members, *, op_id, reason):
        raise PreserveFailed("no")


def test_an_unreachable_vault_is_unavailable_and_the_next_reader_answers(tmp_path, monkeypatch):
    _vault, live, store, loc = _setup(tmp_path)
    _snap(store, live)
    vid = _sha((live / "save.hg").read_bytes())
    good = loc.open(vid)

    def unplugged(*a, **k):
        raise vstore.StoreUnavailable("The vault is not reachable.")

    monkeypatch.setattr(store, "vid_refs", unplugged)
    assert loc.locate(vid) == [] and not loc.is_durable(vid)
    with pytest.raises(VersionUnavailable):
        loc.open(vid)
    assert VaultLocator([loc, _OtherReader(good)], loc).open(vid) is good
    monkeypatch.undo()

    def denied(*a, **k):
        raise PermissionError(13, "access denied")

    monkeypatch.setattr(store.objects, "get", denied)
    with pytest.raises(VersionUnavailable):
        loc.open(vid)
    monkeypatch.setattr(store.objects, "size_of", denied)
    assert not loc.is_durable(vid)


def test_preserve_turns_any_failure_into_preserve_failed(tmp_path, monkeypatch):
    _vault, live, store, loc = _setup(tmp_path)
    member = [LiveMember(_target(1), "A", _sha((live / "save.hg").read_bytes()))]

    def disk_full(data, digest):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(store.objects, "put", disk_full)
    with pytest.raises(PreserveFailed) as failed:
        loc.preserve(member, op_id="op-1", reason="r")
    assert isinstance(failed.value.__cause__, OSError)
    monkeypatch.undo()

    committed = set()
    store._faults = committed.add
    real_get = store.objects.get

    def unplugged_after_the_commit(digest, size=None):
        if "entry.after_replace" in committed:
            raise vstore.StoreUnavailable("The vault is not reachable.")
        return real_get(digest, size)

    monkeypatch.setattr(store.objects, "get", unplugged_after_the_commit)
    with pytest.raises(PreserveFailed) as failed:
        loc.preserve(member, op_id="op-2", reason="r")
    assert isinstance(failed.value.__cause__, vstore.StoreUnavailable)


def test_preserve_refuses_a_save_it_could_never_give_back(tmp_path):
    _vault, live, store, loc = _setup(tmp_path)
    (live / "mf_save2.hg").write_bytes(b"\x00" * 432)            # no ordinal opens it
    vid_b = _sha((live / "save2.hg").read_bytes())
    with pytest.raises(PreserveFailed, match="cannot be read"):
        loc.preserve([LiveMember(_target(1), "B", vid_b)], op_id="op-1", reason="r")
    assert store.entries() == []
    vid_a = _sha((live / "save.hg").read_bytes())                 # its partner is fine
    assert loc.preserve([LiveMember(_target(1), "A", vid_a)], op_id="op-1", reason="r")


def test_preserve_refuses_two_versions_for_one_member(tmp_path):
    _vault, live, store, loc = _setup(tmp_path)
    vid_a = _sha((live / "save.hg").read_bytes())
    with pytest.raises(PreserveFailed, match="two different versions"):
        loc.preserve([LiveMember(_target(1), "A", vid_a), LiveMember(_target(1), "A", "0" * 64)],
                     op_id="op-1", reason="r")
    assert store.entries() == []
    kept = loc.preserve([LiveMember(_target(1), "A", vid_a), LiveMember(_target(1), "A", vid_a)],
                        op_id="op-1", reason="r")
    assert kept[0] == kept[1]


def test_capture_preserve_and_fsck_share_one_validity_rule(tmp_path, monkeypatch):
    monkeypatch.setattr(capture, "steam_valid", lambda info, st: False)
    _vault, live, store, loc = _setup(tmp_path)
    vid_a = _sha((live / "save.hg").read_bytes())
    kept = loc.preserve([LiveMember(_target(1), "A", vid_a)], op_id="op-1", reason="r")
    assert [m.valid for m in store.entry(kept[0].ref.key).members] == [False, False]
    assert store.fsck(deep=True).ok


def test_a_kept_creation_time_is_the_entrys_time_in_utc_too(tmp_path):
    vault = new_vault(tmp_path)
    folder = steam_live(tmp_path / "legacy")
    store = open_vault_store(vault, clock=lambda: FIXED_NOW)
    entry = store.add_from_folder(folder, kind="full", label="old",
                                  source={"type": "folder", "name": "x"},
                                  entry_id="full-steam-20260101-000000",
                                  created_local="2026-01-01T00:00:00")
    expected = datetime(2026, 1, 1).astimezone().astimezone(timezone.utc)
    assert entry.created_utc == expected.strftime("%Y-%m-%dT%H:%M:%SZ")
    with pytest.raises(ValueError):
        store.add_from_folder(folder, kind="full", label="x", source={"type": "folder"},
                              created_local="yesterday")


def test_a_manifest_without_notes_or_absent_still_loads():
    entry = _entry(notes=["slot 4A: save7.hg kept without its partner"],
                   absent={"4A": "incomplete: missing meta file"})
    raw = entry.to_bytes()
    assert b'"notes"' in raw and b'"absent"' in raw
    older = _reseal(raw, lambda d: (d.pop("notes"), d.pop("absent")))
    loaded = StoreEntry.from_bytes(older)
    assert (loaded.notes, loaded.absent) == ((), {})
    assert loaded.to_bytes() == older                 # and it re-serializes exactly
    assert b'"notes"' not in _entry().to_bytes()


def test_configured_live_sources_are_protected_too(tmp_path, monkeypatch):
    vault = new_vault(tmp_path)
    live = steam_live(tmp_path / "configured")
    monkeypatch.setattr(discover, "discover_live_sources", lambda *a, **k: [])
    state = AppState(sources=[Source(id="steam-1", platform="steam", account="1",
                                     path=str(live))])
    store = vstore.VersionStore.open(vault, lock=versionstore.vault_lock(vault),
                                     app_version="test", app_state=state)
    entry = _snap(store, live)
    with pytest.raises(vstore.StoreError, match="empty folder"):
        store.export_entry(entry.id, live / "restored")
    with pytest.raises(vstore.StoreError, match="empty folder"):
        vstore.open_store(vault, app_state=state).export_entry(entry.id, live / "restored")
    catalog_vault = Vault(vault)
    catalog_vault.upsert(catalog_vault.make_entry_from_dir(
        "inplace-20260102-000000", "inplace", "the live folder", live, managed=False))
    assert vstore.list_vault_entries(catalog_vault, None, app_state=state) == []


def test_the_index_is_refreshed_only_when_entries_changes(tmp_path, monkeypatch):
    vault, live, store, _loc = _setup(tmp_path)
    first = _snap(store, live)
    old = time.time() - 3600
    os.utime(vault / "store" / "entries", (old, old))
    store.entries()                                   # one look at the settled folder
    listings = []
    real = os.listdir
    monkeypatch.setattr(os, "listdir", lambda p=".": listings.append(p) or real(p))
    for _ in range(20):
        assert store.has_entry(first.id) and store.entry(first.id) == first
        store.vid_refs(first.members[0].vid)
    assert listings == []
    second = _snap(open_vault_store(vault), live)     # another window commits
    assert store.has_entry(second.id) and listings


def test_a_failed_repair_is_listed_and_the_check_goes_on(tmp_path, monkeypatch):
    vault, live, store, _loc = _setup(tmp_path)
    shutil.copytree(live, vault / "backups" / "full-steam-20260101-000000")
    entry = _snap(store, live)
    digest = entry.file("save.hg").sha256
    store.objects.path_for(digest).unlink()

    def disk_full(data, digest):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(store.objects, "put", disk_full)
    report = store.fsck(repair=True)
    assert not report.ok and report.repaired == 0
    assert len(report.problems) == 1 and "the repair failed" in report.problems[0].detail
    assert {d.problem for d in store.damage()} == {"missing"}


def test_deep_hashes_each_object_once(tmp_path, monkeypatch):
    _vault, live, store, _loc = _setup(tmp_path)
    _snap(store, live)
    _snap(store, live)
    hashed = []
    real = objects_module.sha256
    monkeypatch.setattr(objects_module, "sha256", lambda b: hashed.append(1) or real(b))
    assert store.fsck(deep=True).ok
    assert len(hashed) == len(list(store.objects.digests()))
