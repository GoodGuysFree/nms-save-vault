"""organizer.recorder: the D5-facing recorder API, and the phase-1 hook that serialises the
classic live writes on the live-op lock and journals them as operations (D1 2.6.6-2.6.7)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from nms_save_vault.core import operations, slotmap
from nms_save_vault.core.catalog import Vault
from nms_save_vault.core.organizer import recorder as rec_mod
from nms_save_vault.core.organizer import scanner
from nms_save_vault.core.organizer.cache import ScanCache
from nms_save_vault.core.organizer.journal import Journal, obs_fields
from nms_save_vault.core.organizer.model import LiveTarget, MemberObs, MetaSnap
from nms_save_vault.core.organizer.recorder import LineageRecorder, OpLineage
from nms_save_vault.core.organizer.store import JOURNAL, LIVEOP_LOCK, OrganizerStore
from nms_save_vault.core.state import Source

from .conftest import steam_save

ACCOUNT = "st_76561198000000001"
MAIN = 0xDDDAD1BDD39F9C72
PID = "DDDAD1BDD39F9C72"
T0 = 1_790_000_000
OP1 = "op-20261016T080055Z-3fa2"


def V(n: int) -> str:
    return f"{n:064x}"


def mobs(vid: str, *, play: int, ts: int) -> MemberObs:
    snap = MetaSnap(play, ts, "meta", "Main", "", 2, 1, 0, 4220, 2004, 100, 50, "", True, float(ts))
    return MemberObs("A", vid, PID, "meta", "steam", snap)


# --- the recorder API ---------------------------------------------------------


def test_record_op_is_idempotent_and_describes_what_it_placed(tmp_path):
    store = OrganizerStore(tmp_path, create=True, clock=lambda: 5000)
    recorder = LineageRecorder(store, Journal(store, {}))
    t9 = LiveTarget("steam", ACCOUNT, 9)
    op = OpLineage(OP1, "place", 4000, t9, (V(2), None), (V(1), None),
                   source={"kind": "entry", "entry": "full-steam-20261005-174129", "slot": 9},
                   members={V(2): mobs(V(2), play=300, ts=900)})
    assert recorder.record_op(op) is True
    assert recorder.record_op(op) is False                       # a retry writes nothing
    (raw,) = store.journal_records()
    assert raw["t"] == "op" and raw["kind"] == "place" and raw["at"] == 4000
    assert list(raw["m"]) == [V(2)] and raw["source"]["entry"] == "full-steam-20261005-174129"
    later = OpLineage("op-20261016T080056Z-0001", "place", 100, LiveTarget("steam", ACCOUNT, 3),
                      (V(2), None), None, members={V(2): mobs(V(2), play=300, ts=900)})
    recorder.record_op(later)
    newest = store.journal_records()[-1]
    assert newest["at"] == 4000 and newest["m"] == {}            # never before; described once


def test_expected_pair_and_outcome(tmp_path):
    store = OrganizerStore(tmp_path, create=True)
    journal = Journal(store, {})
    recorder = LineageRecorder(store, journal)
    t9 = LiveTarget("steam", ACCOUNT, 9)
    assert recorder.expected_pair(t9) is None and recorder.op_outcome(OP1, t9) == "unknown"
    recorder.record_op(OpLineage(OP1, "place", 1000, t9, (V(2), None), None,
                                 members={V(2): mobs(V(2), play=300, ts=900)}))
    assert recorder.expected_pair(t9) == (V(2), None)
    assert recorder.op_outcome(OP1, t9) == "pending"
    with store.transaction() as tx:
        tx.append(obs_fields("scan", t9, (V(3), None), {V(3): mobs(V(3), play=400, ts=2000)}, {}))
    assert recorder.op_outcome(OP1, t9) == "confirmed"
    # The op record described what it placed: the journal alone draws the recorded step.
    edge = journal.replay().edges[-1]
    assert (edge.parent, edge.child, edge.tier, edge.method) == (V(2), V(3), "recorded", "op")


# --- the classic-operation hook -----------------------------------------------


def _setup(tmp_path):
    vault = Vault(tmp_path / "vault")
    vault.ensure()
    live = tmp_path / "NMS" / ACCOUNT
    steam_save(live, 9, 0, "Main", 7000, MAIN, T0, extra="live9")
    steam_save(live, 3, 0, "Main", 5000, MAIN, T0 - 100, extra="live3")
    return vault, live


def _records(vault: Vault) -> list[dict]:
    return list(OrganizerStore(vault.root, create=False).journal_records())


def _ops(vault: Vault) -> list[dict]:
    return [r for r in _records(vault) if r["t"] == "op"]


def _data(folder: Path, slot: int, member: int = 0) -> Path:
    return folder / slotmap.data_filename(slotmap.slot_file_numbers(slot)[member])


def _vid(path: Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_a_repopulate_is_journaled_before_and_recorded_after(tmp_path):
    vault, live = _setup(tmp_path)
    backup = operations.create_full_backup(vault, live, warnings=[])
    before = len(_records(vault))
    assert before == 15                       # the backup journaled the folder it read
    evicted = _vid(_data(live, 9))
    placed = _vid(Path(backup.path) / _data(live, 3).name)
    operations.repopulate_slot(vault, Path(backup.path), 3, live, 9)

    ops = _ops(vault)
    assert len(ops) == 1
    op = ops[0]
    assert (op["kind"], op["src"], op["status"]) == ("repopulate", "app", "done")
    assert op["target"]["slot"] == 9 and op["pair"] == [placed, None]
    assert op["evicted"] == [evicted, None]
    assert op["source"] == {"kind": "entry", "entry": backup.id, "slot": 3}
    assert op["preserved"][evicted]["kind"] == "entry"            # the pre-op snapshot keeps it
    assert len(_records(vault)) == before + 1                     # nothing else changed

    steam_save(live, 9, 0, "Main", 5600, MAIN, T0 + 500, extra="played on")   # the game saved
    vault2 = Vault(vault.root)
    result = scanner.scan(vault2, [Source(id="s", platform="steam", account=ACCOUNT,
                                          path=str(live))], ScanCache(None, None),
                          game_running=False)
    journal = Journal(OrganizerStore(vault.root, create=False), result.versions)
    journal.observe(result, src="scan", skip=frozenset())
    state = journal.replay()
    t9 = LiveTarget("steam", ACCOUNT, 9)
    assert state.ops[(op["op_id"], t9)].outcome == "confirmed"
    assert [(e.parent, e.tier, e.method) for e in state.edges if e.target == t9][-1] == (
        placed, "recorded", "op")


def test_the_slot_is_journaled_as_it_was_just_before_the_write(tmp_path):
    vault, live = _setup(tmp_path)
    other = tmp_path / "elsewhere"
    steam_save(other, 3, 0, "Main", 9000, MAIN, T0 + 9, extra="other")
    operations.import_backup(vault, other, warnings=[])        # no look at the live folder
    evicted = _vid(_data(live, 9))
    operations.repopulate_slot(vault, other, 3, live, 9)
    records = _records(vault)
    kinds = [(r["t"], r["target"]["slot"]) for r in records]
    first_op = kinds.index(("op", 9))
    assert ("obs", 9) in kinds[:first_op]                      # journaled before the write
    assert records[kinds.index(("obs", 9))]["pair"] == [evicted, None]
    assert records[first_op]["source"] == {"kind": "entry", "entry": records and _entry_id(vault),
                                           "slot": 3}


def _entry_id(vault: Vault) -> str:
    (entry,) = [e for e in Vault(vault.root).entries if e.kind == "inplace"]
    return entry.id


def test_a_write_that_wrote_nothing_records_nothing(tmp_path):
    vault, live = _setup(tmp_path)
    steam_save(live, 4, 1, "Main", 10, MAIN, T0, extra="b")
    steam_save(live, 4, 0, "Main", 20, MAIN, T0 + 5, extra="a")     # A is already the newest
    for member, when in ((0, T0 + 99), (1, T0)):                    # ahead on every clock
        fno = slotmap.slot_file_numbers(4)[member]
        for path in (live / slotmap.data_filename(fno), live / slotmap.meta_filename(fno)):
            os.utime(path, (when, when))
    result = operations.promote_member(vault, live, 4, 0)
    assert result.snapshot_id is None and _ops(vault) == []


def test_clear_promote_and_restore_are_recorded(tmp_path):
    vault, live = _setup(tmp_path)
    backup = operations.create_full_backup(vault, live, warnings=[])
    operations.clear_slot(vault, live, 3)
    clear = _ops(vault)[-1]
    assert clear["kind"] == "clear" and clear["pair"] == [None, None]
    steam_save(live, 4, 0, "Main", 10, MAIN, T0 - 10, extra="a")
    steam_save(live, 4, 1, "Main", 20, MAIN, T0, extra="b")
    operations.promote_member(vault, live, 4, 0)
    promote = _ops(vault)[-1]
    assert promote["kind"] == "promote" and promote["pair"] == promote["evicted"]
    operations.restore_full(vault, backup, live)
    restored = [r for r in _ops(vault) if r["kind"] == "restore_full"]
    # Slot 3 came back, slot 4 went (the backup did not have it); slot 9 was unchanged.
    assert sorted(r["target"]["slot"] for r in restored) == [3, 4]
    assert len({r["op_id"] for r in restored}) == 1                 # one operation


def test_an_undo_is_one_operation(tmp_path):
    vault, live = _setup(tmp_path)
    operations.clear_slot(vault, live, 3)
    operations.undo_last(vault, live)
    kinds = [r["kind"] for r in _ops(vault)]
    assert kinds == ["clear", "undo"]                       # the restore inside is the undo


def test_a_recording_failure_never_fails_the_operation(tmp_path, monkeypatch, caplog):
    vault, live = _setup(tmp_path)
    backup = operations.create_full_backup(vault, live, warnings=[])

    def broken(*args, **kwargs):
        raise RuntimeError("the organizer is broken")

    monkeypatch.setattr(rec_mod, "scan", broken)
    with caplog.at_level("WARNING"):
        result = operations.repopulate_slot(vault, Path(backup.path), 3, live, 9)
    assert result.ok and _vid(_data(live, 9)) == _vid(Path(backup.path) / _data(live, 3).name)
    assert "was not recorded" in caplog.text and "not journaled" in caplog.text


def test_a_read_only_organizer_does_not_stop_a_write(tmp_path):
    vault, live = _setup(tmp_path)
    backup = operations.create_full_backup(vault, live, warnings=[])
    header = vault.root / "organizer" / "organizer.json"
    data = json.loads(header.read_text(encoding="ascii"))
    data["schema"] = 99
    header.write_text(json.dumps(data), encoding="ascii")
    before = (vault.root / "organizer" / JOURNAL).read_bytes()
    assert operations.repopulate_slot(vault, Path(backup.path), 3, live, 9).ok
    assert (vault.root / "organizer" / JOURNAL).read_bytes() == before


_HOLDER = r"""
import sys, time
from pathlib import Path
from nms_save_vault.core.safety import exclusive_lock

lock, work = Path(sys.argv[1]), Path(sys.argv[2])
with exclusive_lock(lock, timeout=30):
    (work / "ready").write_text("1", encoding="ascii")
    end = time.monotonic() + 60
    while not (work / "release").exists() and time.monotonic() < end:
        time.sleep(0.005)
"""


def test_a_live_write_waits_for_another_windows_write(tmp_path, monkeypatch):
    vault, live = _setup(tmp_path)
    backup = operations.create_full_backup(vault, live, warnings=[])
    monkeypatch.setattr(rec_mod, "LIVEOP_TIMEOUT", 0.3)
    env = dict(os.environ)
    src = str(Path(operations.__file__).resolve().parents[2])
    env["PYTHONPATH"] = os.pathsep.join(p for p in (src, env.get("PYTHONPATH")) if p)
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(vault.root / "organizer" / LIVEOP_LOCK), str(tmp_path)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
    try:
        end = time.monotonic() + 30
        while not (tmp_path / "ready").exists():
            assert holder.poll() is None, holder.communicate()[1]
            assert time.monotonic() < end
            time.sleep(0.005)
        before = _vid(_data(live, 9))
        with pytest.raises(operations.OperationError, match="Another NMS Save Vault window"):
            operations.repopulate_slot(vault, Path(backup.path), 3, live, 9)
        assert _vid(_data(live, 9)) == before                    # nothing was written
        (tmp_path / "release").write_text("1", encoding="ascii")
        holder.communicate(timeout=60)
    finally:
        holder.kill()
    assert operations.repopulate_slot(vault, Path(backup.path), 3, live, 9).ok
