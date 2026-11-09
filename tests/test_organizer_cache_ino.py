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
