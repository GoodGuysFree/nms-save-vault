"""Undo (P4): only the target folder's own operations, several levels deep, and never twice.

Built on synthetic Steam folders (``steam_save``) and wgs accounts (``make_wgs_account``) in a
temp sandbox; no real save folder is touched.
"""
from __future__ import annotations

import json
import pathlib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from nms_save_vault import cli
from nms_save_vault.core import msstore, savedir, slotmap
from nms_save_vault.core import operations as ops
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


def _rewrite_oplog(vault: Vault, change) -> None:
    lines = [json.loads(l) for l in vault.oplog_path.read_text("utf-8").splitlines() if l.strip()]
    vault.oplog_path.write_text("".join(json.dumps(change(r)) + "\n" for r in lines), "utf-8")


# --- a: the target folder only -----------------------------------------------------------


def test_undo_only_touches_the_folder_it_is_asked_about(tmp_path):
    one, two = _steam_live(tmp_path, "st_1"), _steam_live(tmp_path, "st_2")
    vault = Vault(tmp_path / "vault")
    one_before = _state(one)
    ops.clear_slot(vault, one, 1, allow_game_running=True)
    ops.clear_slot(vault, two, 2, allow_game_running=True)        # the newest op overall
    two_after = _state(two)

    ops.undo_last(vault, one, allow_game_running=True)

    assert _state(one) == one_before                              # its own snapshot, not st_2's
    assert _state(two) == two_after


def test_old_records_are_placed_by_their_snapshot_folder_name(tmp_path):
    one, two = _steam_live(tmp_path, "st_1"), _steam_live(tmp_path, "st_2")
    vault = Vault(tmp_path / "vault")
    one_before = _state(one)
    ops.clear_slot(vault, one, 1, allow_game_running=True)
    ops.clear_slot(vault, two, 2, allow_game_running=True)
    _rewrite_oplog(vault, lambda r: {k: v for k, v in r.items() if k != "target"})  # as before

    ops.undo_last(vault, one, allow_game_running=True)

    assert _state(one) == one_before and not savedir.scan(two).slots[2].occupied


def test_an_old_record_that_cannot_be_placed_stops_the_undo(tmp_path):
    """Its snapshot is still there but names no folder: it might be this folder's newest
    change, so undo refuses rather than guess."""
    one = _steam_live(tmp_path, "st_1")
    vault = Vault(tmp_path / "vault")
    result = ops.clear_slot(vault, one, 1, allow_game_running=True)
    _rewrite_oplog(vault, lambda r: {k: v for k, v in r.items() if k != "target"})
    vault.upsert(replace(vault.get(result.snapshot_id), source=None))

    with pytest.raises(ops.OperationError, match="cannot tell whether it belongs"):
        ops.undo_last(vault, one, allow_game_running=True)
    assert not savedir.scan(one).slots[1].occupied


def test_an_old_record_whose_snapshot_is_gone_does_not_block_other_folders(make_wgs_account, tmp_path):
    """It could not be undone anyway: undo passes over it, says so, and goes on."""
    xbox = make_wgs_account(tmp_path / "xb", [("Slot1Auto", '{"a":1}', "Run", "", 100, 1000)])
    steam = _steam_live(tmp_path, "st_1")
    vault = Vault(tmp_path / "vault")
    ops.clear_slot(vault, xbox, 1, allow_game_running=True)
    legacy = ops.clear_slot(vault, steam, 1, allow_game_running=True)   # the newest record
    _rewrite_oplog(vault, lambda r: r if r["snapshot_id"] != legacy.snapshot_id
                   else {k: v for k, v in r.items() if k != "target"})
    vault.remove(legacy.snapshot_id)

    result = ops.undo_last(vault, xbox, allow_game_running=True)

    assert savedir.scan_any(xbox).slots[1].occupied
    assert any(w.startswith("A newer operation") and "passed over" in w for w in result.warnings)
    with pytest.raises(ops.OperationError, match="nothing to undo.*An operation .*passed over"):
        ops.undo_last(vault, steam, allow_game_running=True)


def test_a_folder_name_shared_with_another_known_folder_cannot_place_an_old_record(tmp_path, monkeypatch, capsys):
    """Old records are placed by folder name; when another known live folder has the same
    name, undo refuses rather than guess -- in the core and through the CLI."""
    one = _steam_live(tmp_path / "a", "st_1")
    twin = _steam_live(tmp_path / "b", "st_1")
    vault = Vault(tmp_path / "vault")
    ops.clear_slot(vault, one, 1, allow_game_running=True)
    _rewrite_oplog(vault, lambda r: {k: v for k, v in r.items() if k != "target"})

    with pytest.raises(ops.OperationError, match="cannot tell whether it belongs"):
        ops.undo_last(vault, one, allow_game_running=True, other_live_dirs=[one, twin])

    known = SimpleNamespace(live_sources=[SimpleNamespace(path=str(one)), SimpleNamespace(path=str(twin))])
    monkeypatch.setattr(cli.appstate, "load", lambda *a, **k: known)
    assert cli.main(["--live", str(one), "--vault", str(vault.root), "undo", "--force"]) == 1
    assert "cannot tell whether it belongs" in capsys.readouterr().err

    ops.undo_last(vault, one, allow_game_running=True)        # with no twin known, it is placed
    assert savedir.scan(one).slots[1].occupied


def test_nothing_to_undo_names_this_folders_own_last_change(tmp_path):
    one, two = _steam_live(tmp_path, "st_1"), _steam_live(tmp_path, "st_2")
    vault = Vault(tmp_path / "vault")
    ops.clear_slot(vault, two, 2, allow_game_running=True)
    with pytest.raises(ops.OperationError, match="records no change to it"):
        ops.undo_last(vault, one, allow_game_running=True)

    ops.clear_slot(vault, one, 1, allow_game_running=True)
    ops.undo_last(vault, one, allow_game_running=True)
    ops.clear_slot(vault, two, 3, allow_game_running=True)      # the newest record overall

    with pytest.raises(ops.OperationError) as nothing:
        ops.undo_last(vault, one, allow_game_running=True)
    assert "last recorded change to it was 'undo'" in str(nothing.value)


# --- b: several levels, never the same snapshot twice -------------------------------------


def test_repeated_undo_steps_back_to_the_state_before_the_first_operation(tmp_path):
    live = _steam_live(tmp_path, "st_1")
    vault = Vault(tmp_path / "vault")
    before = _state(live)
    ops.clear_slot(vault, live, 1, allow_game_running=True)       # A
    ops.clear_slot(vault, live, 2, allow_game_running=True)       # B

    ops.undo_last(vault, live, allow_game_running=True)           # undoes B
    assert savedir.scan(live).slots[2].occupied and not savedir.scan(live).slots[1].occupied
    ops.undo_last(vault, live, allow_game_running=True)           # undoes A, not B again

    assert _state(live) == before
    records = vault.read_oplog()
    assert [r["op"] for r in records[-2:]] == ["undo", "undo"]
    with pytest.raises(ops.OperationError, match="nothing to undo"):
        ops.undo_last(vault, live, allow_game_running=True)


def test_an_undo_written_by_an_older_version_is_not_repeated(tmp_path):
    """Before this version an undo logged a restore_full of the snapshot and then an 'undo'
    line naming that snapshot. Neither is undone again, and the undone op is skipped."""
    live = _steam_live(tmp_path, "st_1")
    vault = Vault(tmp_path / "vault")
    ops.clear_slot(vault, live, 1, allow_game_running=True)       # A
    b = ops.clear_slot(vault, live, 2, allow_game_running=True)   # B
    ops.restore_full(vault, vault.get(b.snapshot_id), live, allow_game_running=True)
    vault.append_oplog({"ts": "2026-10-01T00:00:00", "op": "undo", "entry_id": None,
                        "snapshot_id": b.snapshot_id, "detail": "undid clear_slot", "changed": []})

    ops.undo_last(vault, live, allow_game_running=True)

    view = savedir.scan(live)
    assert view.slots[1].occupied and view.slots[2].occupied      # A undone; B stays undone


# --- c: a failed read-back is still undoable -------------------------------------------------


def test_a_steam_repopulate_that_reads_back_wrong_is_logged_and_undoable(tmp_path, monkeypatch):
    live = _steam_live(tmp_path, "st_1")
    (live / slotmap.data_filename(slotmap.slot_file_numbers(3)[0])).unlink()
    (live / slotmap.meta_filename(slotmap.slot_file_numbers(3)[0])).unlink()
    vault = Vault(tmp_path / "vault")
    real_write = ops.safety.atomic_write_bytes

    def corrupt_metas(path, data, *args, **kwargs):
        if Path(path).name.startswith("mf_"):
            data = bytes(b ^ 0xFF for b in data)
        return real_write(path, data, *args, **kwargs)

    monkeypatch.setattr(ops.safety, "atomic_write_bytes", corrupt_metas)
    with pytest.raises(ops.WrittenButInvalidError) as failed:
        ops.repopulate_slot(vault, live, 1, live, 3, allow_game_running=True)
    monkeypatch.undo()

    assert vault.read_oplog()[-1]["snapshot_id"] == failed.value.result.snapshot_id
    ops.undo_last(vault, live, allow_game_running=True)
    assert not savedir.scan(live).slots[3].occupied


def test_a_steam_clear_that_does_not_take_is_logged(tmp_path, monkeypatch):
    live = _steam_live(tmp_path, "st_1")
    vault = Vault(tmp_path / "vault")
    monkeypatch.setattr(pathlib.Path, "unlink", lambda self, missing_ok=False: None)

    with pytest.raises(ops.WrittenButInvalidError) as failed:
        ops.clear_slot(vault, live, 1, allow_game_running=True)
    monkeypatch.undo()

    assert vault.read_oplog()[-1]["snapshot_id"] == failed.value.result.snapshot_id


def test_an_xbox_clear_that_does_not_take_is_logged(make_wgs_account, tmp_path, monkeypatch):
    live = make_wgs_account(tmp_path, [("Slot1Auto", '{"a":1}', "Run", "", 100, 1000)])
    vault = Vault(tmp_path / "vault")
    monkeypatch.setattr(msstore, "delete_save", lambda folder, identifier, when: True)

    with pytest.raises(ops.WrittenButInvalidError) as failed:
        ops.clear_slot(vault, live, 1, allow_game_running=True)

    assert vault.read_oplog()[-1]["snapshot_id"] == failed.value.result.snapshot_id


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


# --- e: damage can be accepted ---------------------------------------------------------------


def test_damage_after_the_operation_can_be_accepted(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cli.appstate, "load", lambda *a, **k: None)   # no real config read
    live = _steam_live(tmp_path, "st_1")
    vault = Vault(tmp_path / "vault")
    ops.clear_slot(vault, live, 1, allow_game_running=True)
    with open(vault.oplog_path, "ab") as f:
        f.write(b'{"ts": "2026-10-17T12:01:00", "op": "clea')

    with pytest.raises(ops.OperationError, match="damaged, so undo is unsafe"):
        ops.undo_last(vault, live, allow_game_running=True)
    assert cli.main(["--live", str(live), "--vault", str(vault.root), "undo", "--force"]) == 1

    code = cli.main(["--live", str(live), "--vault", str(vault.root), "undo", "--force", "--accept-damaged-log"])

    assert code == 0 and savedir.scan(live).slots[1].occupied
    assert "damaged lines" in capsys.readouterr().out             # the warning is shown


# --- f: what an undo would do, asked before doing it (the GUI names it) -----------------------


def test_plan_undo_names_the_change_and_changes_nothing(tmp_path):
    live = _steam_live(tmp_path, "st_1")
    vault = Vault(tmp_path / "vault")
    ops.clear_slot(vault, live, 2, allow_game_running=True)
    after, log = _state(live), vault.oplog_path.read_bytes()

    plan = ops.plan_undo(vault, live)

    assert (plan.op, plan.damaged_after) == ("clear_slot", False)
    assert plan.snapshot_id in [e.id for e in vault.entries]
    assert _state(live) == after and vault.oplog_path.read_bytes() == log


def test_plan_undo_reports_damage_that_undo_would_refuse(tmp_path):
    live = _steam_live(tmp_path, "st_1")
    vault = Vault(tmp_path / "vault")
    ops.clear_slot(vault, live, 1, allow_game_running=True)
    with open(vault.oplog_path, "ab") as f:
        f.write(b'{"ts": "2026-10-17T12:01:00", "op": "clea')

    plan = ops.plan_undo(vault, live)

    assert plan.damaged_after and "damaged" in plan.log_warning
    with pytest.raises(ops.OperationError, match="damaged, so undo is unsafe"):
        ops.undo_last(vault, live, allow_game_running=True)


def test_plan_undo_refuses_where_undo_would(tmp_path):
    live = _steam_live(tmp_path, "st_1")
    with pytest.raises(ops.OperationError, match="Nothing has been done yet"):
        ops.plan_undo(Vault(tmp_path / "vault"), live)


def test_an_undo_is_held_to_the_change_the_user_confirmed(tmp_path):
    live = _steam_live(tmp_path, "st_1")
    vault = Vault(tmp_path / "vault")
    ops.clear_slot(vault, live, 1, allow_game_running=True)
    confirmed = ops.plan_undo(vault, live)
    ops.clear_slot(vault, live, 2, allow_game_running=True)        # a newer change meanwhile
    before = _state(live)

    with pytest.raises(ops.OperationError, match="changed after you were asked"):
        ops.undo_last(vault, live, allow_game_running=True, acknowledge_damage=True, expected=confirmed.token)
    assert _state(live) == before

    ops.undo_last(vault, live, allow_game_running=True, expected=ops.plan_undo(vault, live).token)
    assert savedir.scan(live).slots[2].occupied and not savedir.scan(live).slots[1].occupied


def test_an_undo_is_refused_when_damage_appears_after_the_user_was_asked(tmp_path):
    live = _steam_live(tmp_path, "st_1")
    vault = Vault(tmp_path / "vault")
    ops.clear_slot(vault, live, 1, allow_game_running=True)
    confirmed = ops.plan_undo(vault, live)
    with open(vault.oplog_path, "ab") as f:
        f.write(b'{"ts": "2026-10-17T12:01:00", "op": "clea')
    before = _state(live)

    with pytest.raises(ops.OperationError, match="changed after you were asked"):
        ops.undo_last(vault, live, allow_game_running=True, acknowledge_damage=True, expected=confirmed.token)
    assert _state(live) == before


def test_a_legacy_xbox_record_still_undoes_with_its_xgs_twin_known(make_wgs_account, tmp_path):
    """Placed by folder name, an old record must not count the live folder's own xgs mirror
    -- same name, same platform -- as another folder that makes it ambiguous."""
    live = make_wgs_account(tmp_path / "SystemAppData", [
        ("Slot1Auto", '{"a":1}', "Run", "", 100, 1000), ("Slot2Auto", '{"b":2}', "Other", "", 200, 1000),
    ])
    twin = tmp_path / "SystemAppData" / "xgs" / live.name
    for identifier in ("Slot1Auto", "Slot2Auto"):
        (twin / identifier).mkdir(parents=True)
        (twin / identifier / "data").write_bytes(b"x")
        (twin / identifier / "meta").write_bytes(b"x")
    vault = Vault(tmp_path / "vault")
    ops.clear_slot(vault, live, 1, allow_game_running=True)
    _rewrite_oplog(vault, lambda r: {k: v for k, v in r.items() if k != "target"})

    ops.undo_last(vault, live, allow_game_running=True, other_live_dirs=[live, twin])

    assert savedir.scan_any(live).slots[1].occupied
