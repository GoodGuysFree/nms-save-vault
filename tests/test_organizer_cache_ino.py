"""The scan cache tells a replaced file from the one before it by the file's id."""
from __future__ import annotations

from pathlib import Path

from nms_save_vault.core.organizer.cache import ScanCache, _file_ok

VID = "a" * 64


def test_a_replaced_file_of_the_same_size_and_time_is_not_taken_for_the_old_one(tmp_path):
    cache = ScanCache(tmp_path / "c.json", None)
    path = Path(tmp_path / "save.hg")
    cache.put_file(path, 10, 5, VID, True, "m", ino=1)
    assert cache.file(path, 10, 5, ino=2) is None
    assert cache.file(path, 10, 5, ino=1).vid == VID
    assert cache.file(path, 10, 5, ino=0).vid == VID          # no id on this file system


def test_records_made_before_the_id_was_kept_still_read():
    assert _file_ok([10, 5, VID, True, "m"]) and _file_ok([10, 5, VID, None, "m", 7])
    assert not _file_ok([10, 5, VID, True, "m", True])


def test_the_cache_file_stays_readable_by_a_released_nms_save_vault(tmp_path):
    """Review H-4: v0.3.5 reads file records of exactly five fields and starts afresh
    otherwise (every hash done again each time the two cores take turns). The file id is
    kept apart from them; a cache without it reads as before."""
    import json
    cache = ScanCache(tmp_path / "c.json", "v1")
    path = Path(tmp_path / "save.hg")
    cache.put_file(path, 10, 5, VID, True, "m", ino=7)
    cache.save()
    data = json.loads((tmp_path / "c.json").read_text(encoding="utf-8"))
    assert all(len(v) == 5 for v in data["files"].values())        # v0.3.5's _file_ok
    assert data["inos"] == {str(path): 7}
    again = ScanCache.load(tmp_path / "c.json", "v1")
    assert again.file(path, 10, 5, ino=8) is None and again.file(path, 10, 5, ino=7).vid == VID
    del data["inos"]                                               # as v0.3.5 writes it back
    (tmp_path / "c.json").write_text(json.dumps(data), encoding="utf-8")
    older = ScanCache.load(tmp_path / "c.json", "v1")
    assert older.file(path, 10, 5, ino=8).vid == VID               # no id kept: size and time


def test_a_record_with_the_id_inside_is_rewritten_apart_on_load(tmp_path):
    """Review I-9: a cache written while the id was a sixth field kept that field on a hit
    (so a released NMS Save Vault dropped the cache once more) and the id was not used."""
    import json
    path = Path(tmp_path / "save.hg")
    data = {"schema": 2, "vault_id": "v1", "files": {str(path): [10, 5, VID, True, "m", 7]},
            "entries": {}, "ids": {}, "legacy": {}}
    (tmp_path / "c.json").write_text(json.dumps(data), encoding="utf-8")
    cache = ScanCache.load(tmp_path / "c.json", "v1")
    assert cache.file(path, 10, 5, ino=8) is None and cache.file(path, 10, 5, ino=7).vid == VID
    cache.save()                                                   # nothing else changed
    again = json.loads((tmp_path / "c.json").read_text(encoding="utf-8"))
    assert again["files"] == {str(path): [10, 5, VID, True, "m"]}
    assert again["inos"] == {str(path): 7}
