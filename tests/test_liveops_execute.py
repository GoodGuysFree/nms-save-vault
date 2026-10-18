"""liveops.execute over real D1 and D3 pieces in a sandbox: place, place into empty,
restore-older and promote; preserve and snapshot before the first live write; the game
checks; stale plans; validation and the exact rollback; the oplog v2 line and D1's record
(D5 2.8, 2.9.1, 2.9.2; T-F1, T-F2, T-G1..G3, T-P1..P3, T-V1..V3, T-O1)."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from nms_save_vault.core import msstore, savedir
from nms_save_vault.core.liveops import ConfirmationRequired, FaultInjector, PairSource
from nms_save_vault.core.liveops import SimulatedCrash
from nms_save_vault.core.liveops import opjournal as jr
from nms_save_vault.core.liveops.execute import execute
from nms_save_vault.core.liveops.planner import plan_place, plan_promote
from nms_save_vault.core.organizer.locator import Location
from nms_save_vault.core.organizer.model import LiveRef, StoreRef
from nms_save_vault.core.store import CaptureScope

from .conftest import steam_save
from .liveops_support import (
    MAIN,
    STEAM_ACCOUNT,
    STEAM_ACCOUNT_2,
    T0,
    XBOX_ACCOUNT,
    live_source,
    steam_world,
    tree_hash,
)

LIVE = LiveRef("steam", STEAM_ACCOUNT)
OTHER = LiveRef("steam", STEAM_ACCOUNT_2)
SLOT1 = ("save.hg", "mf_save.hg", "save2.hg", "mf_save2.hg")


def run(world, plan, *, note: str = ""):
    return execute(world.ctx, plan, plan.confirmations, note=note)


def events(world, op_id: str) -> list[str]:
    return [e["event"] for e in jr.read_ops(world.ctx)[op_id].events]


def minus(tree: dict, names) -> dict:
    return {k: v for k, v in tree.items() if k not in names}


def oplog(world) -> list[dict]:
    return list(world.ctx.vault.read_oplog())


@pytest.mark.parametrize("cloud", [True, False])
def test_place_replaces_both_members_after_keeping_them(tmp_path, cloud):
    """T-F1, T-V3, T-O1: the slot holds the version (A loads), what it held is in a
    preserve entry, the folder in a snapshot entry, nothing else in the folder changed, and
    the journal, the oplog and D1 all know the op."""
    world = steam_world(tmp_path, cloud=cloud)
    before = tree_hash(world.live)
    old = {n: (world.live / n).read_bytes() for n in SLOT1}
    plan = plan_place(world.ctx, world.polestar(), LIVE.target(1))
    outcome = run(world, plan, note="trying the older base")
    assert outcome.status == "done", outcome
    assert outcome.outcome == "Moved 'Polestar' into Steam slot 1 (Desktop). 'Main' is in the vault."
    assert outcome.undo is not None and outcome.verify == "pending"

    entry = world.ctx.versions.entry(world.backup_id)
    slot = savedir.scan_slot(world.live, 1)
    assert slot.newest.label == "A" and slot.a.save_name == "Polestar"
    assert _sha(world.live / "save.hg") == entry.member(3, "A").vid
    assert _sha(world.live / "save2.hg") == entry.member(3, "B").vid
    assert slot.a.data_mtime > slot.b.data_mtime
    assert minus(tree_hash(world.live), SLOT1) == minus(before, SLOT1)
    assert not [p for p in world.live.iterdir() if p.name.startswith(".tmp_")]

    assert events(world, outcome.op_id) == ["preparing", "writing", "validated", "done"]
    assert jr.read_ops(world.ctx)[outcome.op_id].events[0]["note"] == "trying the older base"
    line = oplog(world)[-1]
    assert list(line)[:6] == ["ts", "op", "entry_id", "snapshot_id", "detail", "changed"]
    assert (line["v"], line["op_id"], line["status"], line["note"]) == (
        2, outcome.op_id, "done", "trying the older base")
    store = world.ctx.versions
    preserve, snapshot = store.entry(line["entry_id"]), store.entry(line["snapshot_id"])
    assert (preserve.kind, snapshot.kind) == ("preserve", "snapshot")
    assert store.read_file(preserve.id, "save.hg") == old["save.hg"]
    assert store.read_file(snapshot.id, "mf_save2.hg") == old["mf_save2.hg"]
    assert STEAM_ACCOUNT not in json.dumps(line["detail"]) and str(tmp_path) not in line["detail"]
    ops = [r for r in world.ctx.store.journal_records() if r["t"] == "op"]
    assert [(r["op_id"], r["kind"], r["note"]) for r in ops] == [
        (outcome.op_id, "place", "trying the older base")]
    assert ops[0]["evicted"] == [_sha_bytes(old["save.hg"]), _sha_bytes(old["save2.hg"])]
    assert not jr.list_markers(world.host)


def test_place_into_an_empty_slot_and_from_a_one_member_source(tmp_path):
    """T-F2: a one-member source fills both positions with one playthrough (P6)."""
    world = steam_world(tmp_path)
    second = savedir.scan_slot(world.live, 2).a
    source = PairSource(_sha(Path(second.data_path)), None, "A",
                        Location(_sha(Path(second.data_path)), "live", LIVE, 2, "A", "steam",
                                 False))
    plan = plan_place(world.ctx, source, OTHER.target(1))     # Main of the laptop, evicted
    assert plan.kind == "place" and "OTHER_ACCOUNT" in plan.confirmations
    assert run(world, plan).status == "done"
    slot = savedir.scan_slot(world.other, 1)
    assert slot.a.save_name == slot.b.save_name == "Second"
    assert _sha(Path(slot.a.data_path)) == _sha(Path(slot.b.data_path))
    empty = plan_place(world.ctx, world.polestar(), LIVE.target(3))
    assert empty.kind == "place_into_empty"
    outcome = run(world, empty)
    assert outcome.status == "done" and outcome.undo is None        # undo would empty it
    assert savedir.scan_slot(world.live, 3).newest.save_name == "Polestar"


def test_restore_older_and_promote(tmp_path):
    world = steam_world(tmp_path)
    steam_save(world.other, 3, 0, "Polestar", 5000, 0x7E57AB1E00000001, T0 + 900,
               extra="pole-played")
    world.refresh()
    played = (world.other / "save5.hg").read_bytes()
    plan = plan_place(world.ctx, world.polestar(), OTHER.target(3))
    assert plan.kind == "restore_older"
    outcome = run(world, plan)
    assert outcome.status == "done"
    assert world.ctx.versions.read_file(oplog(world)[-1]["entry_id"], "save5.hg") == played

    data = {n: (world.live / n).read_bytes() for n in ("save.hg", "save2.hg")}
    promote = plan_promote(world.ctx, LIVE.target(1), "B")
    assert run(world, promote).status == "done"
    slot = savedir.scan_slot(world.live, 1)
    assert slot.newest.label == "B"
    assert {n: (world.live / n).read_bytes() for n in data} == data     # data verbatim


def test_a_running_game_stops_the_operation_before_any_write(tmp_path):
    """T-G1 and T-G2."""
    world = steam_world(tmp_path)
    plan = plan_place(world.ctx, world.polestar(), LIVE.target(1))
    before = tree_hash(world.live)
    world.ctx.game_probe = lambda: True
    assert execute(world.ctx, plan, plan.confirmations).code == "GAME_RUNNING"
    assert not jr.ops_path(world.ctx).exists() and tree_hash(world.live) == before
    answers = iter([False, True])
    world.ctx.game_probe = lambda: next(answers)
    outcome = execute(world.ctx, plan, plan.confirmations)
    assert (outcome.status, outcome.code) == ("refused", "GAME_RUNNING")
    assert events(world, outcome.op_id) == ["preparing", "writing", "abandoned"]
    assert tree_hash(world.live) == before
    assert not jr.list_markers(world.host)


def test_an_unknown_game_state_must_be_confirmed(tmp_path):
    """T-G3."""
    world = steam_world(tmp_path)
    world.ctx.game_probe = lambda: None
    plan = plan_place(world.ctx, world.polestar(), LIVE.target(3))
    assert "GAME_STATE_UNKNOWN" in plan.confirmations
    with pytest.raises(ConfirmationRequired):
        execute(world.ctx, plan, plan.confirmations - {"GAME_STATE_UNKNOWN"})
    assert execute(world.ctx, plan, plan.confirmations).status == "done"


def test_the_vault_and_the_preserved_copy_exist_before_the_first_live_write(tmp_path):
    """T-P1: a crash right after the write-ahead record finds the slot untouched, its
    members in a preserve entry and the folder in a snapshot, every object re-reading."""
    world = steam_world(tmp_path)
    before = tree_hash(world.live)
    plan = plan_place(world.ctx, world.polestar(), LIVE.target(1))
    world.ctx.faults = FaultInjector({"after_writing"})
    with pytest.raises(SimulatedCrash):
        execute(world.ctx, plan, plan.confirmations)
    assert tree_hash(world.live) == before
    (op,) = jr.read_ops(world.ctx).values()
    writing = op.last("writing")["targets"][0]
    store = world.ctx.versions
    for kept in writing["preserved"]:
        loc = jr.location_from_json(kept["location"])
        assert store.entry(loc.ref.key).kind == "preserve"
        assert world.ctx.locator.open(loc.vid, loc).location == loc
    snap = store.entry(writing["snapshot_id"])
    assert snap.kind == "snapshot"
    assert all(store.file_bytes(f) is not None for f in snap.files)


def test_a_vault_upgrade_undone_or_a_slot_changed_since_the_plan_refuses(tmp_path):
    """T-P2, T-P3."""
    world = steam_world(tmp_path)
    plan = plan_place(world.ctx, world.polestar(), LIVE.target(1))
    before = tree_hash(world.live)
    steam_save(world.live, 1, 0, "Main", 7300, MAIN, T0 + 500, extra="played on")
    played = tree_hash(world.live)
    outcome = execute(world.ctx, plan, plan.confirmations)
    assert outcome.status == "stale" and outcome.replan is not None
    assert outcome.replan.fingerprint != plan.fingerprint
    assert tree_hash(world.live) == played and not jr.ops_path(world.ctx).exists()
    (world.vault_root / "store" / "FORMAT").rename(world.vault_root / "FORMAT.aside")
    outcome = execute(world.ctx, outcome.replan, outcome.replan.confirmations)
    assert outcome.code == "VAULT_UPGRADE_NEEDED" and tree_hash(world.live) == played
    assert before != played


def test_a_slot_written_between_preserve_and_swap_is_never_overwritten(tmp_path):
    """The game saves after the writer planned: the swap finds the slot changed, writes
    nothing, and the op ends stale (no rollback over the game's save)."""
    world = steam_world(tmp_path)
    plan = plan_place(world.ctx, world.polestar(), LIVE.target(1))

    def game_saves():
        steam_save(world.live, 1, 1, "Main", 7150, MAIN, T0 + 700, extra="autosave")

    world.ctx.faults = FaultInjector(actions={"after_writing": game_saves})
    played_b = None
    outcome = execute(world.ctx, plan, plan.confirmations)
    played_b = (world.live / "save2.hg").read_bytes()
    assert outcome.status == "stale"
    assert b"autosave" in _payload(played_b)
    assert events(world, outcome.op_id)[-1] == "abandoned"


@pytest.mark.parametrize("victim", ["save.hg", "mf_save2.hg"])
def test_a_write_that_does_not_check_out_is_rolled_back_exactly(tmp_path, victim):
    """T-V1, T-V2: a file corrupted after the swap fails validation; every touched name
    gets its pre-op bytes and mtime back from the store snapshot."""
    world = steam_world(tmp_path)
    before = tree_hash(world.live)
    plan = plan_place(world.ctx, world.polestar(), LIVE.target(1))

    def corrupt():
        path = world.live / victim
        raw = path.read_bytes()      # flip the byte, never set it to 0: a meta file's last
        path.write_bytes(raw[:-1] + bytes([raw[-1] ^ 0xFF]))   # byte is random, 1 in 256 is 0x00

    world.ctx.faults = FaultInjector(actions={"swapped:4": corrupt})
    outcome = execute(world.ctx, plan, plan.confirmations)
    assert (outcome.status, outcome.code) == ("rolled_back", "ROLLED_BACK")
    assert outcome.outcome.startswith("Nothing changed in Steam slot 1 (Desktop).")
    assert tree_hash(world.live) == before
    assert events(world, outcome.op_id) == ["preparing", "writing", "rolled_back"]
    line = oplog(world)[-1]
    assert line["status"] == "rolled_back" and line["snapshot_id"]
    assert world.ctx.versions.entry(line["snapshot_id"]).kind == "snapshot"


def test_an_xbox_place_and_its_rollback(tmp_path, make_wgs_account):
    world = steam_world(tmp_path)
    acct = make_wgs_account(tmp_path / "SystemAppData", [
        ("AccountData", '{"acct":1}', "", "", 0, T0 + 0.5),
        ("Slot1Auto", '{"x1a":1}', "Xmain", "", 600, T0 + 10.5),
        ("Slot1Manual", '{"x1m":1}', "Xmain", "", 500, T0 + 5.5)])
    (acct.parent.parent / "xgs" / acct.name).mkdir(parents=True)
    msstore.mirror_xgs(acct)
    store = world.ctx.versions
    backup = store.add_from_live(acct, platform="xbox", account_key=acct.name, kind="full",
                                 label="xbox backup", scope=CaptureScope())
    msstore.write_save(acct, "Slot1Auto", _chunk('{"x1a":"played"}'),     # the game played on
                       _xmeta("Xmain", 900, T0 + 50), T0 + 50.5)
    msstore.mirror_xgs(acct)
    os.utime(acct / "containers.index", (T0, T0))
    world.sources.append(live_source(acct, platform_name="xbox"))
    world.refresh()
    xbox = LiveRef("xbox", XBOX_ACCOUNT)
    a = store.entry(backup.id).member(1, "A")
    source = PairSource(a.vid, store.entry(backup.id).member(1, "B").vid, None,
                        Location(a.vid, "store", StoreRef(backup.id), 1, "A", "xbox", True))
    root = tmp_path / "SystemAppData"
    before = _contents(root)

    def corrupt():                       # the new blob the index now names goes bad
        data = Path(msstore.scan(acct).slots[1].a.data_path)
        data.write_bytes(b"not the save")

    plan = plan_place(world.ctx, source, xbox.target(1))
    assert plan.kind == "restore_older" and not plan.blocked
    world.ctx.faults = FaultInjector(actions={"after_index": corrupt})
    outcome = execute(world.ctx, plan, plan.confirmations)
    assert outcome.status == "rolled_back", outcome
    assert _contents(root) == before
    world.ctx.faults = None
    os.utime(acct / "containers.index", (T0, T0))          # the rollback wrote it just now
    world.refresh()
    plan = plan_place(world.ctx, source, xbox.target(1))
    outcome = execute(world.ctx, plan, plan.confirmations)
    assert outcome.status == "done", outcome
    view = msstore.scan(acct).slots[1]
    assert view.newest.label == "A" and _sha(Path(view.a.data_path)) == a.vid
    assert view.a.xbox.sync_state == msstore.BLOB_SYNC_MODIFIED


def _sha(path: Path) -> str:
    return _sha_bytes(Path(path).read_bytes())


def _sha_bytes(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()


def _payload(data: bytes) -> bytes:
    from nms_save_vault.core import lz4_block
    chunks, _ = lz4_block.walk_chunks(data)
    return lz4_block.decode_chunk(data, chunks[0])


def _chunk(text: str) -> bytes:
    from .conftest import chunked
    return chunked(text.encode())


def _xmeta(name: str, play: int, ts: float) -> bytes:
    from .conftest import xbox_meta
    return xbox_meta(name, play=play, pid=0xA1B2C3D4_00000001, ts=int(ts))


def _contents(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): _sha(p) for p in sorted(root.rglob("*")) if p.is_file()}
