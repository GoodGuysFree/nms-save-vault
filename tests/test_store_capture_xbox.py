"""Version store B-5: Xbox captures - verbatim wgs folders, slot scopes with a rebuilt index,
xgs folders (D3 T4, records part; the materialize part is in test_store_materialize)."""
from __future__ import annotations

import hashlib
import os

import pytest

from nms_save_vault.core import msstore
from nms_save_vault.core import store as vstore
from nms_save_vault.core.catalog import Vault
from nms_save_vault.core.operations import extract_slot
from nms_save_vault.core.store import capture

from .conftest import chunked, wgs_playthrough_id, xbox_meta
from .store_helpers import fingerprint, new_vault, open_vault_store

WHEN = 1_790_000_000
BIG = "y" * 9000


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def wgs_live(make_wgs_account, parent):
    return make_wgs_account(parent, [
        ("Slot1Auto", '{"a":"' + BIG + '"}', "Main", "On foot", 3600, WHEN),
        ("Slot1Manual", '{"m":"' + BIG + '"}', "Main", "In ship", 3500, WHEN - 100),
        ("Slot2Auto", '{"s":2}', "Second", "", 60, WHEN - 5000),
        ("AccountData", '{"acct":"' + BIG + '"}', "", "", 0, WHEN - 9000),
    ])


def _xbox_add(store, acct, kind="snapshot", scope=vstore.CaptureScope()):
    return store.add_from_live(acct, platform="xbox", account_key=acct.name, kind=kind,
                               label=f"test {kind}", scope=scope)


def test_capture_xbox_exact_tree(tmp_path, make_wgs_account):
    vault = new_vault(tmp_path)
    acct = wgs_live(make_wgs_account, tmp_path / "live")
    before = fingerprint(acct)
    store = open_vault_store(vault)
    entry = _xbox_add(store, acct)

    assert fingerprint(acct) == before
    assert {f.path: (f.size, f.mtime_ns, f.sha256) for f in entry.files} == before
    for fe in entry.files:
        assert store.read_file(entry.id, fe.path) == (acct / fe.path).read_bytes()
    assert entry.platform == "xbox" and entry.xbox == {"index": "containers.index",
                                                       "index_kind": "verbatim"}
    assert entry.scope == {"slots": "all", "account_files": True, "cache": "none"}
    _info, containers = msstore.parse_containers_index(acct)
    account = next(c for c in containers if c.identifier == "AccountData")
    assert entry.account == {"data": f"{account.dir_guid}/{account.data_file.name}",
                             "meta": f"{account.dir_guid}/{account.meta_file.name}"}

    assert [(m.slot, m.member) for m in entry.members] == [(1, "A"), (1, "B"), (2, "A")]
    view = msstore.scan(acct)
    for m in entry.members:
        mv = getattr(view.slots[m.slot], m.member.lower())
        assert m.data == mv.data_path.relative_to(acct).as_posix()
        assert m.vid == _sha(mv.data_path.read_bytes())
        assert m.meta_plain_sha256 == _sha(mv.meta_path.read_bytes()) == entry.file(m.meta).sha256
        assert m.ordinal is None and m.valid
        assert m.xbox_identifier == mv.xbox.identifier
        assert m.pid == f"{wgs_playthrough_id(m.xbox_identifier):016X}"
        assert (m.name, m.play_time, m.meta_ts) == (mv.save_name, mv.info.total_play_time,
                                                    mv.timestamp)
        assert abs(m.file_time_ns - mv.data_mtime * 1e9) < 1000
    assert entry.member(1, "A").summary == "On foot"


def test_slot_scope_matches_the_classic_extract(tmp_path, make_wgs_account):
    vault = new_vault(tmp_path)
    acct = wgs_live(make_wgs_account, tmp_path / "live")
    store = open_vault_store(vault)
    entry = _xbox_add(store, acct, kind="extract", scope=vstore.CaptureScope(slots=(1,)))

    legacy = Vault(tmp_path / "legacy")
    folder = tmp_path / "legacy" / "extracts" / extract_slot(legacy, acct, 1).id
    expected = {rel: sha for rel, (_size, _mtime, sha) in fingerprint(folder).items()}
    assert {f.path: f.sha256 for f in entry.files} == expected
    assert entry.xbox == {"index": "containers.index", "index_kind": "rebuilt"}
    assert entry.scope == {"slots": [1], "account_files": False, "cache": "none"}
    assert entry.account == {}
    index = store.read_file(entry.id, "containers.index")
    assert [i for i, _ in msstore.split_index(index)[1]] == ["Slot1Auto", "Slot1Manual"]
    assert entry.file("containers.index").mtime_ns == (acct / "containers.index").stat().st_mtime_ns
    assert [(m.slot, m.member) for m in entry.members] == [(1, "A"), (1, "B")]


def test_an_index_changed_during_a_slot_capture_is_caught(tmp_path, make_wgs_account, monkeypatch):
    vault = new_vault(tmp_path)
    acct = wgs_live(make_wgs_account, tmp_path / "live")
    real = capture._read_file

    def xbox_app_syncs(path):
        st = (acct / "containers.index").stat()
        os.utime(acct / "containers.index", ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
        return real(path)

    monkeypatch.setattr(capture, "_read_file", xbox_app_syncs)
    with pytest.raises(vstore.SourceChanged, match="containers.index"):
        _xbox_add(open_vault_store(vault), acct, kind="extract",
                  scope=vstore.CaptureScope(slots=(2,)))


def test_a_whole_capture_catches_an_index_rewritten_meanwhile(tmp_path, make_wgs_account,
                                                              monkeypatch):
    vault = new_vault(tmp_path)
    acct = wgs_live(make_wgs_account, tmp_path / "live")
    index = acct / "containers.index"
    real = capture._read_file
    done = []

    def xbox_app_rewrites_the_index(path):
        data = real(path)
        if path == index and not done:      # after the capture read it: same size and time
            st = index.stat()
            raw = bytearray(index.read_bytes())
            at = raw.find("synchex".encode("utf-16-le"))
            raw[at] ^= 0x01
            index.write_bytes(bytes(raw))
            os.utime(index, ns=(st.st_atime_ns, st.st_mtime_ns))
            done.append(1)
        return data

    monkeypatch.setattr(capture, "_read_file", xbox_app_rewrites_the_index)
    store = open_vault_store(vault)
    with pytest.raises(vstore.SourceChanged, match="containers.index"):
        _xbox_add(store, acct)
    assert done and store.entries() == []


def test_a_blob_missing_from_a_whole_capture_is_caught(tmp_path, make_wgs_account, monkeypatch):
    vault = new_vault(tmp_path)
    acct = wgs_live(make_wgs_account, tmp_path / "live")
    real_walk = capture.walk
    slot2 = next(m for m in msstore.scan(acct).slots[2].members if m.exists)
    blob = slot2.data_path.relative_to(acct).as_posix()
    monkeypatch.setattr(capture, "walk", lambda folder: [r for r in real_walk(folder) if r != blob])
    with pytest.raises(vstore.SourceChanged, match=blob.split("/")[-1]):
        _xbox_add(open_vault_store(vault), acct)


def test_progress_reaches_the_total(tmp_path, make_wgs_account):
    vault = new_vault(tmp_path)
    acct = wgs_live(make_wgs_account, tmp_path / "live")
    calls = []
    entry = open_vault_store(vault).add_from_folder(
        acct, kind="extract", label="x", source={"type": "folder", "name": acct.name},
        scope=vstore.CaptureScope(slots=(1,)), progress=lambda d, t: calls.append((d, t)))
    assert calls[-1] == (len(entry.files), len(entry.files))
    assert [d for d, _ in calls] == list(range(1, len(entry.files) + 1))


def test_an_empty_xbox_slot_has_nothing_to_keep(tmp_path, make_wgs_account):
    vault = new_vault(tmp_path)
    acct = wgs_live(make_wgs_account, tmp_path / "live")
    with pytest.raises(vstore.StoreError, match="holds no saves"):
        _xbox_add(open_vault_store(vault), acct, kind="extract",
                  scope=vstore.CaptureScope(slots=(5,)))


def _xgs_account(parent):
    acct = parent / "xgs" / "0000000000000001_00000000000000000000000000000001"
    for ident, payload, name, play in (("Slot1Auto", '{"a":"' + BIG + '"}', "Main", 3600),
                                       ("Slot3Manual", '{"b":1}', "Third", 50),
                                       ("AccountData", '{"acct":1}', "", 0)):
        d = acct / ident
        d.mkdir(parents=True)
        (d / "data").write_bytes(chunked(payload.encode()))
        (d / "meta").write_bytes(xbox_meta(name, play=play, size=len(payload),
                                           pid=wgs_playthrough_id(ident), ts=WHEN))
    return acct


def test_an_xgs_folder_is_kept_with_its_identifier_folders(tmp_path):
    vault = new_vault(tmp_path)
    acct = _xgs_account(tmp_path)
    store = open_vault_store(vault)
    whole = _xbox_add(store, acct)
    assert whole.xbox is None
    assert whole.account == {"data": "AccountData/data", "meta": "AccountData/meta"}
    assert [(m.slot, m.member, m.data, m.xbox_identifier) for m in whole.members] == [
        (1, "A", "Slot1Auto/data", "Slot1Auto"), (3, "B", "Slot3Manual/data", "Slot3Manual")]
    assert whole.member(3, "B").pid == f"{wgs_playthrough_id('Slot3Manual'):016X}"

    slot = _xbox_add(store, acct, kind="extract", scope=vstore.CaptureScope(slots=(3,)))
    assert [f.path for f in slot.files] == ["Slot3Manual/data", "Slot3Manual/meta"]
    assert [(m.slot, m.member) for m in slot.members] == [(3, "B")]
