"""Live-op safety fixes from the whole-program review: the xgs-only AccountData is never
overwritten by a restore, an unknown game state is unknown, a Steam rollback leaves a file
it did not write, and undoing the vault upgrade waits for live ops."""
from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from nms_save_vault.core import msstore, safety
from nms_save_vault.core import operations as ops
from nms_save_vault.core.liveops import RollbackError
from nms_save_vault.core.liveops import steamwrite as sw
from nms_save_vault.core.store import RollbackError as RollbackErr
from nms_save_vault.core.store import rollback

from . import test_xgs
from .test_liveops_steamwrite import TAG, live_folder, source_writes
from .test_store_rollback import _tree, isolated, upgraded  # noqa: F401  (fixtures)
from .test_xgs import _vault

REAL_PROBE = safety.is_game_running          # the fixture replaces it for each test


def test_a_restore_keeps_an_account_data_that_only_xgs_holds(make_wgs_account, tmp_path,
                                                             monkeypatch):
    from nms_save_vault.core.liveops import planner

    monkeypatch.setattr(planner, "XBOX_SYNC_SECONDS", 0)
    live = make_wgs_account(tmp_path / "live", [("Slot1Auto", '{"x":"live"}', "Live", "", 10, 900)])
    backup = make_wgs_account(tmp_path / "bk", [
        ("Slot1Auto", '{"x":"old"}', "Old", "", 5, 800),
        ("AccountData", '{"acct":"backup"}', "", "", 0, 800)])
    twin = test_xgs._add_xgs_twin(live)
    (twin / "AccountData").mkdir()
    precious = b"ACCOUNT DATA ONLY XGS" * 8
    (twin / "AccountData" / "data").write_bytes(precious)
    (twin / "AccountData" / "meta").write_bytes(b"xgs meta")
    vault = _vault(tmp_path)
    entry = ops.import_backup(vault, backup, label="bk")

    result = ops.restore_full(vault, entry, live, allow_game_running=True)

    assert (twin / "AccountData" / "data").read_bytes() == precious
    assert (twin / "AccountData" / "meta").read_bytes() == b"xgs meta"
    assert any("account data was kept" in w for w in result.warnings)


def _tasklist(monkeypatch, *, code, out):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=code, stdout=out, stderr=b""))


def test_a_failing_tasklist_is_unknown_not_not_running(monkeypatch):
    _tasklist(monkeypatch, code=1, out=b"")
    assert REAL_PROBE() is None
    _tasklist(monkeypatch, code=0, out=b"INFO: No tasks are running\r\n")
    assert REAL_PROBE() is False
    _tasklist(monkeypatch, code=0, out=b"NMS.exe   1234 Console\r\n\xff\xfe")
    assert REAL_PROBE() is True


def test_a_steam_rollback_keeps_the_bytes_of_a_file_it_did_not_write(tmp_path):
    folder = live_folder(tmp_path)
    writes = source_writes(tmp_path)
    plan = sw.prepare(folder, writes, 7, TAG)
    saved = {n: (folder / n).read_bytes() for n, pre in plan.pre.items() if pre is not None}
    sw.stage(plan, sw.payload_of(writes, 7))
    sw.swap(plan)
    (folder / "save13.hg").write_bytes(b"the game saved after the swap")
    before = {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()}

    def cannot_keep(name, data):
        raise PermissionError(13, "disk full", name)

    with pytest.raises(RollbackError):
        sw.rollback(plan, saved, keep=cannot_keep)
    assert {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()} == before

    kept = {}
    sw.rollback(plan, saved, keep=lambda name, data: kept.setdefault(name, data))
    assert kept == {"save13.hg": b"the game saved after the swap"}
    assert (folder / "save13.hg").read_bytes() == saved["save13.hg"]


def _open_op(vault_root, *, event="writing"):
    organizer = vault_root / "organizer"
    organizer.mkdir(exist_ok=True)
    line = {"j": 1, "op_id": "op-20261018T101500Z-ab12", "seq": 1, "event": event,
            "utc": "2026-10-18T10:15:00.000Z", "machine": "m-0123456789ab"}
    (organizer / "ops.jsonl").write_text(json.dumps(line) + "\n", "ascii")


def test_undoing_the_upgrade_waits_for_an_interrupted_live_op(upgraded):
    vault, _live, store, _copy, _old = upgraded
    plan = rollback.plan_rollback(vault.root, store)
    assert plan.problems == ()
    _open_op(vault.root)
    with pytest.raises(RollbackErr) as info:
        rollback.run_rollback(plan, store)
    assert info.value.code == "recovery-pending"
    before = _tree(vault.root)
    with pytest.raises(RollbackErr):
        rollback.run_rollback(plan, store)
    assert _tree(vault.root) == before
    _open_op(vault.root, event="done")
    rollback.run_rollback(plan, store)


def test_undoing_the_upgrade_refuses_while_a_live_op_holds_the_lock(upgraded):
    from nms_save_vault.core.organizer.store import liveop_lock_path

    vault, _live, store, _copy, _old = upgraded
    plan = rollback.plan_rollback(vault.root, store)
    (vault.root / "organizer").mkdir(exist_ok=True)
    with safety.exclusive_lock(liveop_lock_path(vault.root), timeout=1):
        import threading
        err = []

        def run():
            try:
                rollback.run_rollback(plan, store)
            except RollbackErr as exc:
                err.append(exc.code)
        t = threading.Thread(target=run)
        t.start()
        t.join()
    assert err == ["live-op-busy"]
