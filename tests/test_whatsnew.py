"""What's new: the release's summary pops up once, the first time a version starts (GGF
2026-11-21: "when a user runs a version for the first time (or after an update) - pop up the
summary of changes in that release")."""
from __future__ import annotations

import pytest

from nms_save_vault import __version__
from nms_save_vault.ui import whatsnew


def test_the_notes_are_for_this_version():
    """A release bumps the version and writes its notes together (RELEASING.md step 1): notes
    left from the last release would otherwise never show, or show under the wrong version."""
    assert whatsnew.NOTES_VERSION == __version__
    assert whatsnew.NOTES.strip()


def test_shown_the_first_time_a_version_starts(monkeypatch):
    monkeypatch.setattr(whatsnew, "NOTES_VERSION", "9.9.9")
    monkeypatch.setattr(whatsnew, "NOTES", "- something new")
    assert whatsnew.to_show({}, "9.9.9") == "- something new"                   # a new install
    assert whatsnew.to_show({whatsnew.SEEN_KEY: "9.9.8"}, "9.9.9") == "- something new"  # an update


def test_not_shown_again_once_seen(monkeypatch):
    monkeypatch.setattr(whatsnew, "NOTES_VERSION", "9.9.9")
    monkeypatch.setattr(whatsnew, "NOTES", "- something new")
    assert whatsnew.to_show({whatsnew.SEEN_KEY: "9.9.9"}, "9.9.9") is None


def test_not_shown_when_the_notes_are_another_versions(monkeypatch):
    monkeypatch.setattr(whatsnew, "NOTES_VERSION", "9.9.8")
    monkeypatch.setattr(whatsnew, "NOTES", "- something old")
    assert whatsnew.to_show({}, "9.9.9") is None


def test_not_shown_without_notes(monkeypatch):
    monkeypatch.setattr(whatsnew, "NOTES_VERSION", "9.9.9")
    monkeypatch.setattr(whatsnew, "NOTES", "  \n")
    assert whatsnew.to_show({}, "9.9.9") is None


# --- the GUI ---------------------------------------------------------------------------------


@pytest.fixture
def fresh_app(gui_app, monkeypatch):
    """The shared App as if this version had never started, state saving stubbed."""
    from nms_save_vault import gui

    saved = []
    monkeypatch.setattr(gui_app, "_save_state_quietly", lambda: saved.append(dict(gui_app.state.ui)))
    before = gui_app.state.ui.get(whatsnew.SEEN_KEY)
    gui_app.state.ui.pop(whatsnew.SEEN_KEY, None)
    shown = []
    monkeypatch.setattr(gui, "_showinfo", lambda title, text, **k: shown.append((title, text)))
    yield gui_app, shown, saved
    gui_app.state.ui[whatsnew.SEEN_KEY] = before


def test_the_app_shows_the_notes_once_and_records_it(fresh_app):
    app, shown, saved = fresh_app
    app._show_whats_new()
    assert shown == [(f"What's new in v{__version__}", whatsnew.NOTES.strip())]
    assert app.state.ui[whatsnew.SEEN_KEY] == __version__
    assert saved and saved[-1][whatsnew.SEEN_KEY] == __version__

    app._show_whats_new()                          # the next start of the same version
    assert len(shown) == 1


def test_the_notes_come_before_the_update_question(fresh_app, monkeypatch):
    """Both are startup dialogs; one at a time, the release's news first."""
    app, shown, _saved = fresh_app
    order = []
    monkeypatch.setattr(app, "_show_whats_new", lambda: order.append("whats_new"))
    monkeypatch.setattr(app, "_startup_update_check", lambda: order.append("update_check"))
    app._startup_dialogs()
    assert order == ["whats_new", "update_check"]
