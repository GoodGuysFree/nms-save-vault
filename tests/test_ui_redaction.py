"""The redaction scan (D6 3.13, U13): with display names set for a (fake) Steam and Xbox
account, every view of the Organizer tab - table rows and versions, the live lists, the
detail pages of a save file, a version and an empty slot, the alert bars, the notes window
and every window title - is rendered from data that carries those ids, and no widget text,
canvas text or title may show them. Edit fields keep the owner's own text and are exempt."""
from __future__ import annotations

import time
import tkinter as tk
from tkinter import ttk

import pytest

from nms_save_vault.core import aliases, operations, safety
from nms_save_vault.ui.organizer import view as V
from nms_save_vault.ui.redact import shown

from .conftest import steam_save
from .organizer_world import PIDS, STEAM_ACCOUNT, build_world

STEAM_ID = STEAM_ACCOUNT.removeprefix("st_")


@pytest.fixture
def names(monkeypatch):
    names = aliases.AliasMap()
    monkeypatch.setattr(aliases, "_active", names)
    return names


@pytest.fixture
def world(tmp_path, make_wgs_account, monkeypatch, names):
    monkeypatch.setattr(safety, "is_game_running", lambda: False)
    world = build_world(tmp_path, make_wgs_account)
    xbox_id = world.xbox.name
    names.set(STEAM_ID, "Main PC")
    names.set(xbox_id, "Couch Xbox")
    world.ids = (STEAM_ID, xbox_id)
    # The ids reach the screen through the owner's own words and folder names.
    for source in world.sources:
        source.label = f"{source.platform.title()} ({source.account})"
    notes = world.organizer.notes
    notes.set_save_note(PIDS["main"], f"Main run. Copied from {STEAM_ACCOUNT}.", base_rev=1)
    head = world.model.versions(PIDS["main"])[0]
    notes.set_version_note(head.head, pair=head.pair, pid=head.pid,
                           label=f"from {xbox_id} {STEAM_ID}")
    copy = tmp_path / f"copy of {STEAM_ACCOUNT}"
    steam_save(copy, 7, 0, f"Run {STEAM_ACCOUNT}", 30, 0x7777, 1_789_000_000, extra="id")
    operations.import_backup(world.vault, copy, label=f"hand copy {STEAM_ACCOUNT}",
                             warnings=[])
    return world


def _wait(app, condition, timeout: float = 15.0) -> None:
    end = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < end, "timed out"
        app.update()
        time.sleep(0.005)


def _texts(root, exempt) -> list[str]:
    """Every string the widgets under ``root`` show."""
    out, stack = [], [root]
    while stack:
        w = stack.pop()
        stack.extend(w.winfo_children())
        if isinstance(w, (tk.Tk, tk.Toplevel)):
            out.append(w.title())
        try:
            out.append(str(w.cget("text")))
        except tk.TclError:
            pass
        if isinstance(w, ttk.Treeview):
            for column in ("#0", *w["columns"]):
                out.append(str(w.heading(column, "text")))
            items = list(w.get_children(""))
            while items:
                item = items.pop()
                items.extend(w.get_children(item))
                out.append(str(w.item(item, "text")))
                out.extend(str(v) for v in w.item(item, "values"))
        if isinstance(w, tk.Canvas):
            out += [w.itemcget(i, "text") for i in w.find_all() if w.type(i) == "text"]
        if isinstance(w, tk.Text) and w not in exempt:
            out.append(w.get("1.0", "end"))
        if isinstance(w, ttk.Combobox):
            out += [str(v) for v in w.cget("values")] + [w.get()]
    return out


def _leaks(texts, ids) -> list[str]:
    digits = [i for i in ids] + [i.split("_")[0] for i in ids]
    return [t for t in texts if any(d in t for d in digits)]


def test_no_view_shows_an_account_id(gui_app, world):
    top = tk.Toplevel(gui_app)
    top.withdraw()
    view = V.OrganizerView(top, worker=gui_app.worker, themed=gui_app.themed, prefs={},
                           save_prefs=lambda: None, make_organizer=lambda: world.organizer)
    view.pack(fill=tk.BOTH, expand=True)
    try:
        view.rescan()
        _wait(gui_app, lambda: not view.scanning)
        model = view.model
        assert any(STEAM_ACCOUNT in r.name or "Main PC" in r.name
                   for line in model.lines() for r in model.versions(line.pid))
        seen = []
        exempt = set()
        # Every save file open, then a line, a version and an empty slot in the detail pane.
        for line in model.lines():
            view.table.set_open(line.pid, True)
        for line in model.lines():
            view.table.select(line.pid)
            gui_app.update()
            seen += _texts(top, exempt)
            for row in model.versions(line.pid):
                view._link(line.pid, row.head)
                gui_app.update()
                seen += _texts(top, exempt)
                for page in ("summary", "where", "notes"):
                    seen.append(view.detail.text_of(page))
        empty = next(t for t, d in view.live.drawn.items() if d.slot.state == "empty")
        view.live.select_slot(empty)
        view.live.set_mode("one")
        view.alert("test", f"A Steam live folder could not be read: {STEAM_ACCOUNT} is missing")
        seen += _texts(top, exempt)
        # Hover texts, built on hover and shown through the filter.
        for iid in view.table.tree.get_children(""):
            seen.append(shown(view.table.tree, view.table._tip(view.table.tree, iid)))
        for token in view.live.drawn:
            seen.append(shown(view.live.canvas, view.live._tip(token)))
        # The notes window, its lists, titles and the conflict view.
        view.open_notes_for(PIDS["main"], model.versions(PIDS["main"])[0].head)
        notes = view.notes_window
        notes.withdraw()
        exempt |= {notes.editor, notes.comment}
        notes._note_failed(type("Conflict", (Exception,), {})())        # an ordinary error
        seen += _texts(notes, exempt)
        assert any("Main PC" in t for t in seen)        # the names are shown instead
        leaks = _leaks(seen, world.ids)
        assert leaks == [], leaks[:5]
    finally:
        if view.notes_window is not None and view.notes_window.winfo_exists():
            view.notes_window.destroy()
        top.destroy()


def test_the_scan_itself_catches_a_leak(gui_app, names):
    """Guard the guard: an unfiltered widget would be found."""
    names.set(STEAM_ID, "Main PC")
    top = tk.Toplevel(gui_app)
    top.withdraw()
    try:
        tk.Label(top, text=f"raw {STEAM_ACCOUNT}")       # deliberately not filtered
        assert _leaks(_texts(top, set()), (STEAM_ID,))
    finally:
        top.destroy()
