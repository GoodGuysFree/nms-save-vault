"""Regression tests of the third review of live operations (D5 2.13, 2.14): the whole-folder
Undo picks this folder's own snapshots, steps back and never forward, a damaged Xbox index has
a way out, an uploaded Xbox write can be put back, a disabled folder is never written."""
from __future__ import annotations

import shutil
import time

import pytest

from nms_save_vault.core import msstore, savedir
from nms_save_vault.core import operations as ops
from nms_save_vault.core import state as appstate
from nms_save_vault.core import store as vstore
from nms_save_vault.core.catalog import Vault
from nms_save_vault.core.state import AppState, Source

from .conftest import chunked, steam_save, xbox_meta
from .liveops_support import STEAM_ACCOUNT, steam_account


def _vault(tmp_path) -> Vault:
    v = Vault(tmp_path / "vault")
    v.ensure()
    vstore.create_store(v.root)
    return v


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    from nms_save_vault.core.liveops import planner

    monkeypatch.setattr(planner, "XBOX_SYNC_SECONDS", 0)


# --- a damaged Xbox containers.index still has a way out ---------------------------------------


def test_a_damaged_xbox_index_is_repaired_by_a_restore_and_by_undo(make_wgs_account, tmp_path):
    live = make_wgs_account(tmp_path / "live", [
        ("Slot1Auto", '{"a":1}', "Keep", "", 1, 100),
        ("Slot1Manual", '{"b":1}', "Keep", "", 1, 90)])
    vault = _vault(tmp_path)
    ops.promote_member(vault, live, 1, 1, allow_game_running=True)     # takes a safety snapshot
    bk = tmp_path / "bk" / "wgs" / live.name
    shutil.copytree(live, bk)
    entry = ops.import_backup(vault, bk, label="bk")
    index = live / "containers.index"
    index.write_bytes(index.read_bytes()[:-30])                       # a write cut off mid-record

    assert ops.plan_undo(vault, live).snapshot_id                     # the plan agrees with the undo
    result = ops.restore_full(vault, entry, live, allow_game_running=True)

    assert any("save index was damaged" in w for w in result.warnings)
    assert savedir.scan_any(live).slots[1].a.save_name == "Keep"
    assert list((vault.root / "damaged-index").iterdir())              # the damaged bytes are kept
    index.write_bytes(b"\x00garbage")                                  # beyond salvage
    assert ops.undo_last(vault, live, allow_game_running=True).ok
    assert savedir.scan_any(live).slots[1].a.save_name == "Keep"


def test_a_damaged_index_of_another_account_is_refused(make_wgs_account, tmp_path):
    x = make_wgs_account(tmp_path / "x", [("Slot1Auto", '{"a":1}', "One", "", 1, 100)],
                         name="0000000000000001_XXXX", account_id="account-x")
    y = make_wgs_account(tmp_path / "y", [("Slot1Auto", '{"a":1}', "One", "", 1, 100)],
                         name="0000000000000002_YYYY", account_id="account-y")
    vault = _vault(tmp_path)
    backup = ops.create_full_backup(vault, x, include_cache=False)
    index = y / "containers.index"
    index.write_bytes(index.read_bytes()[:-30])
    before = index.read_bytes()

    with pytest.raises(ops.OperationError):
        ops.restore_full(vault, backup, y, allow_game_running=True)

    assert index.read_bytes() == before


# --- two live folders of one name --------------------------------------------------------------


def test_whole_folder_undo_never_takes_another_folders_snapshot(tmp_path):
    real = steam_account(tmp_path / "NMS")                       # the real live folder
    copy = steam_account(tmp_path / "TestCopy")                  # a test copy, same name
    steam_save(copy, 6, 0, "CopyOnly", 50, 0x6666, 1_790_000_500, extra="copy-only")
    assert real.name == copy.name == STEAM_ACCOUNT
    vault = _vault(tmp_path)
    ops.promote_member(vault, real, 1, 1, allow_game_running=True)
    time.sleep(1.2)
    ops.promote_member(vault, copy, 1, 1, allow_game_running=True)   # the newest snapshot overall

    plan = ops.plan_undo(vault, real, other_live_dirs=[copy])
    ops.undo_last(vault, real, allow_game_running=True, expected=plan.token)

    assert not savedir.scan_any(real).slots[6].occupied, "the copy's save reached the real folder"
    assert savedir.scan_any(real).slots[1].newest.label == "A"


# --- an abandoned move is not an Undo target ---------------------------------------------------


def test_whole_folder_undo_skips_the_snapshot_of_an_abandoned_move(tmp_path):
    from nms_save_vault.core.liveops import FaultInjector, execute
    from nms_save_vault.core.liveops.panic import plan_panic_restore
    from nms_save_vault.core.liveops.planner import plan_place
    from nms_save_vault.core.organizer.model import LiveRef

    from .liveops_support import steam_world

    world = steam_world(tmp_path)
    live = LiveRef("steam", STEAM_ACCOUNT)
    before1 = (world.live / "save.hg").read_bytes()
    plan = plan_place(world.ctx, world.polestar(), live.target(1))
    assert execute(world.ctx, plan, plan.confirmations).status == "done"
    world.refresh()
    plan2 = plan_place(world.ctx, world.polestar(), live.target(2))

    def game_writes():                       # the game writes slot 2 after the snapshot
        steam_save(world.live, 2, 0, "Second", 700, 0x0123456789ABCDEF, 1_800_000_000,
                   extra="second-played")
    world.ctx.faults = FaultInjector(actions={"after_snapshot": game_writes})
    out2 = execute(world.ctx, plan2, plan2.confirmations)
    world.ctx.faults = None
    assert out2.status == "stale"
    world.refresh()

    p = plan_panic_restore(world.ctx, live)
    assert execute(world.ctx, p, p.confirmations).status == "done"

    assert (world.live / "save.hg").read_bytes() == before1   # the last completed move is undone


# --- repeated Undo steps back, never forward ---------------------------------------------------


def test_two_undos_step_back_two_changes(tmp_path):
    live = steam_account(tmp_path / "NMS")
    vault = _vault(tmp_path)
    first = savedir.scan_any(live).slots[1].newest.label
    ops.promote_member(vault, live, 1, 1, allow_game_running=True)          # change 1
    time.sleep(1.1)
    steam_save(live, 4, 0, "Fourth", 10, 0x4444, 1_790_000_900, extra="fourth")
    ops.repopulate_slot(vault, live, 4, live, 5, allow_game_running=True)   # change 2
    time.sleep(1.1)
    p1 = ops.plan_undo(vault, live)
    ops.undo_last(vault, live, allow_game_running=True, expected=p1.token)
    time.sleep(1.1)
    p2 = ops.plan_undo(vault, live)

    assert p2.snapshot_id != p1.snapshot_id
    ops.undo_last(vault, live, allow_game_running=True, expected=p2.token)

    assert savedir.scan_any(live).slots[1].newest.label == first


# --- Xbox: a put-back after an upload, and the account data -----------------------------------


def test_a_rollback_after_the_xbox_app_uploaded_flags_the_records_for_upload(
        make_wgs_account, tmp_path):
    live = make_wgs_account(tmp_path, [("Slot1Auto", '{"a":1}', "Run", "", 1, 100)])
    writes = [msstore.XboxRecordWrite("Slot1Auto", chunked(b'{"a":2}'), xbox_meta("Run", ts=200),
                                      1_790_000_000.0)]
    plan = msstore.prepare_pair(live, writes)
    msstore.apply_pair(plan, {w.identifier: (w.data, w.meta) for w in writes})
    for ident in ("Slot1Auto",):                                # the app uploads it
        info, cs = msstore.parse_containers_index(live)
        next(c for c in cs if c.identifier == ident).sync_state = msstore.BLOB_SYNC_SYNCED
        (live / "containers.index").write_bytes(msstore.build_containers_index(info, cs))

    msstore.rollback_pair(plan)

    info, cs = msstore.parse_containers_index(live)
    rec = next(c for c in cs if c.identifier == "Slot1Auto")
    assert rec.sync_state == msstore.BLOB_SYNC_MODIFIED         # the app uploads the put-back one
    assert rec.data_file.read_bytes() == chunked(b'{"a":1}')


def test_an_xbox_restore_gives_a_folder_without_account_data_the_backups(
        make_wgs_account, tmp_path):
    saves = [("AccountData", '{"acct":1}', "", "", 0, 500),
             ("Slot1Auto", '{"a":1}', "One", "", 100, 1000)]
    live = make_wgs_account(tmp_path / "live", saves)
    vault = _vault(tmp_path)
    backup = ops.create_full_backup(vault, live, include_cache=False)
    msstore.delete_save(live, "AccountData", 3000.0)

    ops.restore_full(vault, backup, live, allow_game_running=True)

    _info, cs = msstore.parse_containers_index(live)
    acct = next(c for c in cs if c.identifier == "AccountData")
    assert acct.data_file.read_bytes() == chunked(b'{"acct":1}')


# --- a disabled folder is never written --------------------------------------------------------


def test_a_folder_the_owner_disabled_is_not_written_by_an_explicit_path(tmp_path, monkeypatch):
    live = steam_account(tmp_path / "NMS")
    vault = _vault(tmp_path)
    state = AppState(sources=[Source(id="s", platform="steam", account=live.name, path=str(live),
                                     enabled=False)], vault=str(vault.root))
    monkeypatch.setattr(appstate, "load", lambda path=None: state)
    before = {p.name: p.read_bytes() for p in live.iterdir() if p.is_file()}

    with pytest.raises(ops.OperationError, match="switched off"):
        ops.promote_member(vault, live, 1, 1, allow_game_running=True)

    assert {p.name: p.read_bytes() for p in live.iterdir() if p.is_file()} == before


def test_the_index_repair_runs_under_the_live_op_lock_and_is_journaled(make_wgs_account, tmp_path,
                                                                      monkeypatch):
    from nms_save_vault.core.liveops import classic, opjournal as jr
    from nms_save_vault.core.liveops.classic import open_context

    live = make_wgs_account(tmp_path / "live", [("Slot1Auto", '{"a":1}', "Keep", "", 1, 100)])
    vault = _vault(tmp_path)
    backup = ops.create_full_backup(vault, live, include_cache=False)
    index = live / "containers.index"
    index.write_bytes(index.read_bytes()[:-30])
    damaged = index.read_bytes()
    monkeypatch.setattr(classic, "LIVEOP_TIMEOUT", 0.2)
    ctx, _ref = open_context(vault, live)

    import threading

    held, release, outcome = threading.Event(), threading.Event(), {}

    def other_operation():                          # another operation holds the lock
        with jr.liveop_lock(ctx):
            held.set()
            release.wait(10)

    t = threading.Thread(target=other_operation)
    t.start()
    held.wait(10)
    try:
        with pytest.raises(ops.OperationError, match="still running"):
            ops.restore_full(vault, backup, live, allow_game_running=True)
        outcome["index"] = index.read_bytes()
    finally:
        release.set()
        t.join(10)
    assert outcome["index"] == damaged               # nothing was written meanwhile

    ops.restore_full(vault, backup, live, allow_game_running=True)
    kinds = [r.kind for r in jr.read_ops(ctx).values()]
    assert "index_repair" in kinds
    repair = next(r for r in jr.read_ops(ctx).values() if r.kind == "index_repair")
    assert repair.status == "done"
