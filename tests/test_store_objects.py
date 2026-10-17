"""Version store B-2: the content-addressed ObjectStore (D3 T1, T2)."""
from __future__ import annotations

import hashlib
import os

import pytest

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


def test_put_refuses_a_digest_that_is_not_the_bytes_hash(tmp_path):
    objs = _objects(tmp_path)
    with pytest.raises(ValueError):
        objs.put(DATA, _sha(b"other"))
    with pytest.raises(ValueError):
        objs.path_for("../" + "a" * 61)
    assert list(objs.digests()) == []


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
