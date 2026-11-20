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
from .test_theme_dark_audit import mode  # noqa: F401  (a fixture)


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


# --- the reminder: Settings and the status bar ---------------------------------------------------

GB = 1 << 30


@pytest.fixture
def settings(app, monkeypatch):
    from nms_save_vault.core import state as appstate

    saved = []
    monkeypatch.setattr(appstate, "save", lambda st, path=None: saved.append(st.thin_reminder_bytes))
    before = app.state.thin_reminder_bytes
    app.saved = saved
    yield app
    app.state.thin_reminder_bytes = before
    win = app.__dict__.get("settings_window")
    if win is not None and win.winfo_exists():
        win.destroy()


def test_settings_keeps_the_reminder_size(settings):
    app = settings
    app.state.thin_reminder_bytes = 0
    menu, index = _menu_entry(app, "Tools", "Settings...")
    menu.invoke(index)
    win = app.settings_window
    assert win.size_var.get() == ""                               # empty: off
    win.size_var.set("1.5")
    win.save()
    assert app.state.thin_reminder_bytes == 3 * GB // 2 and app.saved == [3 * GB // 2]
    assert not win.winfo_exists()
    win = app.on_settings()
    assert win.size_var.get() == "1.5"
    win.size_var.set("lots")
    win.save()
    assert win.winfo_exists() and "number of GB" in win.error.cget("text")
    assert app.state.thin_reminder_bytes == 3 * GB // 2
    for text in ("0", " "):
        win.size_var.set(text)
        win.save()
        assert app.state.thin_reminder_bytes == 0
        win = app.on_settings()
    win.size_var.set("7")
    win._escape_handler()
    assert not win.winfo_exists() and app.state.thin_reminder_bytes == 0


def test_the_settings_window_is_placed_and_focused(settings):
    import tkinter as tk

    main = tk.Toplevel(settings)
    main.geometry("900x600")
    try:
        settings.update()
        win = settings.on_settings(master=main)
        settings.update()
        assert str(win.tk.call("focus", "-lastfor", win)) == str(win.entry)
        assert str(win.transient()) == str(main)
        win.destroy()
    finally:
        main.destroy()


def _status_after_listing(app) -> str:
    _listed(app)
    return app.status.get()


def test_the_status_bar_says_when_the_vault_passes_the_size(settings, world, monkeypatch):
    app = settings
    asked = []
    real = thinning.vault_size
    monkeypatch.setattr(thinning, "vault_size", lambda root: asked.append(root) or real(root))
    app.state.thin_reminder_bytes = 0
    assert "Thin the vault..." not in _status_after_listing(app) and asked == []   # off: not read
    app.state.thin_reminder_bytes = 1
    before = entries(world)
    text = _status_after_listing(app)
    assert "past the" in text and "Thin the vault..." in text and asked == [world.root]
    assert entries(world) == before                                # a reminder deletes nothing
    app.state.thin_reminder_bytes = 100 * GB
    assert "Thin the vault..." not in _status_after_listing(app)

def test_the_size_words():
    from nms_save_vault.ui import settings as S

    assert S.parse_gb("2") == 2 * GB and S.parse_gb("0,5") == GB // 2
    assert S.parse_gb("1.5 GB") == 3 * GB // 2 and S.parse_gb("") == 0 == S.parse_gb("0")
    assert S.parse_gb("-1") is None and S.parse_gb("nan") is None and S.parse_gb("x") is None
    assert S.gb_text(0) == "" and S.gb_text(2 * GB) == "2" and S.gb_text(GB // 4) == "0.25"


def test_the_settings_window_follows_the_theme(settings, mode):
    from .test_theme_dark_audit import audit

    win = settings.on_settings()
    settings.update()
    assert audit(win, mode) == []