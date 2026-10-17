"""The GUI's texts and layout rules, as plain functions: no Tk root is created here.

What the dialogs say is what the user decides on, so it is pinned without opening one.
"""
from __future__ import annotations

from nms_save_vault import gui
from nms_save_vault.core import operations as ops


# --- P10: the toolbar wraps instead of running off the window ----------------------------------


def test_buttons_wrap_onto_a_new_row_when_the_next_one_would_not_fit():
    assert gui._flow_rows([100] * 14, 1000) == [0] * 10 + [1] * 4


def test_a_row_takes_exactly_what_fits_and_an_oversized_button_gets_its_own():
    assert gui._flow_rows([500, 500, 1], 1000) == [0, 0, 1]
    assert gui._flow_rows([1200, 10], 1000) == [0, 1]


# --- clear slot: identical, unverifiable, not checked -------------------------------------------


def _plan(**kw) -> ops.ClearPlan:
    return ops.ClearPlan(slot=4, occupied=True, live_name="Run", live_play_time=7200, **kw)


def test_the_clear_question_says_when_the_vault_holds_this_very_save():
    text = gui._clear_question(_plan(entry_id="full-1", backup_play_time=7200, identical=True), "st_1")
    assert "Best copy in the vault: full-1" in text and "identical copy" in text
    assert "WARNING" not in text


def test_the_clear_question_says_when_a_copy_cannot_be_verified():
    text = gui._clear_question(_plan(unverifiable=True), "st_1")
    assert "CANNOT VERIFY: " in text


def test_the_clear_question_lists_what_could_not_be_checked():
    notes = [f"entry-{i} could not be checked (drive unplugged)." for i in range(7)]
    text = gui._clear_question(_plan(notes=notes), "st_1")
    assert "Not checked" in text and notes[0] in text and notes[4] in text
    assert notes[5] not in text and "and 2 more" in text


# --- undo: the folder, the change, damaged history ---------------------------------------------


def _undo_plan(**kw) -> ops.UndoPlan:
    return ops.UndoPlan(op="repopulate_slot", ts="2026-10-17T10:00:00", snapshot_id="snapshot-1",
                        detail="backup slot 3 -> slot 5", **kw)


def test_the_undo_question_names_the_folder_the_change_and_the_snapshot():
    text = gui._undo_question(_undo_plan(), "C:/saves/st_1")
    assert "Undo the last change to st_1?" in text
    assert "a copy into a slot (backup slot 3 -> slot 5), at 2026-10-17T10:00:00" in text
    assert "snapshot-1" in text and "account data" not in text


def test_undoing_a_restore_with_account_data_says_it_comes_back_too():
    assert "account data" in gui._undo_question(_undo_plan(with_accountdata=True), "st_1")


def test_the_damaged_history_question_shows_what_is_damaged():
    text = gui._undo_damage_question(_undo_plan(damaged_after=True, log_warning="2 damaged lines (12, 14)."))
    assert "2 damaged lines (12, 14)." in text and text.endswith("Undo anyway?")


# --- copy (P6) and warnings ---------------------------------------------------------------------


def test_the_copy_rule_says_a_single_save_can_be_refused():
    assert "only one save is refused" in gui.COPY_RULE.replace("\n", " ")
    assert "Both saves of the slot are copied" in gui.COPY_RULE


def test_warnings_are_listed_under_the_message():
    assert gui._with_warnings("Done.", []) == "Done."
    assert gui._with_warnings("Done.", ["not in the history"]) == "Done.\n\nWarnings:\n - not in the history"
