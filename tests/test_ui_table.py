"""The organizer table (D6 U7): one row per save file with a > / v marker, other lines
muted while one is open, chips (Vault only among them), Find, sort, column chooser, and
the preferences kept in ``ui``."""
from __future__ import annotations

import tkinter as tk

import pytest

from nms_save_vault.core import safety
from nms_save_vault.ui.organizer import table as T

from .organizer_world import PIDS, build_world


@pytest.fixture
def world(tmp_path, make_wgs_account, monkeypatch):
    monkeypatch.setattr(safety, "is_game_running", lambda: False)
    return build_world(tmp_path, make_wgs_account)


@pytest.fixture
def host(gui_app):
    top = tk.Toplevel(gui_app)
    top.withdraw()
    yield top
    top.destroy()


@pytest.fixture
def table(host, gui_app, world):
    prefs, saves = {}, []
    widget = T.OrganizerTable(host, themed=gui_app.themed, prefs=prefs,
                              save_prefs=lambda: saves.append(dict(prefs)))
    widget.pack(fill=tk.BOTH, expand=True)
    widget.set_model(world.model)
    widget.saves = saves
    return widget


def _names(table) -> list[str]:
    return [table.tree.item(iid, "text") for iid in table.tree.get_children("")]


def _image(value) -> str:
    """Tk hands an image option back as a name or a one-item tuple."""
    if isinstance(value, (tuple, list)):
        return str(value[0]) if value else ""
    return str(value)


def _line(world, key):
    return world.model.line(PIDS[key])


# --- what the rows say (pure) ------------------------------------------------------------


def test_the_world_has_the_lines_the_tests_need(world):
    assert {line.name for line in world.model.lines()} == {
        "Main Save", "Other Run", "Cross Save", "Old Run", "Xbox Only"}


def test_line_values(world):
    main = T.line_values(_line(world, "main"))
    assert main["live"] == "S9" and main["play"] == "2h10" and main["versions"] == "2"
    assert main["state"] == "NOT IN VAULT; pinned"           # played on since the backup
    assert main["note"] == "Main run."
    other = T.line_values(_line(world, "other"))
    assert other["state"].startswith("NOT IN VAULT")
    cross = T.line_values(_line(world, "cross"))
    assert cross["live"] == "S5 X1" and "Xbox behind 2h00" in cross["state"]
    old = T.line_values(_line(world, "old"))
    assert old["live"] == "vault" and "vault only" in old["state"]


def test_a_nameless_save_says_what_it_was(world):
    import dataclasses

    line = _line(world, "main")
    assert T.line_name(dataclasses.replace(line, name="", former_names=("Swarm",))) == \
        "(unnamed, was Swarm)"
    assert T.line_name(dataclasses.replace(line, name="", former_names=())) == "(unnamed 9C72)"


def test_chip_counts_come_from_every_line(world):
    counts = T.chip_counts(world.model.lines())
    assert counts["all"] == 5 and counts["live"] == 4 and counts["vault"] == 1
    assert counts["steam"] == 4 and counts["xbox"] == 2 and counts["both"] == 1
    assert counts["note"] == 1 and counts["pinned"] == 1 and counts["risk"] == 4


def test_chips_or_within_a_group_and_across_groups(world):
    lines = world.model.lines()
    names = lambda chips, text="": {l.name for l in T.visible_lines(
        lines, chips, text, model=world.model)}
    assert names({"vault"}) == {"Old Run"}
    assert names({"live", "vault"}) == names(set())                 # OR inside a group
    assert names({"xbox"}) == {"Cross Save", "Xbox Only"}
    assert names({"vault", "risk"}) == set()                        # AND across groups
    assert names({"steam", "risk"}) == {"Main Save", "Other Run", "Cross Save"}
    assert names(set(), "main") == {"Main Save"}
    assert names(set(), "ATLAS") == {"Main Save"}           # the whole note is searched too
    assert {l.name for l in T.visible_lines(lines, (), "ATLAS")} == set()   # preview only


def test_sorting_follows_the_read_model(world):
    lines = world.model.lines()
    by_play = [l.name for l in T.visible_lines(lines, (), "", "play_time", True)]
    assert by_play[0] == "Cross Save"
    by_name = [l.name for l in T.visible_lines(lines, (), "", "name", False)]
    assert by_name == sorted(by_name, key=str.casefold)


# --- the widget ---------------------------------------------------------------------------


def test_one_row_per_line_with_a_closed_marker(table):
    assert len(table.tree.get_children("")) == 5
    for iid in table.tree.get_children(""):
        assert _image(table.tree.item(iid, "image")) == str(table._images["closed"])
        assert table.tree.get_children(iid)                    # openable


def test_opening_a_line_shows_its_versions_and_mutes_the_others(table, world):
    pid = PIDS["main"]
    table.set_open(pid, True)
    iid = table._iid_of[pid]
    assert _image(table.tree.item(iid, "image")) == str(table._images["open"])
    versions = table.tree.get_children(iid)
    assert [table.tree.item(v, "text") for v in versions] == ["#2", "#1"]
    for other in table.tree.get_children(""):
        dim = "dim" in table.tree.item(other, "tags")
        assert dim == (other != iid)
    table.set_open(pid, False)
    assert _image(table.tree.item(iid, "image")) == str(table._images["closed"])
    assert not any("dim" in table.tree.item(o, "tags") for o in table.tree.get_children(""))


def test_keyboard_and_double_click_open_through_the_tree_events(table):
    pid = PIDS["cross"]
    iid = table._iid_of[pid]
    table.tree.focus(iid)
    table.tree.item(iid, open=True)
    table.tree.event_generate("<<TreeviewOpen>>")
    assert pid in table.open_pids and "dim" not in table.tree.item(iid, "tags")
    table.tree.item(iid, open=False)
    table.tree.event_generate("<<TreeviewClose>>")
    assert pid not in table.open_pids


def test_chips_filter_and_are_remembered(table):
    table.chip_buttons["vault"].invoke()
    assert _names(table) == ["Old Run"]
    assert table.prefs["table_chips"] == ["vault"] and not table.all_var.get()
    assert table.chip_buttons["vault"].cget("text") == "Vault only 1"
    table.chip_buttons["all"].invoke()
    assert len(_names(table)) == 5 and table.all_var.get()


def test_find_waits_for_typing_to_pause(table, gui_app):
    import time

    table.find_var.set("other")
    assert len(_names(table)) == 5                        # not yet: debounced
    end = time.monotonic() + 5
    while len(_names(table)) != 1:
        assert time.monotonic() < end
        gui_app.update()
        time.sleep(0.01)
    assert _names(table) == ["Other Run"]


def test_a_heading_sorts_and_again_reverses_with_an_arrow(table):
    table.sort_by("play")
    assert _names(table)[0] == "Cross Save" and table.descending
    assert _image(table.tree.heading("play", "image")) == str(table._images["down"])
    assert _image(table.tree.heading("#0", "image")) == ""
    table.sort_by("play")
    assert _names(table)[-1] == "Cross Save" and not table.descending
    assert table.prefs["table_sort"] == ["play_time", False]
    table.sort_by("branches")                              # not sortable: nothing happens
    assert table.sort_key == "play_time"


def test_a_new_model_keeps_what_is_open_and_selected(table, world):
    pid = PIDS["main"]
    table.set_open(pid, True)
    table.select(pid)
    before = table._iid_of[pid]
    world.organizer.notes.set_save_note(PIDS["other"], "Base on the moon.", base_rev=None)
    table.set_model(world.refresh())
    assert table._iid_of[pid] == before and table.tree.selection() == (before,)
    assert pid in table.open_pids and table.tree.get_children(before)
    other = table._iid_of[PIDS["other"]]
    assert table.tree.set(other, "note") == "Base on the moon."


def test_the_column_chooser_shows_hides_and_orders(table):
    chooser = T.ColumnChooser(table)
    try:
        chooser.on["platforms"].set(True)
        chooser.on["mode"].set(False)
        chooser.move("note", -1)
        chooser.ok()
    finally:
        if chooser.winfo_exists():
            chooser.destroy()
    shown = table.displayed_columns()
    assert "platforms" in shown and "mode" not in shown
    assert table.prefs["table_columns"] == shown
    table.set_columns(T.DEFAULT_COLUMNS)
    assert table.displayed_columns() == list(T.DEFAULT_COLUMNS)


def test_preferences_come_back(host, gui_app, world):
    prefs = {"table_sort": ["name", False], "table_chips": ["live"],
             "table_columns": ["live", "note"]}
    widget = T.OrganizerTable(host, themed=gui_app.themed, prefs=prefs, save_prefs=lambda: None)
    widget.set_model(world.model)
    assert widget.sort_key == "name" and widget.chips == {"live"}
    assert widget.displayed_columns() == ["live", "note"]
    assert _names(widget) == sorted(_names(widget), key=str.casefold)
    assert "Old Run" not in _names(widget)


def test_an_empty_vault_says_how_to_start(host, gui_app, tmp_path):
    from nms_save_vault.core.catalog import Vault
    from nms_save_vault.core.organizer import Organizer
    from nms_save_vault.core.organizer.cache import ScanCache

    vault = Vault(tmp_path / "empty")
    vault.ensure()
    model = Organizer(vault.root, [], read_only=True, cache=ScanCache(None, None),
                      game_running=lambda: False).refresh(record=False)
    widget = T.OrganizerTable(host, themed=gui_app.themed, prefs={}, save_prefs=lambda: None)
    widget.set_model(model)
    assert _names(widget) == [T.EMPTY_VAULT]


def test_selection_is_reported_as_line_or_version(table):
    seen = []
    table.on_select = lambda pid, head: seen.append((pid, head))
    pid = PIDS["main"]
    table.select(pid)
    table.update()
    assert seen[-1] == (pid, None)
    head = table.model.versions(pid)[0].head
    table.select(pid, head)
    table.update()
    assert seen[-1] == (pid, head)


def test_hover_text_is_built_for_the_row(table):
    iid = table._iid_of[PIDS["cross"]]
    tip = table._tip(table.tree, iid)
    assert "Cross Save" in tip and "S5, X1" in tip and "behind" in tip


def test_a_theme_switch_redraws_the_markers(table, gui_app):
    from nms_save_vault import theme

    before = table._images["closed"]
    original = gui_app.state.theme
    try:
        gui_app.state.theme = theme.DARK
        gui_app._apply_theme()
        assert table._images["closed"] is not before
        assert str(table.tree.tag_configure("dim", "foreground")) == theme.PALETTES[theme.DARK]["dim"]
    finally:
        gui_app.state.theme = original
        gui_app._apply_theme()
