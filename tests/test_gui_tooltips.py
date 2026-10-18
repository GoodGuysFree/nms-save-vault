"""The owner-approved tooltips for non-obvious words (hover text, or the status bar for
menus). No real dialogs: the conftest stub stays in force."""
from __future__ import annotations

import tkinter as tk
from types import SimpleNamespace

from nms_save_vault.ui.organizer import history as H
from nms_save_vault.ui.organizer import table, words
from nms_save_vault.ui.organizer.detail import _version_state

from .test_organizer_readmodel import PID
from .test_ui_history import forked, host  # noqa: F401  (fixtures)
from .test_ui_ops import (  # noqa: F401  (fixtures)
    _menu_labels, _slot, _open, _polestar, _wait, running, threads, view, world)


def _select(menu: tk.Menu, label: str) -> None:
    """Highlight an entry and fire what Tk fires when the pointer rests on it."""
    pos = next(i for i in range(menu.index("end") + 1)
               if menu.type(i) in ("command", "cascade") and menu.entrycget(i, "label") == label)
    menu.activate(pos)
    assert menu.bind("<<MenuSelect>>")       # the real binding; an unposted menu gets no event
    menu.hint_handler()


def test_texts_are_the_approved_ones():
    assert words.NOT_LINKED_TIP == (
        "No recorded move or backup links these versions, so they are listed by date. "
        "Running the organizer bootstrap links versions that backups saw.")
    assert words.PROMOTE_TIP == "Make the slot's other save the one the game loads."
    assert words.SLOT_MARKERS_TIP.startswith("* = the save the game loads.")


def test_not_linked_header_has_a_tip_in_the_table(gui_app):
    tbl = gui_app.organizer_view.table
    spec = table.RowSpec(("sep",), "Not linked, by date - 2 versions", (), ("sep",))
    tbl._values["Vtest"] = spec
    try:
        assert tbl.tooltip.text_for(tbl.tree, "Vtest") == words.NOT_LINKED_TIP
    finally:
        del tbl._values["Vtest"]


def test_not_linked_section_has_a_tip_in_the_history_window(host, gui_app, forked):  # noqa: F811
    w, _vids, _ = forked
    win = H.HistoryWindow(host, model=w.refresh(), themed=gui_app.themed, pid=PID)
    try:
        iid = next(i for i, r in win.rows.items() if r.key == ("sec", "dated"))
        assert win.dated_tip.text_for(win.tree, iid) == words.NOT_LINKED_TIP
        assert win.dated_tip.text_for(win.tree, "nope") == ""
    finally:
        win.destroy()


def _row(**kw):
    base = dict(name="A", saved_at=0, locations=[], parent=None, label="", durable_copies=0,
                flags=frozenset({"outside-only"}))
    base.update(kw)
    return SimpleNamespace(**base)


def test_only_outside_the_vault_has_a_tip_in_table_and_detail(gui_app):
    assert words.OUTSIDE_ONLY_TIP in table.version_tip(_row())
    assert words.OUTSIDE_ONLY_TIP not in table.version_tip(_row(flags=frozenset()))
    assert "only outside the vault" in _version_state(_row())
    text = gui_app.organizer_view.detail.summary_text
    assert text.nobackup_tip.text_for(text, "outside") == words.OUTSIDE_ONLY_TIP
    assert text.nobackup_tip.text_for(text, "tip") == words.NO_BACKUP_TIP


def test_the_pin_button_has_a_tip(gui_app):
    detail = gui_app.organizer_view.detail
    assert detail.pin_tip.text_for(detail.pin_button, "pin") == words.PIN_TIP


def test_classic_promote_button_has_a_tip_and_its_menu_entry_a_status_hint(gui_app):
    assert gui_app.promote_tip.text_for(None, "promote") == words.PROMOTE_TIP
    live = _submenu(gui_app, "Live")
    gui_app.status.set("")
    _select(live, "Promote")
    assert gui_app.status.get() == words.PROMOTE_TIP


def _submenu(app, label: str) -> tk.Menu:
    bar = app.menubar
    for i in range(bar.index("end") + 1):
        if bar.type(i) == "cascade" and bar.entrycget(i, "label") == label:
            return app.nametowidget(bar.entrycget(i, "menu"))
    raise AssertionError(label)


def test_vault_upgrade_menu_entry_has_a_status_hint(gui_app):
    gui_app.status.set("")
    _select(_submenu(gui_app, "Tools"), "Vault upgrade...")
    assert gui_app.status.get() == words.VAULT_UPGRADE_TIP


def test_organizer_promote_entries_put_the_explanation_in_the_status_bar(view):
    shown = []
    view.on_hint = shown.append
    menu = view.slot_menu(_slot(view, 1))
    _select(menu, "Make the restore point load")
    assert shown[-1] == words.PROMOTE_TIP
    _select(menu, "Keep a copy in the vault now")
    assert shown[-1] == ""


def test_classic_slot_and_save_rows_explain_the_markers(gui_app):
    sv = SimpleNamespace(slot=1, display_name="X", newest=None, members=[])
    assert words.SLOT_MARKERS_TIP in gui_app._slot_tip(sv, SimpleNamespace())
    assert "/ moved = the file was moved here from another slot." in words.SLOT_MARKERS_TIP


def test_help_has_a_markers_and_words_note():
    from nms_save_vault import gui

    assert "MARKERS AND WORDS" in gui.HELP_TEXT
    for needle in ("Not linked, by date", "Only outside the vault", "Pin", "moved"):
        assert needle in gui.HELP_TEXT
