"""liveops.recovery: an operation killed at every crash point is classified as D5 2.10 says
after a restart, and finishing it (forward) or putting the slot back (back) ends exactly in
the post- or the pre-image (T-C1..C14, T-R1..R6)."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from nms_save_vault.core import msstore, savedir
from nms_save_vault.core.liveops import FaultInjector, PairSource, SimulatedCrash
from nms_save_vault.core.liveops import opjournal as jr
from nms_save_vault.core.liveops import steamwrite
from nms_save_vault.core.liveops.execute import execute
from nms_save_vault.core.liveops.planner import plan_place
from nms_save_vault.core.liveops.recovery import pending_recovery, resolve_recovery
from nms_save_vault.core.organizer.locator import Location
from nms_save_vault.core.organizer.model import LiveRef, StoreRef
from nms_save_vault.core.store import CaptureScope

from .conftest import chunked, steam_save, xbox_meta
from .liveops_support import MAIN, STEAM_ACCOUNT, T0, XBOX_ACCOUNT, live_source, steam_world, tree_hash

LIVE = LiveRef("steam", STEAM_ACCOUNT)
XBOX = LiveRef("xbox", XBOX_ACCOUNT)


def crash(world, plan, point: str) -> str:
    world.ctx.faults = FaultInjector({point})
    with pytest.raises(SimulatedCrash):
        execute(world.ctx, plan, plan.confirmations)
    world.ctx.faults = None
    world.refresh()                                   # "the next start"
    (op_id,) = jr.read_ops(world.ctx)
    return op_id


def status(world, op_id) -> str:
    return jr.read_ops(world.ctx)[op_id].status


def post_of(world, op_id) -> dict:
    writing = jr.read_ops(world.ctx)[op_id].last("writing")["targets"][0]
    return {p: jr.member_write_from_json(w) for p, w in writing["post"].items()}


def assert_post_steam(world, op_id) -> None:
    for pos, mw in post_of(world, op_id).items():
        data, meta = steamwrite.file_names(1, pos)
        assert _sha(world.live / data) == mw.vid and _sha(world.live / meta) == mw.meta_disk_sha256
    assert savedir.scan_slot(world.live, 1).newest.label == "A"
    assert not [p for p in world.live.iterdir() if p.name.startswith(".tmp_")]


def oplog_lines(world, op_id) -> int:
    return sum(1 for r in world.ctx.vault.read_oplog() if r.get("op_id") == op_id)


@pytest.fixture
def world(tmp_path):
    return steam_world(tmp_path)


@pytest.mark.parametrize("point", ["after_preparing", "in_preserve", "after_snapshot",
                                   "after_writing", "staged:2"])
def test_a_crash_before_the_first_live_write_ends_the_op_by_itself(world, point):
    """C1-C5: nothing live changed; recovery abandons the op and removes its temp files."""
    before = tree_hash(world.live)
    plan = plan_place(world.ctx, world.polestar(), LIVE.target(1))
    op_id = crash(world, plan, point)
    assert pending_recovery(world.ctx) == []
    assert status(world, op_id) == "abandoned"
    assert tree_hash(world.live) == before
    assert not jr.list_markers(world.host)


@pytest.mark.parametrize("point,state", [("swapped:1", "torn"), ("swapped:2", "torn"),
                                         ("swapped:3", "torn"), ("swapped:4", "complete")])
@pytest.mark.parametrize("choice", ["forward", "back"])
def test_a_crash_mid_swap_goes_forward_or_back_exactly(world, point, state, choice):
    """C6-C9: torn ops offer both ways, a fully written one only forward."""
    before = tree_hash(world.live)
    plan = plan_place(world.ctx, world.polestar(), LIVE.target(1))
    op_id = crash(world, plan, point)
    (item,) = pending_recovery(world.ctx)
    assert (item.op_id, item.state, item.recommended) == (op_id, state, "forward")
    assert item.labels == ("Steam slot 1",) and item.text.isascii()
    if choice not in item.choices:
        assert resolve_recovery(world.ctx, op_id, choice).status in ("refused", "rolled_back")
        return
    outcome = resolve_recovery(world.ctx, op_id, choice)
    if choice == "forward":
        assert outcome.status == "done", outcome
        assert_post_steam(world, op_id)
        assert status(world, op_id) == "done" and oplog_lines(world, op_id) == 1
        ops = [r for r in world.ctx.store.journal_records() if r.get("op_id") == op_id]
        assert len(ops) == 1
    else:
        assert outcome.status == "rolled_back", outcome
        assert tree_hash(world.live) == before
        assert status(world, op_id) == "rolled_back"
    assert pending_recovery(world.ctx) == [] and not jr.list_markers(world.host)


@pytest.mark.parametrize("point", ["after_validated", "after_oplog", "after_record"])
def test_a_crash_after_the_check_is_finished_by_itself_once(world, point):
    """C13, C14: records are finished, the oplog line and D1's record written once."""
    plan = plan_place(world.ctx, world.polestar(), LIVE.target(1))
    op_id = crash(world, plan, point)
    assert pending_recovery(world.ctx) == []
    assert status(world, op_id) == "done"
    assert_post_steam(world, op_id)
    assert oplog_lines(world, op_id) == 1
    assert len([r for r in world.ctx.store.journal_records() if r.get("op_id") == op_id]) == 1
    pending_recovery(world.ctx)
    assert oplog_lines(world, op_id) == 1


def test_a_slot_written_by_someone_else_since_is_only_left(world):
    """R3 (I13): neither image any more - nothing is written; leave keeps what is there."""
    plan = plan_place(world.ctx, world.polestar(), LIVE.target(1))
    op_id = crash(world, plan, "swapped:2")
    steam_save(world.live, 1, 1, "Main", 9999, MAIN, T0 + 9000, extra="the game wrote this")
    world.refresh()
    now = tree_hash(world.live)
    (item,) = pending_recovery(world.ctx)
    assert (item.state, item.recommended, item.choices) == ("superseded", "leave", ("leave",))
    assert resolve_recovery(world.ctx, op_id, "back").status == "refused"
    assert resolve_recovery(world.ctx, op_id, "forward").status == "refused"
    assert tree_hash(world.live) == now
    assert resolve_recovery(world.ctx, op_id, "leave").status == "rolled_back"
    assert status(world, op_id) == "abandoned"
    b = savedir.scan_slot(world.live, 1).b
    assert world.ctx.locator.is_durable(_sha(Path(b.data_path)))
    assert tree_hash(world.live) == {k: v for k, v in now.items() if not k.startswith(".tmp_")}


def test_another_machines_op_is_listed_and_never_touched(world):
    """R4."""
    plan = plan_place(world.ctx, world.polestar(), LIVE.target(1))
    op_id = crash(world, plan, "swapped:2")
    now = tree_hash(world.live)
    world.ctx.machine_id = "m-00000000000b"
    (item,) = pending_recovery(world.ctx)
    assert (item.state, item.choices) == ("other_machine", ())
    assert resolve_recovery(world.ctx, op_id, "back").status == "refused"
    assert tree_hash(world.live) == now


def test_nothing_is_recovered_while_an_operation_holds_the_lock(world):
    """An op that is running is not an interrupted one (R5)."""
    jr.append(world.ctx, "op-20261016T101502Z-3fa2", "preparing",
              {"kind": "place", "targets": [jr.target_json(LIVE.target(1))]})
    with jr.liveop_lock(world.ctx):
        import threading
        seen = []
        t = threading.Thread(target=lambda: seen.append(pending_recovery(world.ctx)))
        t.start()
        t.join(10)
    assert seen == [[]]
    assert status(world, "op-20261016T101502Z-3fa2") == "preparing"


def test_a_marker_of_a_vault_that_is_not_there(world, tmp_path):
    """R6: the records are on a drive that is not connected."""
    other = json.dumps({"op_id": "op-20261016T101502Z-77aa", "vault_id": "f" * 32,
                        "vault_root": str(tmp_path / "unplugged"), "labels": ["Steam slot 4"],
                        "utc": "2026-10-16T10:15:03.120Z"})
    folder = Path(world.ctx.host.env["LOCALAPPDATA"]) / "NMSSaveVault" / "cache" / "vaults" / \
        ("f" * 32) / "open-ops"
    folder.mkdir(parents=True)
    (folder / "op-20261016T101502Z-77aa.json").write_text(other, "utf-8")
    (item,) = pending_recovery(world.ctx)
    assert (item.state, item.labels) == ("vault_missing", ("Steam slot 4",))
    assert "Connect it before playing" in item.text


def test_a_failed_rollback_is_offered_again_and_the_game_must_be_closed(world, monkeypatch):
    before = tree_hash(world.live)
    plan = plan_place(world.ctx, world.polestar(), LIVE.target(1))

    def corrupt():
        (world.live / "save.hg").write_bytes(b"broken")

    real = steamwrite.rollback
    monkeypatch.setattr(steamwrite, "rollback",
                        lambda *a: (_ for _ in ()).throw(OSError(5, "held open")))
    world.ctx.faults = FaultInjector(actions={"swapped:4": corrupt})
    outcome = execute(world.ctx, plan, plan.confirmations)
    assert outcome.status == "failed" and outcome.code == "ROLLBACK_FAILED"
    monkeypatch.setattr(steamwrite, "rollback", real)
    world.ctx.faults = None
    world.refresh()
    world.ctx.game_probe = lambda: True
    (item,) = pending_recovery(world.ctx)
    assert item.state == "failed" and "Close No Man's Sky" in item.text
    assert resolve_recovery(world.ctx, outcome.op_id, "back").code == "GAME_RUNNING"
    world.ctx.game_probe = lambda: False
    assert resolve_recovery(world.ctx, outcome.op_id, "back").status == "rolled_back"
    assert tree_hash(world.live) == before


# --- Xbox -------------------------------------------------------------------------------------


def xbox_world(tmp_path, make_wgs_account):
    world = steam_world(tmp_path)
    acct = make_wgs_account(tmp_path / "SystemAppData", [
        ("AccountData", '{"acct":1}', "", "", 0, T0 + 0.5),
        ("Slot1Auto", '{"x1a":1}', "Xmain", "", 600, T0 + 10.5),
        ("Slot1Manual", '{"x1m":1}', "Xmain", "", 500, T0 + 5.5)])
    (acct.parent.parent / "xgs" / acct.name).mkdir(parents=True)
    msstore.mirror_xgs(acct)
    backup = world.ctx.versions.add_from_live(acct, platform="xbox", account_key=acct.name,
                                              kind="full", label="x", scope=CaptureScope())
    msstore.write_save(acct, "Slot1Auto", chunked(b'{"x1a":"played"}'),
                       xbox_meta("Xmain", play=900, pid=0xA1B2C3D4_00000001, ts=T0 + 50),
                       T0 + 50.5)
    msstore.mirror_xgs(acct)
    os.utime(acct / "containers.index", (T0, T0))
    world.sources.append(live_source(acct, platform_name="xbox"))
    world.refresh()
    entry = world.ctx.versions.entry(backup.id)
    a = entry.member(1, "A")
    source = PairSource(a.vid, entry.member(1, "B").vid, None,
                        Location(a.vid, "store", StoreRef(backup.id), 1, "A", "xbox", True))
    return world, acct, source


@pytest.mark.parametrize("point,state", [("before_index", None), ("after_index", "torn"),
                                         ("xgs:Slot1Manual", "torn")])
@pytest.mark.parametrize("choice", ["forward", "back"])
def test_an_xbox_crash_at_each_point(tmp_path, make_wgs_account, point, state, choice):
    """C10-C12: before the index nothing is referenced (ended by itself); after it the
    xgs mirror is finished forward, or the index, wgs and xgs go back exactly."""
    world, acct, source = xbox_world(tmp_path, make_wgs_account)
    root = tmp_path / "SystemAppData"
    before = _contents(root)
    before_index = (acct / "containers.index").read_bytes()
    plan = plan_place(world.ctx, source, XBOX.target(1))
    op_id = crash(world, plan, point)
    items = pending_recovery(world.ctx)
    if state is None:
        assert items == [] and status(world, op_id) == "abandoned"
        assert _contents(root) == before
        return
    (item,) = items
    assert item.state == state
    outcome = resolve_recovery(world.ctx, op_id, choice)
    if choice == "forward":
        assert outcome.status == "done", outcome
        view = msstore.scan(acct).slots[1]
        assert view.newest.label == "A" and _sha(Path(view.a.data_path)) == source.vid_a
        twin = acct.parent.parent / "xgs" / acct.name
        assert (twin / "Slot1Auto" / "data").read_bytes() == Path(view.a.data_path).read_bytes()
    else:
        assert outcome.status == "rolled_back", outcome
        assert _contents(root) == before
        assert (acct / "containers.index").read_bytes() == before_index


def _sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _contents(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): _sha(p) for p in sorted(root.rglob("*")) if p.is_file()}
