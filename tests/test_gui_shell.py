"""Shell 10a: the menu bar, then the tabs "Live saves and backups" and "Organizer", then the
status bar; the app reopens the tab used last (D6 2.11, U5)."""
from __future__ import annotations

import pytest

from nms_save_vault import __version__, gui
from nms_save_vault.core import state as appstate

pytest.importorskip("tkinter")


@pytest.fixture
def app(gui_app, monkeypatch):
    saved = []
    monkeypatch.setattr(gui.appstate, "save", lambda st, path=None: saved.append(
        (dict(st.ui), path)))
    gui_app.saved = saved
    before = gui_app.current_view(), dict(gui_app.state.ui)
    yield gui_app
    gui_app.show_view(before[0])
    gui_app.update()
    gui_app.state.ui.clear()
    gui_app.state.ui.update(before[1])


def _tabs(app) -> list[str]:
    return [app.notebook.tab(tab, "text") for tab in app.notebook.tabs()]


def _inside(widget, ancestor) -> bool:
    while widget is not None:
        if widget == ancestor:
            return True
        widget = widget.master
    return False


def test_two_tabs_under_the_menu_bar(app):
    assert _tabs(app) == ["Live saves and backups", "Organizer"]
    assert not app.cget("menu")                           # the in-window bar, not a native one
    for widget in (app.toolbar, app.live_tree, app.backup_tree, app.active_combo):
        assert _inside(widget, app.classic_view)          # today's content, in its tab
    assert not _inside(app.banner, app.notebook)          # the update bar stays above the tabs


def test_switching_tabs_remembers_the_last_one(app):
    app.show_view(gui.ORGANIZER)
    app.update()
    assert app.current_view() == gui.ORGANIZER and app.view_var.get() == gui.ORGANIZER
    assert app.state.ui["last_view"] == gui.ORGANIZER
    assert app.saved[-1] == ({**app.state.ui}, app.state_path)
    assert "Organizer" in app.title() and __version__ in app.title()
    app.show_view(gui.CLASSIC)
    app.update()
    assert app.state.ui["last_view"] == gui.CLASSIC and "Live saves and backups" in app.title()


def test_opening_on_the_last_tab_saves_nothing(app):
    app.state.ui["last_view"] = gui.ORGANIZER
    app._open_last_view()
    app.update()
    assert app.current_view() == gui.ORGANIZER and app.saved == []
    app.state.ui["last_view"] = "somewhere else"            # hand-edited config
    app._open_last_view()
    app.update()
    assert app.current_view() == gui.CLASSIC and app.state.ui["last_view"] == gui.CLASSIC
    assert app.saved == []


def test_ctrl_1_and_ctrl_2_and_the_view_menu_switch_tabs(app):
    assert app.bind_all("<Control-Key-1>") and app.bind_all("<Control-Key-2>")
    view = app.nametowidget(app.menubar.entrycget("View", "menu"))
    labels = [view.entrycget(i, "label") for i in range(2)]
    assert labels == ["Organizer", "Live saves and backups"]
    view.invoke(0)
    app.update()
    assert app.current_view() == gui.ORGANIZER
    view.invoke(1)
    app.update()
    assert app.current_view() == gui.CLASSIC


def test_the_ui_preferences_round_trip_through_state(tmp_path):
    path = tmp_path / "state.json"
    appstate.save(appstate.AppState(ui={"last_view": "organizer", "live_mode": "two"}), path)
    assert appstate.load(path).ui == {"last_view": "organizer", "live_mode": "two"}
    path.write_text('{"version": 2, "sources": [], "ui": "garbage"}', encoding="utf-8")
    assert appstate.load(path).ui == {}
