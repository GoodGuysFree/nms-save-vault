"""liveops.opjournal: the write-ahead operation journal, its tolerant reader and fold, the
busy targets, the live-op lock, open-op markers and the machine id (D5 2.7, 2.16; T-C15,
T-L1, T-L3)."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from nms_save_vault.core import msstore, safety
from nms_save_vault.core.liveops import MemberWrite
from nms_save_vault.core.liveops import opjournal as jr
from nms_save_vault.core.liveops.steamwrite import FileState, SteamFileWrite, SteamWritePlan
from nms_save_vault.core.organizer import recorder as rec_mod
from nms_save_vault.core.organizer.model import LiveTarget
from nms_save_vault.core.organizer.recorder import LiveOpBusy
from nms_save_vault.core.organizer.store import LIVEOP_LOCK, LOCK, StoreBusy

from .conftest import chunked, xbox_meta
from .liveops_support import STEAM_ACCOUNT, host_in, make_ctx

OP1 = "op-20261016T101502Z-3fa2"
OP2 = "op-20261016T101600Z-0b1c"
OP3 = "op-20261016T101700Z-77aa"
OP4 = "op-20261016T101800Z-c0de"
T7 = LiveTarget("steam", STEAM_ACCOUNT, 7)
T8 = LiveTarget("steam", STEAM_ACCOUNT, 8)
T9 = LiveTarget("steam", STEAM_ACCOUNT, 9)
SHA = "ab" * 32


def preparing(ctx, op_id, *targets, kind="place"):
    return jr.append(ctx, op_id, "preparing",
                     {"kind": kind, "targets": [jr.target_json(t) for t in targets]})


def test_events_append_with_an_envelope_and_fold_into_ops(tmp_path):
    ctx = make_ctx(tmp_path)
    preparing(ctx, OP1, T7)
    jr.append(ctx, OP1, "writing", {"targets": [{"target": jr.target_json(T7)}]})
    jr.append(ctx, OP1, "validated", {"targets": [{"target": jr.target_json(T7), "ok": True}]})
    last = jr.append(ctx, OP1, "done", {"oplog": True, "lineage": True})
    assert last["seq"] == 4 and last["machine"] == ctx.machine_id
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", last["utc"])
    raw = jr.ops_path(ctx).read_bytes()
    assert raw.isascii() and raw.count(b"\n") == 4 and b", " not in raw   # compact lines
    assert list(json.loads(raw.splitlines()[0])) == ["j", "op_id", "seq", "event", "utc",
                                                     "machine", "kind", "targets"]

    preparing(ctx, OP2, T8, kind="swap")
    ops = jr.read_ops(ctx)
    assert list(ops) == [OP1, OP2]
    done, open_ = ops[OP1], ops[OP2]
    assert (done.status, done.open, done.kind, done.targets) == ("done", False, "place", (T7,))
    assert (open_.status, open_.open, open_.kind) == ("preparing", True, "swap")
    assert done.verify_state(T7) == "pending" and open_.verify_state(T8) is None
    jr.append(ctx, OP1, "verify", {"target": jr.target_json(T7), "state": "confirmed",
                                   "detail": "played"})
    again = jr.read_ops(ctx)[OP1]
    assert again.status == "done" and again.verify_state(T7) == "confirmed"


def test_only_known_events_of_real_ops_are_appended(tmp_path):
    ctx = make_ctx(tmp_path)
    with pytest.raises(ValueError):
        jr.append(ctx, OP1, "exploded")
    with pytest.raises(ValueError):
        jr.append(ctx, "op-yesterday", "preparing")
    with pytest.raises(ValueError):
        jr.append(ctx, OP1, "done", {"seq": 9})
    assert not jr.ops_path(ctx).exists()


def test_busy_targets_are_the_open_ops_of_this_machine(tmp_path):
    ctx = make_ctx(tmp_path)
    other = make_ctx(tmp_path, machine_id="m-00000000000b")
    preparing(ctx, OP1, T7)                                    # open
    preparing(ctx, OP2, T8)
    jr.append(ctx, OP2, "abandoned", {"reason": "stale"})      # closed
    preparing(other, OP3, T9)                                  # another machine's
    jr.append(other, OP3, "writing", {"targets": []})
    preparing(ctx, OP4, T9, kind="swap")
    jr.append(ctx, OP4, "failed", {"stage": "apply", "error": "x", "rollback_error": "y"})
    assert jr.busy_targets(ctx) == frozenset({T7, T9})
    assert jr.busy_targets(other) == frozenset({T9})
    assert [r.op_id for r in jr.open_ops(ctx)] == [OP1, OP4]


def test_a_torn_last_line_is_ignored_then_sealed_off(tmp_path, caplog):
    """T-C15: a crash during the `writing` append leaves the op as `preparing`, so recovery
    treats it as never started; the next append never merges with the torn line."""
    ctx = make_ctx(tmp_path)
    preparing(ctx, OP1, T7)
    path = jr.ops_path(ctx)
    with open(path, "ab") as f:
        f.write(b'{"j":1,"op_id":"' + OP1.encode() + b'","seq":2,"event":"writing","utc":"20')
    caplog.set_level("WARNING")
    assert [e["event"] for e in jr.read_events(path)] == ["preparing"]
    assert jr.read_ops(ctx)[OP1].status == "preparing"
    assert "incomplete" in caplog.text

    jr.append(ctx, OP1, "abandoned", {"reason": "not_started"})
    events = jr.read_events(path)
    assert [(e["event"], e["seq"]) for e in events] == [("preparing", 1), ("abandoned", 2)]
    assert "damaged" in caplog.text
    with open(path, "ab") as f:
        f.write(b'{"not": "an event"}\n')
    assert len(jr.read_events(path)) == 2


def test_a_store_lock_timeout_fails_only_that_append(tmp_path):
    """T-L3: another writer holds the store lock; the append raises StoreBusy and writes
    nothing; the next one goes through."""
    ctx = make_ctx(tmp_path, lock_timeout=0.2)
    preparing(ctx, OP1, T7)
    before = jr.ops_path(ctx).read_bytes()
    taken, release = threading.Event(), threading.Event()

    def holder():
        with safety.exclusive_lock(ctx.store.root / LOCK, timeout=5):
            taken.set()
            release.wait(10)

    thread = threading.Thread(target=holder)
    thread.start()
    try:
        assert taken.wait(10)
        with pytest.raises(StoreBusy):
            jr.append(ctx, OP1, "abandoned", {"reason": "not_started"})
        assert jr.ops_path(ctx).read_bytes() == before
    finally:
        release.set()
        thread.join(10)
    jr.append(ctx, OP1, "abandoned", {"reason": "not_started"})
    assert jr.read_ops(ctx)[OP1].status == "abandoned"


def test_the_live_op_lock_serialises_threads_and_classic_writes(tmp_path, monkeypatch):
    """T-L1 (lock level): a second holder waits and gives up with LiveOpBusy; the classic
    hook (D1 2.6.7) takes the same lock, so it is refused while an operation holds it; a
    journal append under the live-op lock takes the store lock without a deadlock."""
    ctx = make_ctx(tmp_path)
    monkeypatch.setattr(rec_mod, "LIVEOP_TIMEOUT", 0.2)
    outcome = {}

    def second(name, fn):
        try:
            fn()
            outcome[name] = "ran"
        except LiveOpBusy:
            outcome[name] = "busy"

    def classic():
        with rec_mod.classic_write(ctx.store.vault_root, tmp_path / "live", "promote"):
            pass

    def organizer():
        with jr.liveop_lock(ctx, timeout=0.2):
            pass

    with jr.liveop_lock(ctx):
        preparing(ctx, OP1, T7)                                # store lock inside: fine
        for name, fn in (("classic", classic), ("organizer", organizer)):
            t = threading.Thread(target=second, args=(name, fn))
            t.start()
            t.join(10)
    assert outcome == {"classic": "busy", "organizer": "busy"}
    with jr.liveop_lock(ctx, timeout=0.2):                     # released again
        pass


_HOLDER = """
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


def test_the_live_op_lock_is_held_across_processes(tmp_path):
    """T-L1 (processes): another app instance's live write keeps this one waiting."""
    ctx = make_ctx(tmp_path)
    env = dict(os.environ)
    src = str(Path(safety.__file__).resolve().parents[2])
    env["PYTHONPATH"] = os.pathsep.join(p for p in (src, env.get("PYTHONPATH")) if p)
    (ctx.store.root).mkdir(parents=True, exist_ok=True)
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(ctx.store.root / LIVEOP_LOCK), str(tmp_path)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
    try:
        end = time.monotonic() + 30
        while not (tmp_path / "ready").exists():
            assert holder.poll() is None, holder.communicate()[1]
            assert time.monotonic() < end
            time.sleep(0.005)
        with pytest.raises(LiveOpBusy):
            with jr.liveop_lock(ctx, timeout=0.3):
                pass
        (tmp_path / "release").write_text("1", encoding="ascii")
        holder.communicate(timeout=60)
    finally:
        holder.kill()
    with jr.liveop_lock(ctx, timeout=5):
        pass


def test_open_op_markers_live_per_machine_outside_the_vault(tmp_path, caplog):
    ctx = make_ctx(tmp_path)
    preparing(ctx, OP1, T7)                                    # creates the store (vault id)
    jr.write_marker(ctx, OP1, ["Steam slot 7"])
    path = jr.markers_dir(ctx) / f"{OP1}.json"
    assert path.is_relative_to(tmp_path / "localappdata")
    assert not path.is_relative_to(ctx.store.vault_root)
    (marker,) = jr.list_markers(ctx.host)
    assert (marker.op_id, marker.vault_id, marker.labels) == (OP1, ctx.store.vault_id,
                                                              ("Steam slot 7",))
    assert marker.vault_root == ctx.store.vault_root.resolve()
    (path.parent / f"{OP2}.json").write_text("{not json", "utf-8")
    caplog.set_level("WARNING")
    assert [m.op_id for m in jr.list_markers(ctx.host)] == [OP1]
    assert "skipped" in caplog.text
    jr.remove_marker(ctx, OP1)
    jr.remove_marker(ctx, OP1)                                 # gone already: fine
    assert jr.list_markers(ctx.host) == []


def test_the_machine_id_is_made_once_and_survives(tmp_path):
    host = host_in(tmp_path / "pc1")
    first = jr.machine_id(host)
    assert re.fullmatch(r"m-[0-9a-f]{12}", first)
    assert jr.machine_id(host) == first
    path = tmp_path / "pc1" / "localappdata" / "NMSSaveVault" / jr.MACHINE_ID_FILE
    assert path.read_text("ascii").strip() == first
    assert jr.machine_id(host_in(tmp_path / "pc2")) != first
    path.write_text("garbage", "ascii")
    repaired = jr.machine_id(host)
    assert repaired != "garbage" and jr.machine_id(host) == repaired


def test_appends_and_refreshes_read_only_the_new_tail(tmp_path, monkeypatch):
    """Review item 9: the n-th append parses its new lines, not the whole journal again; a
    refresh folds only the ops that got events; lines another process appended are seen."""
    ctx = make_ctx(tmp_path)
    for op_id, target in ((OP1, T7), (OP2, T8), (OP3, T9)):
        preparing(ctx, op_id, target)
        jr.append(ctx, op_id, "abandoned", {"reason": "not_started"})
    jr.read_ops(ctx)
    parsed, folded = [], []
    real_parse, real_fold = jr._parse_line, jr._record
    monkeypatch.setattr(jr, "_parse_line", lambda raw, n: (parsed.append(n), real_parse(raw, n))[1])
    monkeypatch.setattr(jr, "_record", lambda op_id, evs: (folded.append(op_id),
                                                           real_fold(op_id, evs))[1])
    last = preparing(ctx, OP4, T9)
    assert last["seq"] == 1 and parsed == []            # 6 lines on disk, none parsed again
    ops = jr.read_ops(ctx)
    assert parsed == [7] and folded == [OP4]
    assert [r.status for r in ops.values()] == ["abandoned"] * 3 + ["preparing"]

    other = dict(last, seq=2, event="writing", machine="m-00000000000b")
    with open(jr.ops_path(ctx), "ab") as f:              # another process's append
        f.write(json.dumps(other, separators=(",", ":")).encode("ascii") + b"\n")
    assert jr.append(ctx, OP4, "abandoned", {"reason": "stale"})["seq"] == 3
    assert jr.read_ops(ctx)[OP4].status == "abandoned"


def test_writer_plans_round_trip_through_the_journal(tmp_path, make_wgs_account):
    steam = SteamWritePlan(
        tmp_path, (SteamFileWrite("save14.hg", SHA, 1_792_500_902.0, ".tmp_3fa2_save14.hg.part"),
                   SteamFileWrite("mf_save13.hg", SHA, 1_792_500_903.0,
                                  ".tmp_3fa2_mf_save13.hg.part")),
        {"save14.hg": FileState(SHA, 1800, 1_791_900_000_000_000_000), "mf_save13.hg": None},
        "3fa2")
    text = json.dumps(jr.steam_plan_to_json(steam), ensure_ascii=True)
    assert jr.steam_plan_from_json(json.loads(text), tmp_path, slot=7, op_id=OP1) == steam

    xbox = xbox_plan(tmp_path, make_wgs_account)
    assert xbox.pre_xgs and xbox.pre_xgs["Slot1Manual"] is None     # captured, and journaled
    text = json.dumps(jr.xbox_plan_to_json(xbox), ensure_ascii=True)
    assert jr.xbox_plan_from_json(json.loads(text), xbox.folder, slot=1) == xbox

    for copy_of, source_ts, ordinal in (("A", 1_791_500_000, 15), (None, None, None)):
        mw = MemberWrite("B", SHA, "DDDAD1BDD39F9C72", 3700, 1_792_500_902, source_ts, SHA,
                         "cd" * 32, 1_792_500_902.0, ordinal, copy_of,
                         {"kind": "store", "entry": "preserve-steam-20261016-131503",
                          "slot": 7, "member": "A"})
        text = json.dumps(jr.member_write_to_json(mw), ensure_ascii=True)
        assert jr.member_write_from_json(json.loads(text)) == mw
    with pytest.raises(jr.JournalError):
        jr.member_write_from_json({"position": "C"})


def xbox_plan(tmp_path, make_wgs_account) -> msstore.XboxPairPlan:
    """A planned write of slot 1 (Auto replaced, Manual new) beside slot 2 and AccountData,
    with an xgs mirror."""
    acct = make_wgs_account(tmp_path / "SystemAppData", [
        ("AccountData", '{"acct":1}', "", "", 0, 1_790_000_000.5),
        ("Slot1Auto", '{"a":1}', "Main", "", 60, 1_790_000_000.5),
        ("Slot2Auto", '{"o":1}', "Other", "", 60, 1_790_000_000.5)])
    (acct.parent.parent / "xgs" / acct.name).mkdir(parents=True)
    msstore.mirror_xgs(acct)
    writes = [msstore.XboxRecordWrite(i, chunked(b'{"b":2}'), xbox_meta("Main", ts=int(t)), t)
              for i, t in (("Slot1Manual", 1_792_500_902.0), ("Slot1Auto", 1_792_500_903.0))]
    return msstore.prepare_pair(acct, writes)


def _dir_of(plan, identifier):
    _h, records, _t = msstore.split_index((plan.folder / "containers.index").read_bytes())
    return msstore.record_info(dict(records)[identifier]).dir_guid


def _damaged(plan, change):
    d = jr.xbox_plan_to_json(plan)
    change(d)
    return d


@pytest.mark.parametrize("damage", [
    "path outside", "absolute", "dir up", "account record", "another record's dir",
    "replaced blob in the new dir", "record of another save", "another slot",
])
def test_a_damaged_xbox_payload_is_never_acted_on(tmp_path, make_wgs_account, damage):
    """Review item 8: names must be the op's own - its slot's records, the new blobs of the
    records it wrote, the replaced blobs of the records it replaced."""
    plan = xbox_plan(tmp_path, make_wgs_account)
    other_dir = _dir_of(plan, "Slot2Auto")
    new_dir = next(b.dir_name for b in plan.blobs if b.identifier == "Slot1Manual")  # created

    def swap_records(d):
        d["new_records_b64"]["Slot1Auto"], d["new_records_b64"]["Slot1Manual"] = (
            d["new_records_b64"]["Slot1Manual"], d["new_records_b64"]["Slot1Auto"])

    changes = {
        "path outside": lambda d: d["new_files"].__setitem__(0, "../../accountdata.hg"),
        "absolute": lambda d: d["new_files"].__setitem__(0, "C:/Windows/x"),
        "dir up": lambda d: d.__setitem__("new_dirs", [".."]),
        "account record": lambda d: d["pre_records_b64"].__setitem__("AccountData", None),
        "another record's dir": lambda d: d["new_files"].__setitem__(
            0, other_dir + "/" + d["new_files"][0].split("/")[1]),
        "replaced blob in the new dir": lambda d: d["superseded"].__setitem__(
            0, new_dir + "/" + d["superseded"][0].split("/")[1]),
        "record of another save": swap_records,
        "another slot": None,
    }
    if damage == "another slot":
        with pytest.raises(jr.JournalError):
            jr.xbox_plan_from_json(jr.xbox_plan_to_json(plan), plan.folder, slot=2)
        return
    with pytest.raises(jr.JournalError):
        jr.xbox_plan_from_json(_damaged(plan, changes[damage]), plan.folder, slot=1)


@pytest.mark.parametrize("name", ["accountdata.hg", "..\\save13.hg", "SAVE13.HG",
                                  "steam_autocloud.vdf", "save31.hg", "save1.hg",
                                  "mf_save15.hg"])
def test_a_damaged_steam_payload_names_only_the_ops_slot_files(tmp_path, name):
    """Review item 8: slot 7's four files only (save1.hg is slot 1's, mf_save15.hg slot 8's)."""
    as_file = {"tag": "3fa2", "pre": {}, "files": [
        {"name": name, "temp": f".tmp_3fa2_{name}.part", "sha256": SHA, "mtime": 1.0}]}
    as_pre = {"tag": "3fa2", "files": [], "pre": {name: None}}
    for payload in (as_file, as_pre):
        with pytest.raises(jr.JournalError):
            jr.steam_plan_from_json(payload, tmp_path, slot=7, op_id=OP1)


@pytest.mark.parametrize("tag,temp", [("3fa2", "../save13.hg"),
                                      ("3fa2", ".tmp_9999_save13.hg.part"),
                                      ("0b1c", ".tmp_0b1c_save13.hg.part"),
                                      ("../x", ".tmp_../x_save13.hg.part")])
def test_a_damaged_steam_payload_names_only_its_own_temp_files(tmp_path, tag, temp):
    """Review item 8: the tag must be the op's own (OP1 ends in 3fa2)."""
    payload = {"tag": tag, "pre": {}, "files": [
        {"name": "save13.hg", "temp": temp, "sha256": SHA, "mtime": 1.0}]}
    with pytest.raises(jr.JournalError):
        jr.steam_plan_from_json(payload, tmp_path, slot=7, op_id=OP1)

