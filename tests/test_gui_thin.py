"""The main window's side of thinning (docs/thinning.md "The apps"): Thin the vault... by the
Backups list and in Tools, the refresh after a run, the pins of the Backups list with their
mark, the reminder in the status bar, and the start-up finish of an interrupted run. Made-up
vaults in tmp_path only; no real dialog is shown."""
from __future__ import annotations

import time

import pytest

from nms_save_vault import gui
from nms_save_vault.core import safety
from nms_save_vault.core import store as vstore
from nms_save_vault.core.catalog import Vault
from nms_save_vault.core.store import thinning, versionstore
from nms_save_vault.ui import thinning as TW

from .test_cli_thin import thin_world


def _wait(app, condition, timeout: float = 30.0) -> None:
    end = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < end, "timed out"
        app.update()
        time.sleep(0.005)


def _menu_entry(app, menu_title: str, label: str):
    bar = app.menubar
    for i in range(bar.index("end") + 1):
        if bar.type(i) == "cascade" and bar.entrycget(i, "label") == menu_title:
            menu = app.nametowidget(bar.entrycget(i, "menu"))
            for j in range(menu.index("end") + 1):
                if menu.type(j) != "separator" and menu.entrycget(j, "label") == label:
                    return menu, j
    raise AssertionError(label)


def _buttons(widget):
    for child in widget.winfo_children():
        yield child
        yield from _buttons(child)


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setattr(safety, "is_game_running", lambda: False)
    monkeypatch.setattr(versionstore.discover, "all_live_paths", lambda *a, **k: [])
    return thin_world(tmp_path)


@pytest.fixture
def app(gui_app, world, monkeypatch):
    changed = []
    monkeypatch.setattr(gui_app, "vault", Vault(world.root))
    monkeypatch.setattr(gui_app, "vault_changed", lambda *a, **k: changed.append(1))
    gui_app.changed = changed
    yield gui_app
    win = gui_app.__dict__.get("thin_window")
    if win is not None and win.winfo_exists():
        win.busy = False
        win.destroy()
    gui_app.thin_window = None
    gui_app.update()


def entries(world) -> set[str]:
    return {e.id for e in vstore.open_store(world.root).entries()}


# --- Thin the vault... ---------------------------------------------------------------------------


def test_the_button_by_the_backups_list_opens_the_list_and_a_run_refreshes(app, world):
    button = app.thin_button
    assert button.cget("text") == "Thin the vault..."
    pane = app.backup_tree.master.master                      # the BACKUPS pane
    assert button in list(_buttons(pane))
    button.invoke()
    win = app.thin_window
    assert isinstance(win, TW.ThinWindow) and win.vault_root == world.root
    _wait(app, lambda: win.ready)
    assert set(win.tree.get_children("")) == {c.id for c in world.planned.candidates}
    assert app.changed == []                                  # looking changes nothing
    win.delete()
    _wait(app, lambda: win.ready)
    assert not {c.id for c in world.planned.candidates} & entries(world)
    assert app.changed == [1]                                 # Backups list and status bar


def test_tools_has_it_too(app, world):
    menu, index = _menu_entry(app, "Tools", "Thin the vault...")
    menu.invoke(index)
    assert isinstance(app.thin_window, TW.ThinWindow)
    _wait(app, lambda: app.thin_window.ready)


# --- pins in the Backups list --------------------------------------------------------------------


def _listed(app) -> None:
    app.refresh()
    gen = app._refresh_gen
    _wait(app, lambda: app._entries is not None and not app.worker.busy
          and gen == app._refresh_gen)


def _row_of(app, entry_id: str) -> str:
    tree = app.backup_tree
    return next(r for r in tree.get_children("")
                if app._meta_for(tree, r)["entry"].id == entry_id)


def _menu_labels(app, row: str, monkeypatch) -> dict:
    posted = []
    monkeypatch.setattr(app, "_post_menu", lambda menu, x, y: posted.append(menu))
    app._context_menu(app.backup_tree, row, 0, 0)
    menu = posted[-1]
    return {menu.entrycget(i, "label"): i for i in range(menu.index("end") + 1)
            if menu.type(i) == "command"}, menu


def test_a_backup_is_pinned_and_unpinned_from_its_menu_and_shows_the_mark(app, world, monkeypatch):
    target = world.old.id
    _listed(app)
    row = _row_of(app, target)
    assert app.backup_tree.item(row, "text") == target
    assert app.backup_tree.item(_row_of(app, world.pinned.id), "text") == (
        f"{world.pinned.id}{gui.PIN_MARK}")
    labels, menu = _menu_labels(app, row, monkeypatch)
    assert gui.UNPIN_LABEL not in labels
    menu.invoke(labels[gui.PIN_LABEL])
    _wait(app, lambda: not app.worker.busy and target in thinning.pinned_entries(world.root))
    _listed(app)
    row = _row_of(app, target)
    assert app.backup_tree.item(row, "text") == f"{target}{gui.PIN_MARK}"
    labels, menu = _menu_labels(app, row, monkeypatch)
    assert gui.PIN_LABEL not in labels
    menu.invoke(labels[gui.UNPIN_LABEL])
    _wait(app, lambda: not app.worker.busy
          and target not in thinning.pinned_entries(world.root))
    _listed(app)
    assert app.backup_tree.item(_row_of(app, target), "text") == target


def test_a_pin_is_refused_while_the_vault_is_read_only_and_says_so(app, world, dialogs,
                                                                   monkeypatch):
    import json

    fmt = world.root / "store" / "FORMAT"
    body = json.loads(fmt.read_text("ascii"))
    body["write_compat"] = versionstore.STORE_VERSION + 1
    fmt.write_text(json.dumps(body, indent=1, sort_keys=True) + "\n", encoding="ascii")
    dialogs.expect()
    app.pin_entry(world.old.id, True)
    _wait(app, lambda: not app.worker.busy and dialogs.shown)
    kind, title, message = dialogs.take()[-1]
    assert kind == "showerror" and title == "Pin" and "not changed" in message
    assert "newer NMS Save Vault" in message
    assert world.old.id not in thinning.pinned_entries(world.root)


def test_a_vault_that_could_not_be_opened_refuses_pins_at_once(app, world, dialogs, monkeypatch):
    monkeypatch.setattr(app, "vault", Vault(world.root, read_only=True))
    dialogs.expect()
    app.pin_entry(world.old.id, True)
    kind, title, message = dialogs.take()[-1]
    assert kind == "showinfo" and "read-only" in message and not app.worker.busy
    assert world.old.id not in thinning.pinned_entries(world.root)
