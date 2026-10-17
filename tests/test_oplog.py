"""The op log (oplog.jsonl) survives a crash mid-write.

A record is appended as one complete line, fsynced, and the reader skips (and reports) any
line that is not a record. Before, a torn last line made every later read raise -- undo
included. A damaged NEWEST line makes undo refuse: the last operation is then unknown.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import time

import pytest

from nms_save_vault.core import catalog, safety
from nms_save_vault.core import operations as ops
from nms_save_vault.core import savedir
from nms_save_vault.core.catalog import Vault

TORN = b'{"ts": "2026-10-17T12:01:00", "op": "pro\xc3'   # cut off mid-record, mid-character


def _record(op: str, **extra) -> dict:
    return {"ts": "2026-10-17T12:00:00", "op": op, "entry_id": None, "snapshot_id": None,
            "detail": "", "changed": [], **extra}


def _tear(vault: Vault) -> None:
    with open(vault.oplog_path, "ab") as f:
        f.write(TORN)


def test_a_torn_last_line_is_reported_and_later_appends_still_read(tmp_path, caplog):
    vault = Vault(tmp_path / "vault")
    vault.append_oplog(_record("first"))
    vault.append_oplog(_record("second"))
    _tear(vault)

    with caplog.at_level(logging.WARNING, logger=catalog.__name__):
        read = vault.read_oplog()
    assert [r["op"] for r in read] == ["first", "second"]
    assert read.damaged_lines == [3] and read.lines == [1, 2]
    assert any("line 3" in r.getMessage() for r in caplog.records)

    vault.append_oplog(_record("third"))

    read = vault.read_oplog()
    assert [r["op"] for r in read] == ["first", "second", "third"]
    assert read.damaged_lines == [3] and read.lines == [1, 2, 4] and "3" in read.warning
    lines = vault.oplog_path.read_bytes().split(b"\n")
    assert json.loads(lines[-2])["op"] == "third" and lines[-1] == b""   # on a line of its own


def test_a_record_is_written_whole_even_when_the_os_takes_it_in_pieces(tmp_path, monkeypatch):
    vault = Vault(tmp_path / "vault")
    vault.append_oplog(_record("before"))
    syncs = []
    real_open, real_fsync = open, catalog.os.fsync

    class Trickle:
        """A file whose write() accepts at most 7 bytes per call."""

        def __init__(self, f):
            self._f = f

        def write(self, data):
            return self._f.write(bytes(data[:7]))

        def __getattr__(self, name):
            return getattr(self._f, name)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return self._f.__exit__(*exc)

    def trickling_open(path, mode="r", *args, **kwargs):
        f = real_open(path, mode, *args, **kwargs)
        return Trickle(f) if "a" in mode else f

    monkeypatch.setattr(catalog, "open", trickling_open, raising=False)
    monkeypatch.setattr(catalog.os, "fsync", lambda fd: (syncs.append(fd), real_fsync(fd)))
    detail = "two\nlines, an é and a   separator"

    vault.append_oplog(_record("x", detail=detail))

    assert len(syncs) == 1
    lines = vault.oplog_path.read_bytes().split(b"\n")
    assert len(lines) == 3 and lines[-1] == b""                 # one line per record
    read = vault.read_oplog()
    assert [r["op"] for r in read] == ["before", "x"] and read[1]["detail"] == detail


def test_damage_in_the_middle_leaves_the_valid_records_readable(tmp_path, caplog):
    vault = Vault(tmp_path / "vault")
    vault.ensure()
    lines = [
        json.dumps(_record("one")).encode(),
        b"this is not json",
        b"[1, 2, 3]",
        b'{"op": "bad \xff utf-8"}',
        b"",
        json.dumps(_record("two")).encode(),
    ]
    vault.oplog_path.write_bytes(b"\n".join(lines) + b"\n")

    with caplog.at_level(logging.WARNING, logger=catalog.__name__):
        read = vault.read_oplog()
    assert [r["op"] for r in read] == ["one", "two"]
    assert read.damaged_lines == [2, 3, 4] and read.lines == [1, 6]
    skipped = " ".join(r.getMessage() for r in caplog.records)
    assert "line 2" in skipped and "line 3" in skipped and "line 4" in skipped


def _xbox_live(make_wgs_account, tmp_path):
    return make_wgs_account(tmp_path, [
        ("Slot1Auto", '{"a":1}', "Source", "", 100, 1000),
        ("Slot1Manual", '{"b":2}', "Source", "", 90, 900),
    ])


def test_undo_refuses_when_the_newest_record_is_damaged(make_wgs_account, tmp_path):
    """The torn line may be the operation the user wants undone; undoing the newest READABLE
    one instead would put back an older state, so undo refuses and says why."""
    live = _xbox_live(make_wgs_account, tmp_path)
    vault = Vault(tmp_path / "vault")
    ops.repopulate_slot(vault, live, 1, live, 3, allow_game_running=True)
    _tear(vault)

    with pytest.raises(ops.OperationError, match="damaged, so undo is unsafe"):
        ops.undo_last(vault, live, allow_game_running=True)
    assert savedir.scan_any(live).slots[3].occupied                 # nothing was put back


def test_undo_works_past_older_damage(make_wgs_account, tmp_path):
    live = _xbox_live(make_wgs_account, tmp_path)
    vault = Vault(tmp_path / "vault")
    vault.ensure()
    _tear(vault)                                    # an older operation whose record was lost
    ops.repopulate_slot(vault, live, 1, live, 3, allow_game_running=True)

    result = ops.undo_last(vault, live, allow_game_running=True)

    assert not savedir.scan_any(live).slots[3].occupied
    assert not any("damaged" in w for w in result.warnings)   # older damage: nothing to say


def test_undo_refuses_when_a_damaged_record_follows_the_operation(make_wgs_account, tmp_path):
    """repopulate, then a torn record, then a backup: the newest record is fine, but the torn
    one between it and the repopulate may be a later live change, so undo still refuses."""
    live = _xbox_live(make_wgs_account, tmp_path)
    vault = Vault(tmp_path / "vault")
    ops.repopulate_slot(vault, live, 1, live, 3, allow_game_running=True)
    _tear(vault)
    ops.create_full_backup(vault, live, include_cache=False)

    with pytest.raises(ops.OperationError, match="damaged, so undo is unsafe"):
        ops.undo_last(vault, live, allow_game_running=True)
    assert savedir.scan_any(live).slots[3].occupied


def test_the_no_undo_messages_mention_the_damage(make_wgs_account, tmp_path):
    live = _xbox_live(make_wgs_account, tmp_path)
    vault = Vault(tmp_path / "vault")
    vault.ensure()
    _tear(vault)
    ops.create_full_backup(vault, live, include_cache=False)
    with pytest.raises(ops.OperationError, match=r"nothing to undo.*damaged lines \(1\)"):
        ops.undo_last(vault, live, allow_game_running=True)

    result = ops.repopulate_slot(vault, live, 1, live, 3, allow_game_running=True)
    shutil.rmtree(vault.get(result.snapshot_id).path)
    with pytest.raises(ops.OperationError, match=r"can no longer be found.*damaged lines \(1\)"):
        ops.undo_last(vault, live, allow_game_running=True)


def test_the_damage_warning_counts_what_it_does_not_list():
    read = catalog.OplogRead([], damaged_lines=range(1, 13))
    assert "(1, 2, 3, 4, 5 and 7 more)" in read.warning


def test_loading_removes_only_stale_catalog_temp_files(tmp_path, caplog):
    vault = Vault(tmp_path / "vault")
    vault.save()
    stale = vault.root / f"{catalog.CATALOG_TEMP_PREFIX}abc.part"
    fresh = vault.root / f"{catalog.CATALOG_TEMP_PREFIX}def.part"
    other = vault.root / ".tmp_ghi.part"                 # not a catalog temp file
    for p in (stale, fresh, other):
        p.write_bytes(b"{")
    old = time.time() - catalog.STALE_TEMP_SECONDS - 60
    for p in (stale, other):
        os.utime(p, (old, old))

    with caplog.at_level(logging.WARNING, logger=catalog.__name__):
        Vault(vault.root).load()

    assert not stale.exists() and fresh.exists() and other.exists()
    assert any(stale.name in r.getMessage() for r in caplog.records)


def test_a_finished_live_write_is_not_reported_failed_when_the_log_cannot_be_written(
    make_wgs_account, tmp_path, monkeypatch
):
    live = _xbox_live(make_wgs_account, tmp_path)
    vault = Vault(tmp_path / "vault")

    def log_unavailable(record):
        raise OSError("disk full")

    monkeypatch.setattr(vault, "append_oplog", log_unavailable)
    result = ops.repopulate_slot(vault, live, 1, live, 3, allow_game_running=True)

    assert result.ok and savedir.scan_any(live).slots[3].occupied
    assert any("missing from the history" in w for w in result.warnings)


def test_the_catalog_is_written_atomically_and_fsynced(tmp_path, monkeypatch):
    vault = Vault(tmp_path / "vault")
    synced = []
    real_fsync = safety.os.fsync
    monkeypatch.setattr(safety.os, "fsync", lambda fd: (synced.append(fd), real_fsync(fd)))

    vault.save()

    assert synced and json.loads(vault.catalog_path.read_text("utf-8"))["entries"] == []
    assert sorted(p.name for p in vault.root.iterdir() if p.is_file()) == ["catalog.json"]
