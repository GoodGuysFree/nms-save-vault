"""Version store: each manifest is read once per process (every store opened on one vault
shares what was parsed: the Backups tab, the Organizer, an app's reader), and one that was
deleted or replaced on disk is not served from memory."""
from __future__ import annotations

import shutil

import pytest

from nms_save_vault.core import api
from nms_save_vault.core import store as vstore
from nms_save_vault.core.catalog import Vault
from nms_save_vault.core.organizer import Organizer
from nms_save_vault.core.store import classic, versionstore

from .store_helpers import STEAM_ACCOUNT, new_vault, open_vault_store, steam_live

SNAP = vstore.CaptureScope()


def _snap(store, live, label="t"):
    return store.add_from_live(live, platform="steam", account_key=STEAM_ACCOUNT,
                               kind="snapshot", label=label, scope=SNAP)


def _world(tmp_path, count=3):
    vault = new_vault(tmp_path)
    live = steam_live(tmp_path / "live")
    store = open_vault_store(vault)
    return vault, live, store, [_snap(store, live, f"n{i}") for i in range(count)]


def _parses(monkeypatch) -> list:
    calls: list = []
    real = versionstore.StoreEntry.from_bytes
    monkeypatch.setattr(versionstore.StoreEntry, "from_bytes",
                        staticmethod(lambda raw: calls.append(1) or real(raw)))
    return calls


def _manifest(vault, entry):
    return vault / "store" / "entries" / f"{entry.id}.json"


def test_every_store_of_a_vault_shares_the_manifests_it_read(tmp_path, monkeypatch):
    vault, _live, _writer, made = _world(tmp_path)
    parses = _parses(monkeypatch)
    first = open_vault_store(vault, read_only=True)
    assert {e.id for e in first.entries()} == {e.id for e in made}
    assert len(parses) == len(made)
    second = open_vault_store(vault)
    reader = api.open_vault(vault, live_roots=())
    assert second.entries() == first.entries() == reader.entries()
    assert len(parses) == len(made)                     # read once, by the first store


def test_the_backups_tab_and_the_organizer_read_each_manifest_once(tmp_path, monkeypatch):
    vault, live, _writer, made = _world(tmp_path)
    parses = _parses(monkeypatch)
    listed = classic.list_entries(Vault(vault), live_roots=[live])
    model = Organizer(vault, [], read_only=True).refresh(record=False, request_facts=False)
    assert {e.id for e in listed} == {e.id for e in made} and model is not None
    assert len(parses) == len(made)


def test_a_deleted_manifest_is_forgotten(tmp_path):
    vault, _live, store, (keep, *_rest, gone) = _world(tmp_path)
    vid = gone.members[0].vid
    assert store.has_entry(gone.id) and any(r[0] == gone.id for r in store.vid_refs(vid))
    _manifest(vault, gone).unlink()                     # retired by hand, or by another tool
    assert not store.has_entry(gone.id)
    assert gone.id not in {e.id for e in store.entries()} and keep.id in {
        e.id for e in store.entries()}
    assert all(r[0] != gone.id for r in store.vid_refs(vid))
    with pytest.raises(KeyError):
        store.entry(gone.id)
    assert not open_vault_store(vault).has_entry(gone.id)      # nor handed to the next store


def test_a_manifest_replaced_on_disk_is_read_again(tmp_path):
    vault, _live, store, (entry, *_rest) = _world(tmp_path)
    other = open_vault_store(vault)
    assert other.has_entry(entry.id)                    # parsed, and shared
    path = _manifest(vault, entry)
    path.write_bytes(path.read_bytes()[:-1])            # damaged
    for s in (store, other, open_vault_store(vault)):
        assert not s.has_entry(entry.id)
        assert f"{entry.id}.json" in s.corrupt_manifests


def test_a_copy_of_a_vault_is_read_on_its_own(tmp_path):
    """A copy has the same store id: what is in one is not what is in the other."""
    vault, _live, store, (a, *_rest, b) = _world(tmp_path)
    store.entries()
    copy = tmp_path / "copy"
    shutil.copytree(vault, copy)
    _manifest(copy, b).unlink()
    assert b.id not in {e.id for e in open_vault_store(copy).entries()}
    assert {a.id, b.id} <= {e.id for e in open_vault_store(vault).entries()}
