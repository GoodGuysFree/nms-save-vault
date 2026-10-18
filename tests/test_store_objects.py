"""Version store B-2: the content-addressed ObjectStore (D3 T1, T2)."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from nms_save_vault.core import safety
from nms_save_vault.core import store as vstore
from nms_save_vault.core.store.objects import ObjectStore


def _objects(tmp_path, faults=None) -> ObjectStore:
    for name in ("objects", "tmp", "quarantine"):
        (tmp_path / name).mkdir()
    return ObjectStore(tmp_path / "objects", tmp_path / "tmp", tmp_path / "quarantine",
                       faults=faults)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


DATA = b"\xe5\xa1\xed\xfe" + bytes(range(256)) * 40


def test_object_put_get_verifies(tmp_path):
    objs = _objects(tmp_path)
    digest = _sha(DATA)
    result = objs.put(DATA, digest)
    assert result == vstore.PutResult(digest, len(DATA), written=True, healed=False)
    path = tmp_path / "objects" / digest[:2] / digest
    assert objs.path_for(digest) == path and path.read_bytes() == DATA
    assert objs.has(digest) and objs.get(digest, size=len(DATA)) == DATA
    assert list((tmp_path / "tmp").iterdir()) == []

    flipped = bytearray(DATA)
    flipped[100] ^= 0xFF
    path.write_bytes(bytes(flipped))
    with pytest.raises(vstore.ObjectCorrupt):
        objs.get(digest)
    assert path.read_bytes() == bytes(flipped)       # reads never write
    path.write_bytes(DATA[:-1])
    with pytest.raises(vstore.ObjectCorrupt):
        objs.get(digest, size=len(DATA))


def test_get_of_an_absent_object_is_missing(tmp_path):
    with pytest.raises(vstore.ObjectMissing):
        _objects(tmp_path).get(_sha(b"nothing"))


def test_a_wrong_digest_fails_the_read_back(tmp_path):
    objs = _objects(tmp_path)
    wrong = _sha(b"other")
    with pytest.raises(vstore.ObjectCorrupt):
        objs.put(DATA, wrong)
    assert list(objs.digests()) == []
    assert (tmp_path / "quarantine" / f"{wrong}.1").read_bytes() == DATA
    with pytest.raises(ValueError):
        objs.path_for("../" + "a" * 61)


def test_put_trusts_the_callers_digest_and_hashes_only_the_read_back(tmp_path, monkeypatch):
    from nms_save_vault.core.store import objects as objects_module

    def no_hashing(data):
        raise AssertionError("the bytes were hashed again")

    objs = _objects(tmp_path)
    digest = _sha(DATA)
    monkeypatch.setattr(objects_module, "sha256", no_hashing)
    assert objs.put(DATA, digest).written
    assert objs.path_for(digest).read_bytes() == DATA


def test_quarantine_claims_its_name_and_never_overwrites(tmp_path, monkeypatch):
    import pathlib

    objs = _objects(tmp_path)
    digest = _sha(DATA)
    taken = tmp_path / "quarantine" / f"{digest}.1"
    taken.write_bytes(b"quarantined earlier")
    real_exists = pathlib.Path.exists
    # Another process creates <digest>.1 right after any existence check would have run.
    monkeypatch.setattr(pathlib.Path, "exists",
                        lambda self: False if self == taken else real_exists(self))
    objs.put(DATA, digest)
    assert objs.quarantine(digest, "test").name == f"{digest}.2"
    assert taken.read_bytes() == b"quarantined earlier"
    assert (tmp_path / "quarantine" / f"{digest}.2").read_bytes() == DATA


def test_new_folders_are_made_durable(tmp_path, monkeypatch):
    synced = []
    monkeypatch.setattr(safety, "fsync_dir", lambda p: synced.append(Path(p)))
    objs = _objects(tmp_path)
    digest = _sha(DATA)
    objs.put(DATA, digest)
    assert tmp_path / "objects" in synced                    # the new fan-out folder
    (tmp_path / "quarantine").rmdir()
    objs.quarantine(digest, "test")
    assert tmp_path in synced                                # the new quarantine folder
    vault = tmp_path / "vault"
    vault.mkdir()
    synced.clear()
    vstore.create_store(vault)
    assert vault in synced and vault / "store" in synced     # store/ itself, then FORMAT


def test_dedupe_hit_rehashes_and_heals(tmp_path):
    objs = _objects(tmp_path)
    digest = _sha(DATA)
    objs.put(DATA, digest)
    again = objs.put(DATA, digest)
    assert (again.written, again.healed) == (False, False)

    bad = DATA[:50] + b"X" + DATA[51:]
    objs.path_for(digest).write_bytes(bad)
    healed = objs.put(DATA, digest)
    assert (healed.written, healed.healed) == (True, True)
    assert objs.get(digest) == DATA
    kept = tmp_path / "quarantine" / f"{digest}.1"
    assert kept.read_bytes() == bad                  # the bad bytes are kept, never deleted


def test_quarantine_never_overwrites(tmp_path):
    objs = _objects(tmp_path)
    digest = _sha(DATA)
    for n in (1, 2):
        objs.put(DATA, digest)
        assert objs.quarantine(digest, "test").name == f"{digest}.{n}"
    assert sorted(p.name for p in (tmp_path / "quarantine").iterdir()) == [
        f"{digest}.1", f"{digest}.2"]
    assert not objs.has(digest)


def test_digests_lists_objects_only(tmp_path):
    objs = _objects(tmp_path)
    blobs = [DATA, DATA + b"1", b"small"]
    for b in blobs:
        objs.put(b, _sha(b))
    (tmp_path / "objects" / "zz").mkdir()
    (tmp_path / "objects" / _sha(DATA)[:2] / "stray.txt").write_bytes(b"x")
    assert sorted(objs.digests()) == sorted(_sha(b) for b in blobs)
    assert objs.size_of(_sha(b"small")) == 5 and objs.size_of(_sha(b"absent")) is None


def test_a_crash_before_the_rename_leaves_no_object_and_a_rerun_works(tmp_path):
    def crash(point):
        if point == "object.after_tmp":
            raise RuntimeError("power loss")

    objs = _objects(tmp_path, faults=crash)
    digest = _sha(DATA)
    with pytest.raises(RuntimeError):
        objs.put(DATA, digest)
    assert not objs.has(digest)
    assert list((tmp_path / "tmp").iterdir()) == []
    assert _objects_again(tmp_path).put(DATA, digest).written


def _objects_again(tmp_path) -> ObjectStore:
    return ObjectStore(tmp_path / "objects", tmp_path / "tmp", tmp_path / "quarantine")


def test_a_bad_read_back_is_quarantined_and_raised(tmp_path, monkeypatch):
    objs = _objects(tmp_path)
    digest = _sha(DATA)
    real_replace = os.replace

    def lying_medium(src, dst):
        real_replace(src, dst)
        with open(dst, "r+b") as f:
            f.write(b"\x00")

    monkeypatch.setattr(os, "replace", lying_medium)
    with pytest.raises(vstore.ObjectCorrupt):
        objs.put(DATA, digest)
    monkeypatch.undo()
    assert not objs.has(digest)
    assert (tmp_path / "quarantine" / f"{digest}.1").is_file()


def test_a_busy_rename_onto_the_same_bytes_is_a_dedupe(tmp_path, monkeypatch):
    objs = _objects(tmp_path)
    digest = _sha(DATA)
    real_replace = os.replace

    def other_process_wins(src, dst):
        objs.path_for(digest).parent.mkdir(parents=True, exist_ok=True)
        objs.path_for(digest).write_bytes(DATA)     # written meanwhile, and held open
        raise PermissionError(13, "in use")

    monkeypatch.setattr(os, "replace", other_process_wins)
    result = objs.put(DATA, digest)
    monkeypatch.setattr(os, "replace", real_replace)
    assert (result.written, result.healed) == (False, False)
    assert list((tmp_path / "tmp").iterdir()) == []
    assert objs.get(digest) == DATA


def test_object_store_has_no_delete():
    assert not [name for name in dir(ObjectStore) if "delete" in name or "remove" in name]


def _busy_opens(monkeypatch, n):
    """Make the first n opens of the object file fail as a replace in flight does on Windows."""
    real, calls = open, []

    def flaky(file, *a, **k):
        if Path(file).parent.parent.name == "objects" and len(calls) < n:
            calls.append(file)
            raise PermissionError(13, "The process cannot access the file", None, 32)
        return real(file, *a, **k)

    monkeypatch.setattr("builtins.open", flaky)
    monkeypatch.setattr(Path, "read_bytes", lambda self: _read(flaky, self))
    return calls


def _read(opener, path):
    with opener(path, "rb") as f:
        return f.read()


def test_a_busy_object_is_waited_for_by_put_and_get(tmp_path, monkeypatch):
    objs = _objects(tmp_path)
    digest = _sha(DATA)
    assert objs.put(DATA, digest).written
    calls = _busy_opens(monkeypatch, 3)
    result = objs.put(DATA, digest)            # the dedupe re-hash waits it out, still verified
    assert not result.written and not result.healed and len(calls) == 3
    calls.clear()
    calls_more = _busy_opens(monkeypatch, 2)
    assert objs.get(digest, len(DATA)) == DATA and len(calls_more) == 2


def test_an_object_that_stays_busy_fails_with_the_store_error(tmp_path, monkeypatch):
    from nms_save_vault.core.store import objects as objmod

    objs = _objects(tmp_path)
    digest = _sha(DATA)
    objs.put(DATA, digest)
    monkeypatch.setattr(objmod, "BUSY_WAIT_SECONDS", 0.05)
    _busy_opens(monkeypatch, 10**6)
    with pytest.raises(vstore.StoreUnavailable):
        objs.put(DATA, digest)
    with pytest.raises(vstore.StoreUnavailable):
        objs.get(digest)
