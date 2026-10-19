"""Regression tests from the final review: the Xbox index repair is guarded, preserving and never
orphans a save; selftest never calls an unreadable folder clean; redaction; relative CLI paths;
the panic dialog executes exactly the plan shown; no test can show a real dialog or browser.
Synthetic tmp data only."""
from __future__ import annotations

import hashlib
import shutil
import threading
import time
import webbrowser
from pathlib import Path

import pytest

from nms_save_vault import cli
from nms_save_vault.core import aliases, gameversion, msstore, operations as ops, safety, selftest
from nms_save_vault.core import store as vstore
from nms_save_vault.core.catalog import Vault
from nms_save_vault.core.liveops import classic, planner
from nms_save_vault.core.store import versionstore

from .conftest import steam_save
from .store_helpers import IDENT


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setattr(safety, "is_game_running", lambda: False)
    monkeypatch.setattr(versionstore.discover, "all_live_paths", lambda *a, **k: [])
    monkeypatch.setattr(planner, "XBOX_SYNC_SECONDS", 0)


def _wait(app, cond, timeout: float = 60.0) -> None:
    end = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < end, "timed out"
        app.update()
        time.sleep(0.005)


def _xbox_world(tmp_path, make_wgs_account):
    live = make_wgs_account(tmp_path / "live", [
        ("Slot1Auto", '{"a":1}', "Keep", "", 1, 100), ("Slot1Manual", '{"b":1}', "Keep", "", 1, 90)])
    vault = Vault(tmp_path / "vault")
    vault.ensure()
    vstore.create_store(vault.root)
    ops.promote_member(vault, live, 1, 1, allow_game_running=True)
    bk = tmp_path / "bk" / "wgs" / live.name
    shutil.copytree(live, bk)
    entry = ops.import_backup(vault, bk, label="bk")
    return live, vault, entry


def _tree(folder: Path) -> dict:
    return {p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(folder.rglob("*")) if p.is_file()}


def _blob_dirs(live: Path) -> set[str]:
    return {p.name for p in live.iterdir() if p.is_dir()}


# --- 1. the index repair ---------------------------------------------------------------------------


@pytest.mark.parametrize("damage", ["future-header", "garbage"])
def test_an_index_of_an_unknown_or_newer_version_is_never_repaired(tmp_path, make_wgs_account,
                                                                 damage):
    live, vault, entry = _xbox_world(tmp_path, make_wgs_account)
    index = live / "containers.index"
    raw = index.read_bytes()
    index.write_bytes((99).to_bytes(4, "little") + raw[4:] if damage == "future-header"
                      else b"\x00garbage")
    before = _tree(live)
    with pytest.raises(ops.OperationError, match="newer game version"):
        ops.restore_full(vault, entry, live, allow_game_running=True)
    assert _tree(live) == before and not (vault.root / "damaged-index").exists()


def test_a_repair_preserves_the_whole_folder_first_and_lists_every_blob(tmp_path, make_wgs_account):
    live, vault, entry = _xbox_world(tmp_path, make_wgs_account)
    index = live / "containers.index"
    index.write_bytes(index.read_bytes()[:-30])                    # the last record is cut off
    before, blobs = _tree(live), _blob_dirs(live)
    result = ops.restore_full(vault, entry, live, allow_game_running=True)
    assert any("save index was damaged" in w for w in result.warnings)
    kept = [d for d in (vault.root / "damaged-index").iterdir() if d.is_dir()]
    assert len(kept) == 1 and _tree(kept[0]) == before             # index + every blob, verified
    _h, records, _t = msstore.split_index(index.read_bytes())
    held = {msstore._parse_record(rec, live).dir_guid for _i, rec in records}
    assert blobs <= held and {i for i, _ in records} >= {"Slot1Auto", "Slot1Manual"}   # no slot vanished


def test_a_repair_never_leaves_a_blob_unlisted(tmp_path, make_wgs_account):
    live, vault, entry = _xbox_world(tmp_path, make_wgs_account)
    index = live / "containers.index"
    raw = index.read_bytes()
    index.write_bytes(raw[:-130])                                  # more than one record is lost
    cut = index.read_bytes()
    try:
        ops.restore_full(vault, entry, live, allow_game_running=True)
    except ops.OperationError:
        assert index.read_bytes() == cut                           # refused: nothing changed
    else:
        _h, records, _t = msstore.split_index(index.read_bytes())
        held = {msstore._parse_record(rec, live).dir_guid for _i, rec in records}
        assert _blob_dirs(live) <= held


def test_a_save_from_a_newer_game_blocks_the_repair(tmp_path, make_wgs_account):
    import struct

    live, vault, entry = _xbox_world(tmp_path, make_wgs_account)
    _info, containers = msstore.parse_containers_index(live)
    meta = next(c.meta_file for c in containers if c.meta_file is not None)
    blob = bytearray(meta.read_bytes())
    struct.pack_into("<I", blob, 0, gameversion.KNOWN["xbox"].base_version + 1)
    meta.write_bytes(bytes(blob))
    index = live / "containers.index"
    index.write_bytes(index.read_bytes()[:-30])
    before = _tree(live)
    with pytest.raises(ops.OperationError, match="newer game version"):
        ops.restore_full(vault, entry, live, allow_game_running=True)
    assert _tree(live) == before


def test_the_repair_check_is_read_only_and_names_the_problem(tmp_path, make_wgs_account):
    live, vault, entry = _xbox_world(tmp_path, make_wgs_account)
    index = live / "containers.index"
    assert classic.index_repair_problem(vault, entry, live) is None           # nothing damaged
    index.write_bytes((99).to_bytes(4, "little") + index.read_bytes()[4:])
    before = _tree(live)
    assert "newer game version" in classic.index_repair_problem(vault, entry, live)
    assert _tree(live) == before


# --- 2. selftest ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("damage", ["truncate", "header"])
def test_a_damaged_xbox_index_is_a_selftest_problem(tmp_path, make_wgs_account, capsys, damage):
    live, _vault, _entry = _xbox_world(tmp_path, make_wgs_account)
    index = live / "containers.index"
    raw = index.read_bytes()
    index.write_bytes(raw[:-30] if damage == "truncate" else (99).to_bytes(4, "little") + raw[4:])
    code = cli.main(["--vault", str(tmp_path / "novault"), "--live", str(live), "selftest"])
    out = capsys.readouterr().out
    assert code == 1 and "result: needs an app update" in out


def test_a_steam_meta_that_does_not_read_is_a_selftest_problem(tmp_path, capsys):
    from nms_save_vault.core import slotmap

    live = tmp_path / "NMS" / "st_76561198000000001"
    steam_save(live, 1, 0, "One", 100, IDENT, 1_790_000_000, extra="x")
    mp = live / slotmap.meta_filename(slotmap.slot_file_numbers(1)[0])
    mp.write_bytes((bytes(range(256)) * 2)[: len(mp.read_bytes())])
    code = cli.main(["--vault", str(tmp_path / "novault"), "--live", str(live), "selftest"])
    out = capsys.readouterr().out
    assert code == 1 and "saves looked at: 0" in out and "result: clean" not in out


def test_live_folders_with_nothing_looked_at_are_never_clean(tmp_path):
    empty = tmp_path / "NMS" / "st_76561198000000002"
    empty.mkdir(parents=True)
    from nms_save_vault.core.state import Source

    report = selftest.run(None, [Source(id="s", platform="steam", account=empty.name, path=str(empty))])
    assert report.saves == 0 and report.unreadable >= 1 and report.exit_code == 1


# --- 3 and 4. redaction ---------------------------------------------------------------------------------


def test_the_panic_dialog_heading_and_the_status_bar_are_scrubbed(gui_app, tmp_path, monkeypatch):
    from nms_save_vault.core import operations
    from tests.store_helpers import steam_live

    live = steam_live(tmp_path / "NMS")
    vault = Vault(tmp_path / "vault")
    operations.create_full_backup(vault, live, warnings=[])
    vstore.run_upgrade(vstore.plan_upgrade(vault.root, live_roots=[live]))
    backup = operations.create_full_backup(vault, live, warnings=[])
    monkeypatch.setattr(gui_app, "vault", vault)
    monkeypatch.setattr(gui_app, "live_dir", live)
    monkeypatch.setattr(gui_app, "_require_live", lambda: True)
    gui_app.on_restore({"type": "entry", "entry": backup})
    d = gui_app.panic_dialog
    heading = next(w for w in d.summary.master.winfo_children() if str(w.winfo_class()) == "TLabel")
    assert "76561198" not in heading.cget("text") and "76561198" not in d.title()
    d.destroy()
    monkeypatch.setattr(gui_app, "live_dir", live)
    gui_app.refresh()
    assert "76561198" not in gui_app.status.get()


# --- 5. relative paths at the CLI boundary ---------------------------------------------------------------


def test_relative_cli_paths_work_as_before_while_the_core_refuses_empty_ones(tmp_path, monkeypatch,
                                                                           capsys):
    src = tmp_path / "work" / "bk" / "st_76561198000000001"
    steam_save(src, 1, 0, "Main", 100, IDENT, 1_790_000_000, extra="m")
    vault = tmp_path / "vault"
    Vault(vault).ensure()
    monkeypatch.chdir(tmp_path / "work")
    assert cli.main(["--vault", str(vault), "extract", "1", "--from", "bk/st_76561198000000001"]) == 0
    assert cli.main(["--vault", str(vault), "--live", "bk/st_76561198000000001", "extract", "1"]) == 0
    capsys.readouterr()
    with pytest.raises(ops.OperationError):
        ops.extract_slot(Vault(vault), "", 1, warnings=[])


# --- 6. the panic dialog executes exactly the plan shown ----------------------------------------------


def _panic_world(gui_app, tmp_path, monkeypatch):
    from tests.store_helpers import steam_live

    live = steam_live(tmp_path / "NMS")
    vault = Vault(tmp_path / "vault")
    ops.create_full_backup(vault, live, warnings=[])
    vstore.run_upgrade(vstore.plan_upgrade(vault.root, live_roots=[live]))
    backup = ops.create_full_backup(vault, live, warnings=[])
    monkeypatch.setattr(gui_app, "vault", vault)
    monkeypatch.setattr(gui_app, "live_dir", live)
    monkeypatch.setattr(gui_app, "_require_live", lambda: True)
    steam_save(live, 1, 0, "Changed", 100, IDENT, 1_790_100_000, extra="changed-a")
    return live, vault, backup


def test_the_confirmed_game_state_and_the_plan_fingerprint_reach_the_restore(gui_app, tmp_path,
                                                                           monkeypatch, dialogs):
    dialogs.expect()
    live, vault, backup = _panic_world(gui_app, tmp_path, monkeypatch)
    seen = {}
    real = classic.restore_full

    def spy(*args, **kw):
        seen["allow"], seen["fp"] = args[4], kw.get("expected_fingerprint")
        return real(*args, **kw)
    monkeypatch.setattr(classic, "restore_full", spy)
    gui_app.on_restore({"type": "entry", "entry": backup})
    d = gui_app.panic_dialog
    _wait(gui_app, lambda: d.plan is not None)
    plan = d.plan
    for v in d.vars.values():
        v.set(True)
    d.vars["GAME_STATE_UNKNOWN"] = type("V", (), {"get": staticmethod(lambda: True)})()
    d._update()
    d.apply()
    _wait(gui_app, lambda: not d.winfo_exists() or (not d.busy and d.error.cget("text")))
    assert seen["fp"] == plan.fingerprint and seen["allow"] is True
    dialogs.take()


def test_a_folder_that_changed_after_the_plan_is_not_restored(gui_app, tmp_path, monkeypatch, dialogs):
    dialogs.expect()
    live, vault, backup = _panic_world(gui_app, tmp_path, monkeypatch)
    gui_app.on_restore({"type": "entry", "entry": backup})
    d = gui_app.panic_dialog
    _wait(gui_app, lambda: d.plan is not None)
    steam_save(live, 2, 0, "ChangedAgain", 5, IDENT + 3, 1_790_200_000, extra="again")   # after the plan
    for v in d.vars.values():
        v.set(True)
    d._update()
    d.apply()
    _wait(gui_app, lambda: not d.busy and d.error.cget("text"))
    assert "changed since you looked" in d.error.cget("text")
    from nms_save_vault.core import savedir
    assert savedir.scan_any(live).slots[1].display_name == "Changed"     # nothing was restored
    d.destroy()


# --- 7. the dialog stubs are in place before the session App ----------------------------------------------


def test_no_dialog_and_no_browser_can_open_in_a_test(gui_app, dialogs):
    from tkinter import messagebox

    assert messagebox.showinfo.__name__ == "shown" and webbrowser.open.__name__ == "browser"
    with pytest.raises(AssertionError, match="unexpected dialog"):
        messagebox.showinfo("x", "y")
    with pytest.raises(AssertionError, match="unexpected browser"):
        webbrowser.open("https://example.test")
    import tkinter as tk
    top = tk.Toplevel(gui_app)
    try:
        with pytest.raises(AssertionError, match="waited on a window"):
            top.wait_window()
    finally:
        top.destroy()
    from nms_save_vault import gui
    assert gui._showinfo.__name__ == "shown"
    assert len(dialogs.take()) == 2            # the attempts were recorded, and all of them raised
