"""organizer.store: the vault's organizer folder - header, documents, journal, lock."""
from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from nms_save_vault.core import safety
from nms_save_vault.core.organizer import store as st
from nms_save_vault.core.organizer.store import (
    HEADER,
    JOURNAL,
    NOTES,
    OrganizerStore,
    StoreBusy,
    StoreReadOnly,
)


def _bytes_of(root: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(root.iterdir()) if p.is_file()}


def _ticking(start: int = 1_792_137_600):
    """A clock that moves one second per reading."""
    now = [start - 1]

    def clock() -> float:
        now[0] += 1
        return now[0]

    return clock


# --- header and creation ------------------------------------------------------


def test_create_writes_the_header_once(tmp_path):
    store = OrganizerStore(tmp_path, create=True, clock=lambda: 1_792_137_600)
    header = json.loads((tmp_path / "organizer" / HEADER).read_text(encoding="ascii"))
    assert header["schema"] == 1 and header["kind"] == "nmsvault.organizer"
    assert header["created"] == 1_792_137_600 and header["app"].startswith("nms-save-vault ")
    assert header["vault_id"] == store.vault_id and len(store.vault_id) == 32
    again = OrganizerStore(tmp_path, create=True)
    assert again.vault_id == store.vault_id and not again.read_only


def test_showing_writes_nothing(tmp_path):
    store = OrganizerStore(tmp_path, create=False)
    assert store.vault_id is None and store.journal_records() == ()
    assert store.document(NOTES) == {"schema": 1, "kind": "nmsvault.organizer.notes"}
    assert not (tmp_path / "organizer").exists()


def test_a_forced_read_only_store_never_writes(tmp_path):
    store = OrganizerStore(tmp_path, create=True, read_only=True)
    with pytest.raises(StoreReadOnly):
        with store.transaction():
            pytest.fail("a read-only store opened a transaction")
    assert not (tmp_path / "organizer").exists()


def test_the_first_write_creates_the_store(tmp_path):
    store = OrganizerStore(tmp_path, create=False, clock=lambda: 1_792_137_655)
    with store.transaction() as tx:
        record = tx.append({"t": "obs", "src": "scan"})
    assert store.vault_id is not None
    assert record == {"t": "obs", "id": f"{store.vault_id[:8]}-1", "seq": 1,
                      "at": 1_792_137_655, "src": "scan"}
    assert store.journal_records() == (record,)


# --- documents ----------------------------------------------------------------


def test_documents_are_ascii_and_replaced_with_a_prev_copy(tmp_path):
    store = OrganizerStore(tmp_path, create=True)
    root = tmp_path / "organizer"
    note = "שלום 旅 \U0001f680"   # Hebrew, CJK, emoji
    with store.transaction() as tx:
        tx.doc(NOTES)["text"] = note
    first = (root / NOTES).read_bytes()
    assert first.isascii() and b"\\u05e9" in first
    assert not (root / "notes.json.prev").exists()
    with store.transaction() as tx:
        tx.doc(NOTES)["text"] = "second"
    assert (root / "notes.json.prev").read_bytes() == first
    assert store.document(NOTES)["text"] == "second"

    before = _bytes_of(root)
    with store.transaction() as tx:
        tx.doc(NOTES)            # read, not changed: nothing is rewritten
    assert _bytes_of(root) == before


def test_an_exception_discards_the_transaction(tmp_path):
    store = OrganizerStore(tmp_path, create=True)
    before = _bytes_of(tmp_path / "organizer")
    with pytest.raises(KeyError):
        with store.transaction() as tx:
            tx.doc(NOTES)["text"] = "lost"
            tx.append({"t": "obs"})
            raise KeyError("boom")
    assert _bytes_of(tmp_path / "organizer") == before
    assert store.journal_records() == ()


def test_another_store_sees_the_writes(tmp_path):
    """The GUI and the CLI see each other's writes on their next read."""
    writer = OrganizerStore(tmp_path, create=True)
    reader = OrganizerStore(tmp_path, create=False)
    assert reader.journal_records() == () and "text" not in reader.document(NOTES)
    with writer.transaction() as tx:
        tx.doc(NOTES)["text"] = "from the CLI"
        tx.append({"t": "obs"})
    assert reader.document(NOTES)["text"] == "from the CLI"
    assert [r["seq"] for r in reader.journal_records()] == [1]
    with writer.transaction() as tx:
        tx.append({"t": "op"})
    assert [r["seq"] for r in reader.journal_records()] == [1, 2]   # only the tail is new


@pytest.mark.parametrize("name, content", [
    (HEADER, {"schema": 2, "kind": "nmsvault.organizer", "vault_id": "a" * 32}),
    (NOTES, {"schema": 2, "kind": "nmsvault.organizer.notes"}),
])
def test_a_newer_schema_turns_read_only_and_is_left_alone(tmp_path, name, content):
    store = OrganizerStore(tmp_path, create=True)
    path = tmp_path / "organizer" / name
    path.write_text(json.dumps(content), encoding="ascii")
    before = _bytes_of(tmp_path / "organizer")
    store = OrganizerStore(tmp_path, create=False)
    with pytest.raises(StoreReadOnly):
        with store.transaction() as tx:
            tx.doc(NOTES)["text"] = "would overwrite"
            tx.append({"t": "obs"})
    assert store.read_only and st.NEWER_SCHEMA in store.problems
    assert _bytes_of(tmp_path / "organizer") == before


@pytest.mark.parametrize("raw", [
    b'{"schema": 1, "kind": "nmsvault.organizer.notes", "text": ',    # cut off
    b'\xff\xfe not utf-8',
    b'{"schema": 1, "kind": "somebody.else"}',
    b'{"schema": "1", "kind": "nmsvault.organizer.notes"}',
    b'[1, 2]',
])
def test_a_document_that_does_not_parse_is_never_overwritten(tmp_path, raw):
    OrganizerStore(tmp_path, create=True)
    (tmp_path / "organizer" / NOTES).write_bytes(raw)
    store = OrganizerStore(tmp_path, create=False)
    with pytest.raises(StoreReadOnly):
        with store.transaction() as tx:
            tx.doc(NOTES)["text"] = "would overwrite"
    assert (tmp_path / "organizer" / NOTES).read_bytes() == raw
    assert store.read_only and any(NOTES in p for p in store.problems)
    assert store.document(NOTES)["kind"] == "nmsvault.organizer.notes"   # shown as empty


# --- the journal --------------------------------------------------------------


def test_a_torn_last_line_is_ignored_then_sealed_off(tmp_path):
    store = OrganizerStore(tmp_path, create=True)
    with store.transaction() as tx:
        tx.append({"t": "obs"})
        tx.append({"t": "obs"})
    journal = tmp_path / "organizer" / JOURNAL
    with open(journal, "ab") as f:
        f.write(b'{"t":"obs","id":"x-3","seq":3,"at":17')    # a crash mid-append

    reader = OrganizerStore(tmp_path, create=False)
    assert [r["seq"] for r in reader.journal_records()] == [1, 2]
    assert reader.problems == []        # without the lock it may be an append in progress
    with reader.transaction() as tx:
        assert [r["seq"] for r in tx.journal()] == [1, 2]
        tx.append({"t": "op"})
    assert any("incomplete" in p for p in reader.problems)

    lines = journal.read_bytes().split(b"\n")
    assert lines[2].endswith(b'"at":17') and lines[3].startswith(b'{"t":"op"')  # not merged
    fresh = OrganizerStore(tmp_path, create=False)
    assert [r["seq"] for r in fresh.journal_records()] == [1, 2, 3]
    assert any("damaged line" in p for p in fresh.problems)


def test_lines_that_are_not_records_are_skipped_and_reported(tmp_path):
    store = OrganizerStore(tmp_path, create=True)
    journal = tmp_path / "organizer" / JOURNAL
    journal.write_bytes(b'{"t":"obs","id":"a-1","seq":1,"at":5}\n'
                        b'{"t":"mystery","id":"a-2","seq":2,"at":6}\n'
                        b'{"t":"obs","id":"a-3","seq":true,"at":6}\n'
                        b'[]\n\n'
                        b'{"t":"op","id":"a-4","seq":4,"at":7}\n')
    assert [r["seq"] for r in store.journal_records()] == [1, 4]
    assert any("not a journal record" in p for p in store.problems)
    assert not store.read_only         # the journal is only ever appended to


def test_seq_and_at_are_assigned_and_at_never_goes_backwards(tmp_path):
    store = OrganizerStore(tmp_path, create=True)
    readings = iter([1000, 900, 950, 800])       # the clock jumps back after the first
    store.clock = lambda: next(readings)
    with store.transaction() as tx:
        first = tx.append({"t": "obs"})
        second = tx.append({"t": "obs"})
        backfill = tx.append({"t": "obs", "src": "backfill"}, at=500)
    assert (first["seq"], first["at"]) == (1, 1000)
    assert (second["seq"], second["at"]) == (2, 1000)
    assert (backfill["seq"], backfill["at"]) == (3, 500)      # its own (older) time
    with store.transaction() as tx:
        assert (tx.append({"t": "op"})["seq"], tx.append({"t": "op"})["at"]) == (4, 1000)


def test_appending_many_records_does_not_rescan_the_journal(tmp_path):
    store = OrganizerStore(tmp_path, create=True)
    with store.transaction() as tx:
        tx.append({"t": "obs"})
    with store.transaction() as tx:
        def no_rescan():
            raise AssertionError("append read the whole journal again")

        tx.journal = no_rescan
        records = [tx.append({"t": "obs"}) for _ in range(500)]
    assert [r["seq"] for r in records] == list(range(2, 502))
    assert len(store.journal_records()) == 501


@pytest.mark.parametrize("record, kw", [
    ({"t": "note"}, {}),
    ({"src": "scan"}, {}),
    ({"t": "obs", "seq": 9}, {}),
    ({"t": "obs", "id": "mine"}, {}),
    ({"t": "obs", "x": float("nan")}, {}),
    ({"t": "obs", "x": object()}, {}),
    ({"t": "obs"}, {"at": -1}),
])
def test_bad_records_are_refused_before_anything_is_written(tmp_path, record, kw):
    store = OrganizerStore(tmp_path, create=True)
    with pytest.raises((ValueError, TypeError)):
        with store.transaction() as tx:
            tx.append(record, **kw)
    assert store.journal_records() == ()


# --- the lock -----------------------------------------------------------------


def test_transactions_do_not_nest(tmp_path):
    store = OrganizerStore(tmp_path, create=True)
    with store.transaction():
        with pytest.raises(RuntimeError, match="organizer store lock is not re-entrant"):
            with store.transaction():
                pass


def test_a_held_lock_fails_only_that_transaction(tmp_path):
    store = OrganizerStore(tmp_path, create=True, lock_timeout=0.2)
    held, release = threading.Event(), threading.Event()

    def other_window() -> None:
        with safety.exclusive_lock(tmp_path / "organizer" / ".lock"):
            held.set()
            release.wait(30)

    thread = threading.Thread(target=other_window)
    thread.start()
    try:
        assert held.wait(10)
        with pytest.raises(StoreBusy, match="try again"):
            with store.transaction() as tx:
                tx.append({"t": "obs"})
    finally:
        release.set()
        thread.join(30)
    assert not store.read_only
    with store.transaction() as tx:
        tx.append({"t": "obs"})
    assert len(store.journal_records()) == 1


def test_a_vault_that_refuses_writes_turns_read_only(tmp_path, monkeypatch):
    def refused(*args, **kwargs):
        raise PermissionError(errno.EACCES, "Access is denied")

    monkeypatch.setattr(safety, "exclusive_lock", refused)
    store = OrganizerStore(tmp_path, create=False)
    with pytest.raises(StoreReadOnly):
        with store.transaction():
            pass
    assert store.read_only and any("cannot be written" in p for p in store.problems)


_CHILD = r"""
import sys, time
from pathlib import Path
from nms_save_vault.core.organizer.store import NOTES, OrganizerStore

vault, n, who = Path(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
end = time.monotonic() + 30
while not (vault / "go").exists():
    if time.monotonic() > end:
        sys.exit("never started")
    time.sleep(0.001)
store = OrganizerStore(vault, create=False, lock_timeout=60)
for _ in range(n):
    with store.transaction() as tx:
        doc = tx.doc(NOTES)
        doc["count"] = doc.get("count", 0) + 1
        tx.append({"t": "obs", "who": who})
"""


def test_two_processes_lose_no_update_and_share_one_seq(tmp_path):
    OrganizerStore(tmp_path, create=True)
    env = dict(os.environ)
    src = str(Path(st.__file__).resolve().parents[3])
    env["PYTHONPATH"] = os.pathsep.join(p for p in (src, env.get("PYTHONPATH")) if p)
    n = 100
    procs = [subprocess.Popen([sys.executable, "-c", _CHILD, str(tmp_path), str(n), who],
                              env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, encoding="utf-8")
             for who in ("gui", "cli")]
    try:
        (tmp_path / "go").write_text("1", encoding="ascii")
        for proc in procs:
            _, err = proc.communicate(timeout=120)
            assert proc.returncode == 0, err
    finally:
        for proc in procs:
            proc.kill()
    store = OrganizerStore(tmp_path, create=False)
    assert store.document(NOTES)["count"] == 2 * n
    records = store.journal_records()
    assert [r["seq"] for r in records] == list(range(1, 2 * n + 1))   # unique and dense
    assert len({r["id"] for r in records}) == 2 * n
    assert {r["who"] for r in records} == {"gui", "cli"}
    assert store.problems == []
