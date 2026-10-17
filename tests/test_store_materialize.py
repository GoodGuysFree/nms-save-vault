"""Version store B-6: materialize, materialized() and export (D3 T3/T4 tree parts, T26, T29)."""
from __future__ import annotations

import os
import threading
import time

import pytest

from nms_save_vault.core import catalog, discover, msstore, savedir
from nms_save_vault.core import store as vstore
from nms_save_vault.core.catalog import Vault
from nms_save_vault.core.operations import extract_slot
from nms_save_vault.core.store import materialize, versionstore

from .store_helpers import STEAM_ACCOUNT, fingerprint, new_vault, open_vault_store, steam_live
from .test_store_capture_xbox import wgs_live

FULL = vstore.CaptureScope(cache="include")


def _steam_full(store, live, kind="full", scope=FULL):
    return store.add_from_live(live, platform="steam", account_key=STEAM_ACCOUNT, kind=kind,
                               label="t", scope=scope)


def _xbox(store, acct, kind="snapshot", scope=vstore.CaptureScope()):
    return store.add_from_live(acct, platform="xbox", account_key=acct.name, kind=kind,
                               label="t", scope=scope)


def test_export_byte_identical(tmp_path):
    vault = new_vault(tmp_path)
    live = steam_live(tmp_path / "live")
    store = open_vault_store(vault, live_roots=[tmp_path / "live"])
    entry = _steam_full(store, live)
    result = store.export_entry(entry.id, tmp_path / "export" / "copy")
    assert fingerprint(tmp_path / "export" / "copy") == fingerprint(live)
    assert (result.files, result.bytes) == (len(entry.files), sum(f.size for f in entry.files))


def test_capture_steam_exact_tree_scans_the_same(tmp_path):
    vault = new_vault(tmp_path)
    live = steam_live(tmp_path / "live")
    store = open_vault_store(vault)
    entry = _steam_full(store, live, kind="snapshot", scope=vstore.CaptureScope())
    with store.materialized(entry.id) as folder:
        assert catalog.summarize(savedir.scan(folder)) == catalog.summarize(savedir.scan(live))
        assert folder.parent == vault / "store" / "tmp" and folder.name.startswith("mat-")
    assert not folder.exists()


def test_capture_xbox_exact_tree(tmp_path, make_wgs_account):
    vault = new_vault(tmp_path)
    acct = wgs_live(make_wgs_account, tmp_path / "live")
    store = open_vault_store(vault)
    entry = _xbox(store, acct)
    with store.materialized(entry.id) as folder:
        assert fingerprint(folder) == fingerprint(acct)
        assert catalog.summarize(savedir.scan_any(folder)) == catalog.summarize(savedir.scan_any(acct))

    extract = _xbox(store, acct, kind="extract", scope=vstore.CaptureScope(slots=(1,)))
    with store.materialized(extract.id) as folder:
        got, want = savedir.scan_any(folder), savedir.scan_any(acct)
        assert catalog.summarize(got, [1]) == catalog.summarize(want, [1])
        assert not got.slots[2].occupied


def test_slot_account_and_cache_filters(tmp_path, make_wgs_account):
    vault = new_vault(tmp_path)
    store = open_vault_store(vault)
    steam = _steam_full(store, steam_live(tmp_path / "live"))
    paths = lambda res: sorted(fingerprint(res.dest))   # noqa: E731

    only_slot = store.materialize(steam.id, tmp_path / "s1", slots=[2], account_files=False)
    assert paths(only_slot) == ["mf_save3.hg", "save3.hg"]
    with_account = store.materialize(steam.id, tmp_path / "s2", slots=[2])
    assert paths(with_account) == ["accountdata.hg", "mf_accountdata.hg", "mf_save3.hg",
                                   "save3.hg", "steam_autocloud.vdf"]
    no_cache = store.materialize(steam.id, tmp_path / "s3", cache=False)
    assert "notes.txt" in paths(no_cache) and not any(p.startswith("cache/") for p in paths(no_cache))

    acct = wgs_live(make_wgs_account, tmp_path / "xlive")
    xbox = _xbox(store, acct)
    slot2 = next(m for m in xbox.members if m.slot == 2)
    account_dir = xbox.account["data"].split("/")[0]
    got = paths(store.materialize(xbox.id, tmp_path / "x1", slots=[2], account_files=False))
    assert got == sorted(["containers.index"] + [p for p in fingerprint(acct)
                                                if p.split("/")[0] == slot2.data.split("/")[0]])
    got = paths(store.materialize(xbox.id, tmp_path / "x2", slots=[2]))
    assert any(p.startswith(account_dir + "/") for p in got)
    with pytest.raises(ValueError):
        store.materialize(xbox.id, tmp_path / "x3", slots=[16])


def test_a_slot_selection_of_an_xbox_entry_gets_a_rebuilt_index(tmp_path, make_wgs_account):
    vault = new_vault(tmp_path)
    acct = wgs_live(make_wgs_account, tmp_path / "live")
    store = open_vault_store(vault)
    whole = _xbox(store, acct)
    out = store.materialize(whole.id, tmp_path / "slot1", slots=[1], account_files=False).dest

    legacy = Vault(tmp_path / "legacy")
    classic = tmp_path / "legacy" / "extracts" / extract_slot(legacy, acct, 1).id
    shas = lambda folder: {p: sha for p, (_s, _m, sha) in fingerprint(folder).items()}  # noqa: E731
    assert shas(out) == shas(classic)
    index = (out / "containers.index").read_bytes()
    assert [i for i, _ in msstore.split_index(index)[1]] == ["Slot1Auto", "Slot1Manual"]
    assert catalog.summarize(savedir.scan_any(out), [1]) == catalog.summarize(
        savedir.scan_any(acct), [1])

    with_account = store.materialize(whole.id, tmp_path / "slot1a", slots=[1]).dest
    index = (with_account / "containers.index").read_bytes()
    assert [i for i, _ in msstore.split_index(index)[1]] == ["Slot1Auto", "Slot1Manual",
                                                            "AccountData"]


def test_the_sweep_leaves_a_folder_in_use(tmp_path):
    vault = new_vault(tmp_path)
    store = open_vault_store(vault)
    entry = _steam_full(store, steam_live(tmp_path / "live"))
    old = time.time() - 2 * 3600
    with store.materialized(entry.id) as folder:
        os.utime(folder, (old, old))
        opened = []
        worker = threading.Thread(target=lambda: opened.append(open_vault_store(vault)))
        worker.start()
        worker.join()
        assert opened and folder.is_dir()                  # another thread's sweep left it
        open_vault_store(vault)
        assert folder.is_dir()                             # and so did this thread's
        assert (folder / "save.hg").is_file()
    assert not folder.exists()
    assert [p.name for p in (vault / "store" / "tmp").iterdir()] == []


def test_live_folders_include_xgs_twins_and_are_discovered_by_default(tmp_path, make_wgs_account,
                                                                       monkeypatch):
    vault = new_vault(tmp_path)
    acct = wgs_live(make_wgs_account, tmp_path / "live")
    twin = tmp_path / "live" / "xgs" / acct.name
    twin.mkdir(parents=True)
    store = open_vault_store(vault, live_roots=[acct])
    entry = _xbox(store, acct)
    with pytest.raises(vstore.StoreError, match="empty folder"):
        store.export_entry(entry.id, twin / "restored")

    monkeypatch.setattr(discover, "all_live_paths", lambda state, *a, **k: [str(acct)])
    discovering = vstore.VersionStore.open(vault, lock=versionstore.vault_lock(vault),
                                           app_version="test")
    with pytest.raises(vstore.StoreError, match="empty folder"):
        discovering.export_entry(entry.id, acct / "restored")
    with pytest.raises(vstore.StoreError, match="empty folder"):
        discovering.export_entry(entry.id, twin / "restored")
    assert discovering.export_entry(entry.id, tmp_path / "elsewhere").files == len(entry.files)


def test_destinations_are_checked(tmp_path):
    vault = new_vault(tmp_path)
    live = steam_live(tmp_path / "live")
    store = open_vault_store(vault, live_roots=[tmp_path / "live"])
    entry = _steam_full(store, live, kind="snapshot", scope=vstore.CaptureScope())
    (tmp_path / "full").mkdir()
    (tmp_path / "full" / "x").write_bytes(b"x")
    for dest in (vault / "store" / "out", live / "restored", tmp_path / "full", live / "save.hg"):
        with pytest.raises(vstore.StoreError, match="empty folder"):
            store.materialize(entry.id, dest)
    with pytest.raises(vstore.StoreError):
        store.export_entry(entry.id, vault / "exports")
    (tmp_path / "empty").mkdir()
    assert store.materialize(entry.id, tmp_path / "empty").files == len(entry.files)


def test_a_damaged_object_stops_and_cleans_up(tmp_path):
    vault = new_vault(tmp_path)
    live = steam_live(tmp_path / "live")
    store = open_vault_store(vault)
    entry = _steam_full(store, live)
    big = next(f for f in entry.files if f.inline is None)
    store.objects.path_for(big.sha256).write_bytes(b"rot")
    with pytest.raises(vstore.ObjectCorrupt):
        store.materialize(entry.id, tmp_path / "new")
    assert not (tmp_path / "new").exists()
    (tmp_path / "kept").mkdir()
    with pytest.raises(vstore.ObjectCorrupt):
        store.materialize(entry.id, tmp_path / "kept")
    assert (tmp_path / "kept").is_dir() and not any((tmp_path / "kept").iterdir())
    with pytest.raises(vstore.ObjectCorrupt):
        with store.materialized(entry.id):
            pass
    assert [p.name for p in (vault / "store" / "tmp").iterdir()] == []


def test_a_bad_destination_drive_is_caught(tmp_path, monkeypatch):
    vault = new_vault(tmp_path)
    store = open_vault_store(vault)
    entry = _steam_full(store, steam_live(tmp_path / "live"))
    real = materialize._write_file
    monkeypatch.setattr(materialize, "_write_file",
                        lambda path, data: real(path, data[:-1] + b"?" if data else data))
    with pytest.raises(vstore.StoreError, match="does not match"):
        store.export_entry(entry.id, tmp_path / "usb")
    assert not (tmp_path / "usb").exists()


def test_materialized_is_removed_after_an_error_in_the_caller(tmp_path):
    vault = new_vault(tmp_path)
    store = open_vault_store(vault)
    entry = _steam_full(store, steam_live(tmp_path / "live"))
    with pytest.raises(KeyError):
        with store.materialized(entry.id) as folder:
            raise KeyError("caller failed")
    assert not folder.exists()


def test_path_lengths(tmp_path, make_wgs_account):
    base = tmp_path / "v"
    root = base.with_name("v" + "x" * (100 - len(str(base))))
    assert len(str(root)) == 100
    root.mkdir()
    vstore.create_store(root)
    store = open_vault_store(root)
    acct = wgs_live(make_wgs_account, tmp_path / "live")
    entry = _xbox(store, acct)
    longest = max(len(str(p)) for p in (root / "store").rglob("*"))
    assert longest < 260
    with store.materialized(entry.id) as folder:
        assert max(len(str(p)) for p in folder.rglob("*")) < 260
    digest = "f" * 64
    assert len(str(store.objects.path_for(digest))) == 100 + len("/store/objects/ff/") + 64
