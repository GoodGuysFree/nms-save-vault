"""The two-pane layout: backup sorting, tooltips, and single-selection across panes.

These drive the real widgets against a synthetic vault + save folder, with the window
withdrawn so nothing appears on screen.
"""
from __future__ import annotations

import pytest

pytest.importorskip("tkinter")


@pytest.fixture
def app(gui_app):
    """The one App shared by the whole session (see conftest.gui_app)."""
    return gui_app


@pytest.fixture(autouse=True)
def _reset_app(app):
    """Undo whatever the previous test did, since the App is shared across the module."""
    app._sort_key, app._sort_reverse = "saved", True
    app._apply_backup_sort()
    for tree in (app.live_tree, app.backup_tree):
        if tree.selection():
            tree.selection_remove(*tree.selection())
    app._active_tree = app.live_tree
    app.update()


def _top_rows(app):
    return [
        (app.backup_tree.item(r, "text"), app.backup_tree.item(r, "values"))
        for r in app.backup_tree.get_children("")
    ]


# --- panes -------------------------------------------------------------------


def test_live_and_backups_are_separate_trees(app):
    assert app.live_tree is not app.backup_tree
    # Backups must not appear in the live pane, which is what the old single tree did.
    assert app.backup_tree.get_children("")
    assert not app.live_tree.get_children("")  # no live sources in this fixture


def test_backup_children_carry_slots_and_saves(app):
    first = app.backup_tree.get_children("")[0]
    slots = app.backup_tree.get_children(first)
    assert slots
    saves = app.backup_tree.get_children(slots[0])
    assert [app.backup_tree.item(s, "text").strip().rstrip(" *") for s in saves] == [
        "Auto-Save",
        "Restore-Point",
    ]


# --- sorting -----------------------------------------------------------------


def test_backups_start_newest_first(app):
    assert [t for t, _v in _top_rows(app)] == [
        "extract-steam-20260301-000000",  # March
        "inplace-20260201-000000",        # February
        "full-steam-20260101-000000",     # January
    ]


def test_sorting_by_date_is_chronological_not_alphabetical(app):
    """The Saved column shows a formatted date; sorting must use the real timestamp."""
    app._sort_backups("saved")  # flip to oldest-first
    assert [t for t, _v in _top_rows(app)] == [
        "full-steam-20260101-000000",
        "inplace-20260201-000000",
        "extract-steam-20260301-000000",
    ]


def test_sorting_by_type_then_reversing(app):
    app._sort_backups("type")
    assert [v[0] for _t, v in _top_rows(app)] == ["extract", "full", "inplace"]
    app._sort_backups("type")
    assert [v[0] for _t, v in _top_rows(app)] == ["inplace", "full", "extract"]


def test_sorting_by_slot_count_is_numeric(app):
    """3 slots must not sort below 10; the column value is a number, not text."""
    app._sort_backups("slots")
    assert [int(v[1]) for _t, v in _top_rows(app)] == [1, 2, 3]


def test_sorting_by_name_is_case_insensitive(app):
    app._sort_backups("name")
    assert [v[2] for _t, v in _top_rows(app)] == ["alpha", "Bravo", "Charlie"]


def test_sorted_heading_shows_a_direction_arrow(app):
    app._sort_backups("type")
    assert app.backup_tree.heading("type", "text").endswith("▲")
    app._sort_backups("type")
    assert app.backup_tree.heading("type", "text").endswith("▼")
    # Only the active column is marked.
    assert app.backup_tree.heading("name", "text") == "Name / Label"


def test_sorting_keeps_every_backup(app):
    before = {t for t, _v in _top_rows(app)}
    for key in ("type", "name", "slots", "saved", "type"):
        app._sort_backups(key)
    assert {t for t, _v in _top_rows(app)} == before


# --- tooltips ----------------------------------------------------------------


def test_every_backup_row_has_a_tooltip(app):
    def walk(node=""):
        for child in app.backup_tree.get_children(node):
            yield child
            yield from walk(child)

    for row in walk():
        assert app._tip_for_row(app.backup_tree, row), f"no tooltip on {row}"


def test_backup_tooltip_explains_the_entry(app):
    row = app.backup_tree.get_children("")[0]
    tip = app._tip_for_row(app.backup_tree, row)
    assert "extract — a single slot lifted aside" in tip
    assert "2026-03-01" in tip


def test_save_tooltip_explains_what_the_save_type_means(app):
    entry = app.backup_tree.get_children("")[0]
    slot = app.backup_tree.get_children(entry)[0]
    auto, restore = app.backup_tree.get_children(slot)
    assert "every few minutes" in app._tip_for_row(app.backup_tree, auto)
    assert "leave your ship" in app._tip_for_row(app.backup_tree, restore)


def test_tooltip_for_an_unknown_row_is_empty_not_an_error(app):
    assert app._tip_for_row(app.backup_tree, "nope") == ""


# --- selection ---------------------------------------------------------------


def test_selecting_in_one_pane_clears_the_other(app):
    """Otherwise a toolbar button could act on a stale row in the pane you are not using."""
    row = app.backup_tree.get_children("")[0]
    app.backup_tree.selection_set(row)
    app.update()
    assert app._selected() is not None
    assert app._active_tree is app.backup_tree
    assert not app.live_tree.selection()


# --- restoring: an extract is not a full backup ------------------------------


def test_restore_label_for_an_extract_names_the_slot(app):
    """Right-clicking an extract used to offer a plain "Restore ... into live", which ran
    a mirroring full restore and deleted every other save."""
    from nms_save_vault import gui

    extract = next(e for e in app.vault.entries if e.kind == "extract")
    assert gui._restore_menu_label(extract) == "Put slot 1 back into live slot 1"


def test_restore_label_for_a_backup_warns_that_it_replaces(app):
    from nms_save_vault import gui

    full = next(e for e in app.vault.entries if e.kind == "full")
    label = gui._restore_menu_label(full)
    assert "Restore all" in label and "replaces other slots" in label


def test_only_single_slot_extracts_are_treated_as_slot_restores(app):
    from nms_save_vault import gui
    from nms_save_vault.core.catalog import SlotSummary

    extract = next(e for e in app.vault.entries if e.kind == "extract")
    assert gui._extract_slot_number(extract) == 1
    extract.slots.append(SlotSummary(slot=7, occupied=True, name="x", newest_label="A", members=[]))
    assert gui._extract_slot_number(extract) is None  # ambiguous: never guess a slot
    extract.slots.pop()
