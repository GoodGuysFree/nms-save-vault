"""Version store B-4: Steam captures - scopes, member records, stability, commit, crash points
(D3 T3, T6, T8, T25, T27)."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time

import pytest

from nms_save_vault.core import formats, meta, safety, slotmap
from nms_save_vault.core import store as vstore
from nms_save_vault.core.store import capture, versionstore

from .conftest import chunked, steam_save
from .store_helpers import (
    FIXED_NOW,
    IDENT,
    STEAM_ACCOUNT,
    fingerprint,
    new_vault,
    open_vault_store,
    steam_live,
)

SNAP = vstore.CaptureScope()
FULL = vstore.CaptureScope(cache="include")


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _live_add(store, live, kind="full", scope=FULL, **kw):
    return store.add_from_live(live, platform="steam", account_key=STEAM_ACCOUNT, kind=kind,
                               label=f"test {kind}", scope=scope, **kw)


def test_capture_steam_exact_tree(tmp_path):
    vault = new_vault(tmp_path)
    live = steam_live(tmp_path / "live")
    before = fingerprint(live)
    store = open_vault_store(vault, clock=lambda: FIXED_NOW)
    entry = _live_add(store, live, op_id="op-20261016T111500Z-ab12", reason="pre-place")

    assert fingerprint(live) == before                      # captures only read
    assert [f.path for f in entry.files] == sorted(before)
    for fe in entry.files:
        size, mtime_ns, digest = before[fe.path]
        assert (fe.size, fe.mtime_ns, fe.sha256) == (size, mtime_ns, digest)
        assert store.read_file(entry.id, fe.path) == (live / fe.path).read_bytes()
        assert (fe.inline is None) == (fe.size > 8192)
    assert re.fullmatch(r"full-steam-\d{8}-\d{6}", entry.id)
    assert (entry.kind, entry.platform, entry.account_key) == ("full", "steam", STEAM_ACCOUNT)
    assert (entry.op_id, entry.reason, entry.app_version) == ("op-20261016T111500Z-ab12",
                                                              "pre-place", "test")
    assert entry.source == {"type": "live", "platform": "steam", "account_key": STEAM_ACCOUNT,
                            "folder": vstore.versionstore.folder_identity(live)}
    assert entry.scope == {"slots": "all", "account_files": True, "cache": "included"}
    assert entry.account == {"data": "accountdata.hg", "meta": "mf_accountdata.hg",
                             "autocloud": "steam_autocloud.vdf"}
    assert entry.created_utc == "2026-10-16T11:15:00Z"
    assert entry.created_local == FIXED_NOW.astimezone().strftime("%Y-%m-%dT%H:%M:%S")
    assert entry.id == f"full-steam-{FIXED_NOW.astimezone():%Y%m%d-%H%M%S}"

    assert [(m.slot, m.member) for m in entry.members] == [(1, "A"), (1, "B"), (2, "A")]
    a = entry.member(1, "A")
    assert a.vid == _sha((live / "save.hg").read_bytes())
    assert (a.data, a.meta, a.ordinal) == ("save.hg", "mf_save.hg", 2)
    assert (a.pid, a.play_time, a.meta_ts, a.name) == (f"{IDENT:016X}", 7200, 1_790_000_000, "Main")
    assert a.valid and a.note == "" and a.xbox_identifier is None
    plain = meta.decrypt((live / "mf_save.hg").read_bytes(), 2)
    assert a.meta_plain_sha256 == _sha(plain)
    assert a.file_time_ns == (live / "save.hg").stat().st_mtime_ns
    assert entry.member(2, "A").pid == f"{IDENT + 1:016X}"

    raw = (vault / "store" / "entries" / f"{entry.id}.json").read_bytes()
    assert vstore.StoreEntry.from_bytes(raw) == entry
    assert str(tmp_path).encode() not in raw and b":\\\\" not in raw
    assert open_vault_store(vault).entries() == [entry]


def test_scopes_pick_the_files_of_each_kind(tmp_path):
    vault = new_vault(tmp_path)
    live = steam_live(tmp_path / "live")
    store = open_vault_store(vault)
    paths = lambda e: [f.path for f in e.files]   # noqa: E731

    snap = _live_add(store, live, kind="snapshot", scope=SNAP)
    assert paths(snap) == sorted(["accountdata.hg", "mf_accountdata.hg", "mf_save.hg",
                                  "mf_save2.hg", "mf_save3.hg", "save.hg", "save2.hg",
                                  "save3.hg", "steam_autocloud.vdf"])
    assert snap.scope == {"slots": "all", "account_files": True, "cache": "none"}

    no_cache = _live_add(store, live, scope=vstore.CaptureScope(cache="omit"))
    assert "notes.txt" in paths(no_cache) and not any(p.startswith("cache/") for p in paths(no_cache))
    assert no_cache.scope["cache"] == "omitted"

    no_account = _live_add(store, live, scope=vstore.CaptureScope(account_files=False))
    assert not {"accountdata.hg", "mf_accountdata.hg", "steam_autocloud.vdf"} & set(paths(no_account))
    assert no_account.account == {}

    extract = _live_add(store, live, kind="extract", scope=vstore.CaptureScope(slots=(2,)))
    assert paths(extract) == ["mf_save3.hg", "save3.hg"]
    assert [(m.slot, m.member) for m in extract.members] == [(2, "A")]
    assert extract.scope == {"slots": [2], "account_files": False, "cache": "none"}

    with pytest.raises(vstore.StoreError, match="holds no saves"):
        _live_add(store, live, kind="extract", scope=vstore.CaptureScope(slots=(9,)))


def test_moved_meta_keeps_ordinal(tmp_path):
    vault = new_vault(tmp_path)
    live = steam_live(tmp_path / "live")
    # Slot 2's A pair copied by hand into slot 3 A without re-keying the meta.
    (live / "save5.hg").write_bytes((live / "save3.hg").read_bytes())
    moved = (live / "mf_save3.hg").read_bytes()
    (live / "mf_save5.hg").write_bytes(moved)
    entry = _live_add(open_vault_store(vault), live, kind="snapshot", scope=SNAP)
    m = entry.member(3, "A")
    assert m.ordinal == slotmap.storage_ordinal(3) != slotmap.storage_ordinal(5)
    assert m.note == "meta keyed for ordinal 4 (moved without re-key)"
    assert entry.file("mf_save5.hg").inline == moved          # kept exactly as found
    plain = meta.decrypt(entry.file("mf_save5.hg").inline, m.ordinal)
    assert int.from_bytes(plain[:4], "little") == formats.META_HEADER
    assert m.valid and m.name == "Second"


def test_invalid_members_are_flagged_not_dropped(tmp_path):
    vault = new_vault(tmp_path)
    live = steam_live(tmp_path / "live")
    (live / "save3.hg").write_bytes(chunked(b'{"short":1}'))      # meta claims another size
    (live / "mf_save2.hg").write_bytes(b"\x00" * 432)            # no ordinal decrypts it
    (live / "save7.hg").write_bytes(b"data without its meta")
    entry = _live_add(open_vault_store(vault), live, kind="snapshot", scope=SNAP)
    resized, unreadable = entry.member(2, "A"), entry.member(1, "B")
    assert not resized.valid and resized.meta_plain_sha256 is not None and resized.note == ""
    assert not unreadable.valid and unreadable.ordinal is None and unreadable.pid is None
    assert unreadable.meta_plain_sha256 is None and unreadable.note.startswith("meta unreadable")
    assert entry.member(4, "A") is None and entry.has_file("save7.hg")
    assert entry.notes == ("slot 4A: save7.hg kept without its partner "
                           "(incomplete: missing meta file)",)


def test_a_slot_scope_keeps_an_unpaired_file_with_a_note(tmp_path):
    vault = new_vault(tmp_path)
    live = steam_live(tmp_path / "live")
    (live / "save7.hg").write_bytes(b"data without its meta")        # slot 4 A
    (live / "mf_save8.hg").write_bytes(b"\x02" * 432)               # slot 4 B, meta only
    entry = _live_add(open_vault_store(vault), live, kind="extract",
                      scope=vstore.CaptureScope(slots=(4,)))
    assert [f.path for f in entry.files] == ["mf_save8.hg", "save7.hg"]
    assert entry.members == ()
    assert [n.split(":")[0] for n in entry.notes] == ["slot 4A", "slot 4B"]


def test_a_save_written_during_the_capture_is_caught(tmp_path, monkeypatch):
    vault = new_vault(tmp_path)
    live = steam_live(tmp_path / "live")
    real = capture._read_file

    def game_saves_slot_5(path):
        if not (live / "save9.hg").exists():
            steam_save(live, 5, 0, "New", 10, IDENT + 5, 1_790_000_100)
        return real(path)

    monkeypatch.setattr(capture, "_read_file", game_saves_slot_5)
    with pytest.raises(vstore.SourceChanged, match="save9.hg"):
        _live_add(open_vault_store(vault), live, kind="snapshot", scope=SNAP)


def test_a_save_deleted_after_it_was_read_is_caught(tmp_path, monkeypatch):
    vault = new_vault(tmp_path)
    live = steam_live(tmp_path / "live")
    store = open_vault_store(vault)
    real = capture.savedir.scan_any

    def game_deletes_slot_2(folder):
        (live / "mf_save3.hg").unlink()
        return real(folder)

    monkeypatch.setattr(capture.savedir, "scan_any", game_deletes_slot_2)
    with pytest.raises(vstore.SourceChanged, match="disappeared"):
        _live_add(store, live, kind="snapshot", scope=SNAP)
    assert store.entries() == []


def test_ids_are_unique_across_catalog_and_store(tmp_path):
    vault = new_vault(tmp_path)
    live = steam_live(tmp_path / "live")
    store = open_vault_store(vault, clock=lambda: FIXED_NOW)
    stamp = f"{FIXED_NOW.astimezone():%Y%m%d-%H%M%S}"
    catalog = {"version": 1, "entries": [{
        "id": f"snapshot-steam-{stamp}", "kind": "snapshot", "label": "", "path": "x",
        "created": "2026-01-01T00:00:00", "managed": True, "slots": []}]}
    (vault / "catalog.json").write_text(json.dumps(catalog), "utf-8")
    ids = [_live_add(store, live, kind="snapshot", scope=SNAP).id for _ in range(3)]
    assert ids == [f"snapshot-steam-{stamp}-2", f"snapshot-steam-{stamp}-3",
                   f"snapshot-steam-{stamp}-4"]


def test_a_given_id_is_kept_verified_on_rerun_and_suffixed_when_different(tmp_path):
    vault = new_vault(tmp_path)
    folder = steam_live(tmp_path / "legacy")
    store = open_vault_store(vault)
    kw = dict(kind="full", label="old backup", source={"type": "folder", "name": folder.name},
              scope=FULL, entry_id="full-steam-20260101-000000",
              origin={"legacy_entry": "full-steam-20260101-000000", "legacy_subdir": "backups"},
              created_local="2026-01-01T00:00:00")
    first = store.add_from_folder(folder, **kw)
    assert (first.id, first.created_local, first.account_key) == (
        "full-steam-20260101-000000", "2026-01-01T00:00:00", None)
    assert first.origin["legacy_entry"] == first.id
    assert store.add_from_folder(folder, **kw) == first
    (folder / "notes.txt").write_bytes(b"changed after the fold")
    assert store.add_from_folder(folder, **kw).id == "full-steam-20260101-000000-L"
    (folder / "notes.txt").write_bytes(b"changed again")
    assert store.add_from_folder(folder, **kw).id == "full-steam-20260101-000000-L2"


def test_a_suffixed_id_is_unique_across_the_catalog_too(tmp_path):
    vault = new_vault(tmp_path)
    folder = steam_live(tmp_path / "legacy")
    store = open_vault_store(vault)
    kw = dict(kind="imported", label="x", source={"type": "folder", "name": "x"},
              entry_id="imported-steam-20260101-000000")
    catalog = {"version": 1, "entries": [{
        "id": "imported-steam-20260101-000000-L", "kind": "imported", "label": "", "path": "x",
        "created": "2026-01-01T00:00:00", "managed": True, "slots": []}]}
    (vault / "catalog.json").write_text(json.dumps(catalog), "utf-8")
    store.add_from_folder(folder, **kw)
    (folder / "notes.txt").write_bytes(b"changed")
    assert store.add_from_folder(folder, **kw).id == "imported-steam-20260101-000000-L2"


def test_a_long_id_keeps_its_suffix_within_the_id_limit(tmp_path):
    vault = new_vault(tmp_path)
    folder = steam_live(tmp_path / "legacy")
    store = open_vault_store(vault)
    long_id = "imported-" + "x" * 119
    kw = dict(kind="imported", label="x", source={"type": "folder", "name": "x"},
              entry_id=long_id)
    assert store.add_from_folder(folder, **kw).id == long_id
    (folder / "notes.txt").write_bytes(b"changed")
    suffixed = store.add_from_folder(folder, **kw)
    assert len(suffixed.id) == 128 and suffixed.id.endswith("-L")
    assert open_vault_store(vault).entry(suffixed.id) == suffixed
    assert open_vault_store(vault).corrupt_manifests == {}


def test_source_changed_during_capture(tmp_path, monkeypatch):
    vault = new_vault(tmp_path)
    live = steam_live(tmp_path / "live")
    store = open_vault_store(vault)
    reads = []
    real = capture._read_file

    def game_writing(path):
        data = real(path)
        if path.name == "save.hg":
            reads.append(1)
            with open(path, "ab") as f:
                f.write(b"!")
        return data

    monkeypatch.setattr(capture, "_read_file", game_writing)
    with pytest.raises(vstore.SourceChanged, match="save.hg"):
        _live_add(store, live, kind="snapshot", scope=SNAP)
    assert len(reads) == capture.STABLE_TRIES
    assert store.entries() == []


def test_a_member_changed_after_it_was_read_is_caught(tmp_path, monkeypatch):
    vault = new_vault(tmp_path)
    live = steam_live(tmp_path / "live")
    store = open_vault_store(vault)
    real = capture.savedir.scan_any

    def game_saves_first(folder):
        st = (live / "mf_save.hg").stat()
        os.utime(live / "mf_save.hg", ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
        return real(folder)

    monkeypatch.setattr(capture.savedir, "scan_any", game_saves_first)
    with pytest.raises(vstore.SourceChanged, match="mf_save.hg"):
        _live_add(store, live, kind="snapshot", scope=SNAP)
    assert store.entries() == []


def test_a_vanished_file_is_a_source_change(tmp_path, monkeypatch):
    vault = new_vault(tmp_path)
    live = steam_live(tmp_path / "live")
    real = capture._read_file

    def deleted(path):
        if path.name == "save2.hg":
            raise FileNotFoundError(2, "gone", str(path))
        return real(path)

    monkeypatch.setattr(capture, "_read_file", deleted)
    with pytest.raises(vstore.SourceChanged, match="disappeared"):
        _live_add(open_vault_store(vault), live, kind="snapshot", scope=SNAP)


@pytest.mark.parametrize("point", ["object.after_tmp", "object.after_replace",
                                   "entry.before_replace", "entry.after_replace"])
def test_crash_points_leave_consistent_store(tmp_path, point):
    vault = new_vault(tmp_path)
    live = steam_live(tmp_path / "live")

    def crash(at):
        if at == point:
            raise RuntimeError(f"power loss at {at}")

    with pytest.raises(RuntimeError):
        _live_add(open_vault_store(vault, faults=crash), live)
    store = open_vault_store(vault)
    visible = store.entries()
    assert len(visible) == (1 if point == "entry.after_replace" else 0)
    for entry in visible:                                   # a visible entry is whole
        for fe in entry.files:
            assert store.file_bytes(fe) == (live / fe.path).read_bytes()
    assert [p.name for p in (vault / "store" / "tmp").iterdir()] == []
    again = _live_add(store, live)
    assert all(store.file_bytes(fe) for fe in again.files)
    assert store.corrupt_manifests == {}


def test_an_object_lost_before_the_commit_is_reported(tmp_path):
    vault = new_vault(tmp_path)
    live = steam_live(tmp_path / "live")
    store = open_vault_store(vault)
    lost = _sha((live / "save.hg").read_bytes())

    def medium_loses_it(at):
        if at == "entry.before_replace":
            store.objects.path_for(lost).unlink()

    store._faults = medium_loses_it
    with pytest.raises(vstore.ObjectMissing):
        _live_add(store, live, kind="snapshot", scope=SNAP)


def test_the_vault_lock_is_held_for_the_commit_only(tmp_path):
    vault = new_vault(tmp_path)
    live = steam_live(tmp_path / "live")
    lock = vault / "organizer" / ".lock"
    seen = {}

    def probe(at):
        if at in ("object.after_tmp", "entry.before_replace") and at not in seen:
            try:
                with safety.exclusive_lock(lock, timeout=0):
                    seen[at] = "free"
            except RuntimeError:
                seen[at] = "held"

    _live_add(open_vault_store(vault, faults=probe), live)
    assert seen == {"object.after_tmp": "free", "entry.before_replace": "held"}


def test_a_busy_lock_fails_only_that_write(tmp_path):
    vault = new_vault(tmp_path)
    live = steam_live(tmp_path / "live")
    store = open_vault_store(vault)
    store._lock = lambda: safety.exclusive_lock(vault / "organizer" / ".lock", timeout=0)
    script = ("import sys, time\nfrom nms_save_vault.core.safety import exclusive_lock\n"
              "with exclusive_lock(sys.argv[1]):\n    print('held', flush=True)\n"
              "    time.sleep(30)\n")
    holder = subprocess.Popen([sys.executable, "-c", script, str(vault / "organizer" / ".lock")],
                              stdout=subprocess.PIPE, text=True, encoding="utf-8")
    try:
        assert holder.stdout.readline().strip() == "held"
        with pytest.raises(vstore.StoreBusy):
            _live_add(store, live, kind="snapshot", scope=SNAP)
    finally:
        holder.kill()
        holder.wait()
    assert not store.read_only
    for _ in range(100):                        # the OS frees a killed holder's lock a moment later
        try:
            entry = _live_add(store, live, kind="snapshot", scope=SNAP)
            break
        except vstore.StoreBusy:
            time.sleep(0.05)
    else:
        raise AssertionError("the lock was never released")
    assert entry.id


def test_space_cancel_and_mode_are_checked_before_anything_is_committed(tmp_path, monkeypatch):
    vault = new_vault(tmp_path)
    live = steam_live(tmp_path / "live")
    store = open_vault_store(vault)
    real_usage = versionstore.shutil.disk_usage
    monkeypatch.setattr(versionstore.shutil, "disk_usage",
                        lambda p: real_usage(p)._replace(free=1024))
    with pytest.raises(vstore.InsufficientSpace, match="needs"):
        _live_add(store, live)
    monkeypatch.undo()
    assert list(store.objects.digests()) == []

    calls = []
    with pytest.raises(vstore.Cancelled):
        store.add_from_folder(live, kind="imported", label="x", source={"type": "folder"},
                              progress=lambda done, total: calls.append((done, total)),
                              cancel=lambda: True)
    assert calls == [(1, calls[0][1])] and store.entries() == []

    (vault / "store" / "state" / "upgrade.json").write_text("{}", "ascii")
    with pytest.raises(vstore.StoreNotUpgraded):
        _live_add(store, live)


def test_add_from_live_refuses_the_wrong_platform(tmp_path):
    vault = new_vault(tmp_path)
    live = steam_live(tmp_path / "live")
    with pytest.raises(vstore.StoreError, match="not a xbox save folder"):
        open_vault_store(vault).add_from_live(live, platform="xbox", account_key="x",
                                              kind="snapshot", label="", scope=SNAP)


_WORKER = """
import sys, time
from pathlib import Path
from nms_save_vault.core import store as vstore
vault, folder, go = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
store = vstore.open_store(vault)
while not go.exists():
    time.sleep(0.005)
entry = store.add_from_folder(folder, kind="imported", label="x",
                              source={"type": "folder", "name": folder.name})
print(entry.id)
"""


def test_two_process_commits(tmp_path):
    vault = new_vault(tmp_path)
    live = steam_live(tmp_path / "live")
    go = tmp_path / "go"
    env = dict(os.environ, PYTHONUTF8="1")
    procs = [subprocess.Popen([sys.executable, "-c", _WORKER, str(vault), str(live), str(go)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                              encoding="utf-8", env=env) for _ in range(2)]
    time.sleep(0.5)
    go.write_bytes(b"")
    outs = [p.communicate(timeout=60) for p in procs]
    assert [p.returncode for p in procs] == [0, 0], outs
    ids = [out.strip() for out, _err in outs]
    assert len(set(ids)) == 2
    store = open_vault_store(vault)
    assert sorted(e.id for e in store.entries()) == sorted(ids)
    for entry in store.entries():
        for fe in entry.files:
            assert store.file_bytes(fe) == (live / fe.path).read_bytes()
