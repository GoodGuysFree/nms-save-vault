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

from nms_save_vault.core import operations, safety, slotmap
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


@pytest.fixture(autouse=True)
def game_not_running(monkeypatch):
    """The hook journals slots only when the game is known not to run; never ask the OS."""
    monkeypatch.setattr(safety, "is_game_running", lambda: False)

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




# --- chunk B review -----------------------------------------------------------


















def test_expected_pair_takes_the_later_record_of_one_second(tmp_path):
    store = OrganizerStore(tmp_path, create=True, clock=lambda: 5000)
    recorder = LineageRecorder(store, Journal(store, {}))
    t9 = LiveTarget("steam", ACCOUNT, 9)
    recorder.record_op(OpLineage(OP1, "place", 5000, t9, (V(2), None), None))
    recorder.record_op(OpLineage("op-20261016T080055Z-0002", "place", 5000, t9, (V(3), None),
                                 (V(2), None)))
    assert recorder.expected_pair(t9) == (V(3), None)




# --- chunk C review -----------------------------------------------------------






# --- chunk D review -----------------------------------------------------------


