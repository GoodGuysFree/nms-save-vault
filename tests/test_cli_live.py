"""The live-operation commands: place, swap, keep, undo, ops, recover, clear and
panic-restore (D5 4.5). Every write runs in the tmp_path worlds of ``steam_world``."""
from __future__ import annotations

import pytest

from nms_save_vault import cli
from nms_save_vault.core import safety, savedir
from nms_save_vault.core import state as appstate
from nms_save_vault.core.organizer import Organizer
from nms_save_vault.core.state import AppState

from .liveops_support import STEAM_ACCOUNT, steam_world, tree_hash

ONE = f"steam:{STEAM_ACCOUNT}"


@pytest.fixture(autouse=True)
def quiet(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    monkeypatch.setattr(safety, "is_game_running", lambda: False)
    real_init = Organizer.__init__
    monkeypatch.setattr(Organizer, "__init__", lambda self, *a, **k: real_init(
        self, *a, **{"game_running": lambda: False, **k}))


@pytest.fixture
def world(tmp_path, monkeypatch):
    world = steam_world(tmp_path)
    state = AppState(sources=world.sources, vault=str(world.vault_root))
    monkeypatch.setattr(appstate, "load", lambda path=None: state)
    return world


def run(capsys, *argv) -> tuple[int, str]:
    code = cli.main(list(argv))
    return code, capsys.readouterr().out


def test_place_prints_the_plan_and_asks_for_the_confirmations_it_names(world, capsys):
    before = tree_hash(world.live)
    args = ["place", f"{ONE}:1", "--from", world.backup_id, "--src-slot", "3", "--yes"]

    with pytest.raises(SystemExit) as stop:
        cli.main(args)

    assert "--confirm OTHER_ACCOUNT" in str(stop.value)
    assert tree_hash(world.live) == before                 # nothing written without the answer
    code, out = run(capsys, *args, "--confirm", "OTHER_ACCOUNT")
    assert code == 0 and "done:" in out and "nmsvault undo --op" in out
    assert savedir.scan_slot(world.live, 1).newest.label in ("A", "B")


def test_a_blocked_plan_says_why_and_writes_nothing(world, capsys):
    before = tree_hash(world.live)

    code = cli.main(["swap", f"{ONE}:1", f"{ONE}:5", "--yes"])
    captured = capsys.readouterr()

    assert code == 2 and "blocked:" in captured.err and "never empties a slot" in captured.err
    assert captured.out == ""                               # a refusal is not a result
    assert tree_hash(world.live) == before


def test_swap_ops_and_the_slot_scoped_undo(world, capsys):
    code, out = run(capsys, "swap", f"{ONE}:1", f"{ONE}:2", "--yes")
    assert code == 0 and "Swapped" in out
    assert savedir.scan_slot(world.live, 1).members[0].data_path.is_file()

    code, out = run(capsys, "ops", f"{ONE}:1")
    assert code == 0 and "swap" in out

    code, out = run(capsys, "undo", "--to", f"{ONE}:1", "--yes")
    assert code == 0 and "back" in out

    code, out = run(capsys, "ops", f"{ONE}:1")
    assert "undo of op-" in out


def test_keep_copies_a_slot_into_the_vault_and_changes_nothing_live(world, capsys):
    before = tree_hash(world.live)

    code, out = run(capsys, "keep", f"{ONE}:1")

    assert code == 0 and "in the vault" in out
    assert tree_hash(world.live) == before


def test_clear_prints_the_steps_and_deletes_nothing(world, capsys):
    before = tree_hash(world.live)

    code, out = run(capsys, "clear", f"{ONE}:1")

    assert code == 0 and "never empties a slot" in out and "1. " in out
    assert "nmsvault keep" in out                          # the slot is not in the vault yet
    assert tree_hash(world.live) == before


def test_recover_with_nothing_open(world, capsys):
    code, out = run(capsys, "recover")
    assert code == 0 and "nothing to recover" in out


def test_the_whole_folder_undo_is_the_panic_restore(world, capsys):
    run(capsys, "place", f"{ONE}:1", "--from", world.backup_id, "--src-slot", "3", "--yes",
        "--confirm", "OTHER_ACCOUNT")
    placed = savedir.scan_slot(world.live, 1).members[0].info.save_name

    code, out = run(capsys, "--live", str(world.live), "undo", "--whole-folder", "--yes")

    assert code == 0 and "restored from the snapshot" in out
    assert savedir.scan_slot(world.live, 1).members[0].info.save_name != placed   # Main is back


def test_a_slot_needs_its_account_when_there_are_two(world, capsys):
    with pytest.raises(SystemExit) as stop:
        cli.main(["keep", "steam:1"])
    assert "steam:<account>:1" in str(stop.value)
