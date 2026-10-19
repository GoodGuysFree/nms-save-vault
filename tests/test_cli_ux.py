"""The command line's plain words, exit codes and refusals (whole-program UX review): errors are
one plain line on stderr, a path that is not there is refused, store backups work in every
command, and slots are written both ways. Every world lives under tmp_path."""
from __future__ import annotations

import argparse
import re

import pytest

from nms_save_vault import cli, cli_store
from nms_save_vault.core import safety
from nms_save_vault.core import state as appstate
from nms_save_vault.core.organizer import Organizer
from nms_save_vault.core.state import AppState

from .liveops_support import STEAM_ACCOUNT, steam_world

ONE = f"steam:{STEAM_ACCOUNT}"


@pytest.fixture(autouse=True)
def quiet(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    monkeypatch.delenv("NMSVAULT_DEBUG", raising=False)
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


def run(capsys, *argv):
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def refused(*argv) -> str:
    with pytest.raises(SystemExit) as stop:
        cli.main(list(argv))
    return str(stop.value.code)


# --- M2: no traceback ------------------------------------------------------------------------


def test_an_os_error_is_one_plain_line_and_exit_1(world, capsys, monkeypatch):
    from nms_save_vault.core.store import classic

    def gone(*a, **k):
        raise FileNotFoundError(3, "The system cannot find the path specified", "Q:\\nope")

    monkeypatch.setattr(classic, "list_entries", gone)

    code, out, err = run(capsys, "list")

    assert code == 1 and out == ""
    assert "'Q:\\nope' was not found" in err and "WinError" not in err and "Traceback" not in err


def test_an_unreadable_file_is_named_and_any_other_error_is_not_called_damage(
        world, capsys, monkeypatch):
    import json

    from nms_save_vault.core.store import classic

    def unreadable(*a, **k):
        raise json.JSONDecodeError("Expecting value", "", 0)

    monkeypatch.setattr(classic, "list_entries", unreadable)
    code, _out, err = run(capsys, "list")
    assert code == 1 and "damaged and cannot be understood" in err and "NMSVAULT_DEBUG=1" in err

    def bug(*a, **k):
        raise KeyError("slot")

    monkeypatch.setattr(classic, "list_entries", bug)
    code, _out, err = run(capsys, "list")
    assert code == 1 and "Something went wrong: 'slot'" in err
    assert "damaged" not in err and "not changed" not in err

    monkeypatch.setenv("NMSVAULT_DEBUG", "0")                 # off, not "set"
    assert run(capsys, "list")[0] == 1
    for on in ("1", "true", "YES"):
        monkeypatch.setenv("NMSVAULT_DEBUG", on)
        with pytest.raises(KeyError):
            cli.main(["list"])


def test_a_prompt_with_nobody_to_answer_says_to_use_yes(monkeypatch):
    def no_terminal(prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", no_terminal)

    with pytest.raises(SystemExit) as stop:
        cli_store._ask("Upgrade the vault now?")

    assert str(stop.value.code) == "error: this needs --yes when it is not run in a terminal"


# --- m11: a path that is not there is refused ----------------------------------------------


def test_a_missing_live_folder_is_refused_not_read_as_clean(world, tmp_path):
    for command in (["verify"], ["status"], ["board"], ["selftest"]):
        message = refused("--live", str(tmp_path / "nope"), *command)
        assert "does not exist" in message and "--live" in message
    assert "is a file" in refused("--live", str(world.live / "accountdata.hg"), "status")


def test_a_missing_vault_is_refused_not_read_as_an_empty_one(world, tmp_path):
    assert "no vault at" in refused("--vault", str(tmp_path / "newvault"), "list")
    assert "parent folder does not exist" in refused("--vault", str(tmp_path / "Q" / "v"), "list")
    assert "is a file" in refused("--vault", str(world.live / "accountdata.hg"), "list")


def test_a_backup_may_start_a_vault_at_a_new_path(world, tmp_path, capsys):
    code, out, _err = run(capsys, "--vault", str(tmp_path / "fresh"), "backup")
    assert code == 0 and "created full backup" in out


def test_backup_of_a_folder_with_no_saves_is_refused(world, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()

    assert "holds no saves" in refused("--live", str(empty), "backup")


# --- M4: store backups in verify and status; plain words -----------------------------------


def test_verify_and_show_resolve_a_store_backup(world, capsys):
    code, out, _err = run(capsys, "verify", world.backup_id)
    assert code == 0 and "all present saves valid" in out

    code, out, _err = run(capsys, "show", world.backup_id)
    assert code == 0 and "Polestar" in out


def test_status_counts_the_store_backups(world, capsys):
    code, out, _err = run(capsys, "status")
    assert code == 0 and "(1 backup)" in out and "catalog entries" not in out


def test_list_keeps_the_id_apart_from_the_kind(world, capsys):
    code, out, _err = run(capsys, "list")
    row = next(ln for ln in out.splitlines() if ln.startswith(world.backup_id))
    assert re.match(rf"{re.escape(world.backup_id)}\s{{2,}}full\s", row)


def test_extract_from_a_store_backup_is_labelled_with_its_id_not_a_temp_folder(world, capsys):
    code, _out, _err = run(capsys, "extract", "3", "--from", world.backup_id)
    assert code == 0

    _code, listing, _err = run(capsys, "list")
    assert f"slot 3 from {world.backup_id}" in listing and "mat-" not in listing


def test_a_missing_backup_id_is_named_as_such(world):
    assert "neither a backup id nor a folder" in refused("verify", "no-such-backup")


# --- m3 / m13: refusals are plain and on stderr ------------------------------------------


def test_a_live_folder_that_is_not_a_save_folder_is_said_in_words(world, tmp_path, capsys):
    empty = tmp_path / "nothing-here"
    empty.mkdir()

    code, out, err = run(capsys, "--live", str(empty), "keep", "steam:nothing-here:1")

    assert code == 1
    assert "NOT_A_LIVE_FOLDER" not in out + err and "No Man's Sky save folder" in err
    assert "BAD_TARGET" not in out + err


def test_the_confirmation_advice_shows_which_line_it_confirms(world, capsys):
    message = refused("place", f"{ONE}:1", "--from", world.backup_id, "--src-slot", "3", "--yes")

    assert "--confirm OTHER_ACCOUNT (for: This save comes from another account" in message
    _code, out, _err = run(capsys, "place", f"{ONE}:1", "--from", world.backup_id,
                           "--src-slot", "3", "--confirm", "OTHER_ACCOUNT", "--yes")
    assert "WARN" not in out


def test_a_blocked_plan_is_on_stderr_once(world, capsys):
    code, out, err = run(capsys, "keep", f"{ONE}:9")

    assert code == 2 and out == ""
    assert "blocked:" in err and err.count("is empty") == 1          # said once, not twice


def test_keep_says_keeps_not_replaces(world, capsys):
    code, out, _err = run(capsys, "keep", f"{ONE}:1")
    assert code == 0 and "replaces" not in out


def test_a_running_game_is_not_told_to_use_force(world, monkeypatch, capsys):
    from nms_save_vault.core import operations as ops

    def running(*a, **k):
        raise ops.GameRunningError("No Man's Sky appears to be running. Close the game first.")

    monkeypatch.setattr(ops, "promote_member", running)

    code, _out, err = run(capsys, "--live", str(world.live), "promote", "--slot", "1",
                          "--member", "B")

    assert code == 2 and "--force to override" not in err and "close the game" in err


# --- m14: the slot in either spelling; the whole-folder step ------------------------------


def test_a_slot_is_accepted_as_a_badge_or_as_steam_colon(world, capsys):
    _code, by_words, _ = run(capsys, "ops", f"{ONE}:1")
    _code, by_badge, _ = run(capsys, "ops", "S1.1")
    _code, other_words, _ = run(capsys, "ops", f"steam:{STEAM_ACCOUNT}:1")
    assert by_words == by_badge == other_words
    assert "S2.7" in cli.build_parser().epilog and "X4" in cli.build_parser().epilog


def test_undo_to_a_slot_does_not_silently_restore_the_whole_folder(world, capsys):
    run(capsys, "place", f"{ONE}:1", "--from", world.backup_id, "--src-slot", "3", "--yes",
        "--confirm", "OTHER_ACCOUNT")
    code, out, _err = run(capsys, "--live", str(world.live), "undo", "--whole-folder", "--yes")
    assert code == 0, out

    message = refused("undo", "--to", f"{ONE}:1", "--yes")

    assert "--whole-folder" in message and "several slots" in message


def test_an_unknown_move_or_snapshot_is_reported_as_unknown(world):
    assert "no move with the id op-nope" in refused("undo", "--op", "op-nope", "--yes")
    assert "no snapshot or backup 'nope'" in refused(
        "--live", str(world.live), "panic-restore", "nope", "--yes")


# --- m15: help everywhere ---------------------------------------------------------------


def _leaves(parser):
    subs = next((a for a in parser._actions if isinstance(a, argparse._SubParsersAction)), None)
    if subs is None:
        yield parser
        return
    for sub in subs.choices.values():
        yield from _leaves(sub)


def test_every_option_and_argument_has_help():
    missing = []
    for leaf in _leaves(cli.build_parser()):
        for action in leaf._actions:
            if isinstance(action, argparse._HelpAction) or action.help is not None:
                continue
            missing.append(f"{leaf.prog}: {action.dest}")
    assert missing == []


def test_every_command_help_shows_live_and_vault(capsys):
    for leaf in _leaves(cli.build_parser()):
        text = leaf.format_help()
        assert "--live" in text and "--vault" in text, leaf.prog


def test_a_vault_that_cannot_be_opened_for_live_operations_is_said_in_words(
        world, monkeypatch, capsys):
    def gone(self, *a, **k):
        raise FileNotFoundError(3, "The system cannot find the path specified", "Q:\\")

    monkeypatch.setattr(Organizer, "__init__", gone)

    message = refused("keep", f"{ONE}:1")

    assert "was not found" in message and "WinError" not in message and "[Errno" not in message


def test_a_promote_reports_in_words_not_in_the_operations_name(world, capsys):
    code, out, _err = run(capsys, "--live", str(world.live), "promote", "--slot", "1",
                          "--member", "B")

    assert code == 0 and "made newest: done" in out and "promote_member" not in out


def test_verify_of_a_store_backup_names_the_backup_not_a_temporary_folder(world, capsys):
    _code, out, _err = run(capsys, "verify", world.backup_id)

    assert f"Backup: {world.backup_id}" in out and "mat-" not in out and "tmp" not in out


def test_the_settings_notices_are_printed_once_to_stderr(world, capsys):
    appstate._notices.append("Your settings file (state.json) could not be read.")

    _code, out, err = run(capsys, "list")
    assert "note: Your settings file (state.json) could not be read." in err
    assert "settings file" not in out
    assert "settings file" not in run(capsys, "list")[2]


def test_a_store_without_its_format_file_ends_in_one_answer_not_a_loop(tmp_path, capsys):
    from .store_helpers import new_vault

    root = new_vault(tmp_path)
    for damage in ("garbage", None):
        if damage is None:
            (root / "store" / "FORMAT").unlink()
        else:
            (root / "store" / "FORMAT").write_text(damage, encoding="utf-8")
        for command in (["store", "fsck"], ["store", "upgrade", "--yes"]):
            code, _out, err = run(capsys, "--vault", str(root), *command)
            assert code == 1 and "FORMAT file" in err and "Tools >" not in err, (damage, command)


def test_menu_names_in_core_texts_become_commands():
    from nms_save_vault import cli_live

    text = cli_live.cli_words("Upgrade the vault first (Tools > Vault upgrade...).")

    assert text == "Upgrade the vault first ('nmsvault-cli store upgrade')."
