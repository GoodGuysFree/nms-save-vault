"""The whole-folder undo is the panic restore (D5 4.3): the newest snapshot of this folder
only (P4), slot by slot, never emptying a slot; the slot-scoped undo of a move is the engine's
(test_liveops_undo.py). Also: an unrecorded backup, extract or import reaches the user.

Built on synthetic Steam folders (``steam_save``) and wgs accounts (``make_wgs_account``) in a
temp sandbox; no real save folder is touched.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nms_save_vault import cli
from nms_save_vault.core import savedir
from nms_save_vault.core import operations as ops
from nms_save_vault.core import store as vstore
from nms_save_vault.core.catalog import Vault

from .conftest import steam_save


def _steam_live(parent: Path, name: str) -> Path:
    """A Steam folder with slots 1-3 filled; each folder's saves are its own (named after it)."""
    live = parent / name
    for slot in (1, 2, 3):
        steam_save(live, slot, 0, f"{name} run {slot}", 3600 * slot, 0x1000 + slot, 1_700_000_000 + slot)
    return live


def _state(folder: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(folder.glob("*.hg"))}


def _store_vault(tmp_path: Path) -> Vault:
    vault = Vault(tmp_path / "vault")
    vault.ensure()
    vstore.create_store(vault.root)
    return vault


# --- the newest snapshot of this folder only ------------------------------------------------


def test_undo_only_touches_the_folder_it_is_asked_about(tmp_path):
    one, two = _steam_live(tmp_path, "st_1"), _steam_live(tmp_path, "st_2")
    vault = _store_vault(tmp_path)
    ops.repopulate_slot(vault, one, 2, one, 1, allow_game_running=True)
    ops.repopulate_slot(vault, two, 3, two, 2, allow_game_running=True)   # the newest op overall
    two_after = _state(two)

    ops.undo_last(vault, one, allow_game_running=True)

    view = savedir.scan(one).slots[1]                             # its own snapshot, not st_2's
    assert view.a.save_name == view.b.save_name == "st_1 run 1"
    assert _state(two) == two_after


def test_a_folder_with_no_snapshot_has_nothing_to_undo(tmp_path):
    one, two = _steam_live(tmp_path, "st_1"), _steam_live(tmp_path, "st_2")
    vault = _store_vault(tmp_path)
    ops.repopulate_slot(vault, two, 3, two, 2, allow_game_running=True)

    with pytest.raises(ops.OperationError, match="nothing to undo"):
        ops.undo_last(vault, one, allow_game_running=True)


def test_undo_never_empties_a_slot(tmp_path):
    live = _steam_live(tmp_path, "st_1")
    vault = _store_vault(tmp_path)
    steam_save(live, 4, 0, "Fresh", 10, 0x44, 1_700_000_100)
    ops.repopulate_slot(vault, live, 1, live, 5, allow_game_running=True)   # fills empty slot 5

    ops.undo_last(vault, live, allow_game_running=True)

    assert savedir.scan(live).slots[5].occupied                  # the snapshot lacks it: left alone


def test_an_undo_is_undone_by_its_own_op_and_the_next_undo_steps_back(tmp_path):
    from nms_save_vault.core.liveops import execute
    from nms_save_vault.core.liveops.classic import open_context
    from nms_save_vault.core.liveops.undo import plan_undo as undo_op

    live = _steam_live(tmp_path, "st_1")
    vault = _store_vault(tmp_path)
    ops.repopulate_slot(vault, live, 2, live, 1, allow_game_running=True)
    changed = savedir.scan(live).slots[1].a.save_name

    first = ops.undo_last(vault, live, allow_game_running=True)
    assert first.op_id and savedir.scan(live).slots[1].a.save_name != changed
    with pytest.raises(ops.OperationError, match="nothing to undo"):    # steps back, never forward
        ops.undo_last(vault, live, allow_game_running=True)
    ctx, _ref = open_context(vault, live)                                # the Undo's own undo
    plan = undo_op(ctx, first.op_id)
    assert execute(ctx, plan, plan.confirmations, ).status == "done"
    assert savedir.scan(live).slots[1].a.save_name == changed


def test_plan_undo_names_the_snapshot_and_changes_nothing(tmp_path):
    live = _steam_live(tmp_path, "st_1")
    vault = _store_vault(tmp_path)
    ops.repopulate_slot(vault, live, 2, live, 1, allow_game_running=True)
    before = _state(live)

    plan = ops.plan_undo(vault, live)

    assert plan.snapshot_id and plan.token == plan.snapshot_id
    assert _state(live) == before


def test_an_undo_is_held_to_the_snapshot_the_user_confirmed(tmp_path):
    live = _steam_live(tmp_path, "st_1")
    vault = _store_vault(tmp_path)
    ops.repopulate_slot(vault, live, 2, live, 1, allow_game_running=True)
    plan = ops.plan_undo(vault, live)
    ops.repopulate_slot(vault, live, 3, live, 1, allow_game_running=True)   # a newer snapshot appears
    before = _state(live)

    with pytest.raises(ops.OperationError, match="changed after you were asked"):
        ops.undo_last(vault, live, allow_game_running=True, expected=plan.token)

    assert _state(live) == before


# --- d: an unrecorded backup, extract or import reaches the user ------------------------------


@pytest.mark.parametrize("make", [
    lambda vault, live: ops.create_full_backup(vault, live, include_cache=False),
    lambda vault, live: ops.extract_slot(vault, live, 1),
    lambda vault, live: ops.import_backup(vault, live, copy_into_vault=True),
], ids=["backup", "extract", "import"])
def test_an_unrecorded_vault_copy_warns(tmp_path, monkeypatch, make):
    live = _steam_live(tmp_path, "st_1")
    vault = Vault(tmp_path / "vault")

    def log_unavailable(record):
        raise OSError("disk full")

    monkeypatch.setattr(vault, "append_oplog", log_unavailable)
    with pytest.warns(ops.OplogNotWrittenWarning, match="missing from the history"):
        entry = make(vault, live)
    assert vault.get(entry.id) is not None                       # the copy itself is done


def test_the_unrecorded_warning_points_at_the_caller(make_wgs_account, tmp_path, monkeypatch):
    xbox = make_wgs_account(tmp_path / "xb", [("Slot1Auto", '{"a":1}', "Run", "", 100, 1000)])
    steam = _steam_live(tmp_path, "st_1")
    vault = Vault(tmp_path / "vault")

    def log_unavailable(record):
        raise OSError("disk full")

    monkeypatch.setattr(vault, "append_oplog", log_unavailable)
    for call in (
        lambda: ops.extract_slot(vault, xbox, 1),
        lambda: ops.extract_slot(vault, steam, 1),
        lambda: ops.create_full_backup(vault, steam, include_cache=False),
    ):
        with pytest.warns(ops.OplogNotWrittenWarning) as caught:
            call()
        assert caught[0].filename == __file__


@pytest.mark.parametrize("make", [
    lambda vault, live, w: ops.create_full_backup(vault, live, include_cache=False, warnings=w),
    lambda vault, live, w: ops.extract_slot(vault, live, 1, warnings=w),
    lambda vault, live, w: ops.import_backup(vault, live, copy_into_vault=True, warnings=w),
], ids=["backup", "extract", "import"])
def test_an_unrecorded_vault_copy_reports_into_the_callers_list(tmp_path, monkeypatch, recwarn, make):
    live = _steam_live(tmp_path, "st_1")
    vault = Vault(tmp_path / "vault")

    def log_unavailable(record):
        raise OSError("disk full")

    monkeypatch.setattr(vault, "append_oplog", log_unavailable)
    collected: list[str] = []
    make(vault, live, collected)

    assert len(collected) == 1 and "missing from the history" in collected[0]
    assert not [w for w in recwarn if issubclass(w.category, ops.OplogNotWrittenWarning)]


def test_the_cli_prints_an_unrecorded_backup(tmp_path, monkeypatch, capsys):
    live = _steam_live(tmp_path, "st_1")

    def log_unavailable(self, record):
        raise OSError("disk full")

    monkeypatch.setattr(Vault, "append_oplog", log_unavailable)
    assert cli.main(["--live", str(live), "--vault", str(tmp_path / "vault"), "backup", "--no-cache"]) == 0
    assert "warning: Done, but missing from the history" in capsys.readouterr().out
