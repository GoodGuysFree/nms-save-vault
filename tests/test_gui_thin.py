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
from nms_save_vault.core.store import versionstore
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
