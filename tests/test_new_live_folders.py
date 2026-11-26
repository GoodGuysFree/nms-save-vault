"""New live folders at startup (GGF 2026-11-26: "detect new live folders at startup, and offer to
add to the vault (which makes one snapshot automatically)"): a folder on disk the settings do not
list is offered once; yes adds it and backs it up, no is remembered and not asked again."""
from __future__ import annotations

import pytest

from nms_save_vault.core import discover
from nms_save_vault.core import state as appstate

pytest.importorskip("tkinter")


@pytest.fixture
def app(gui_app, monkeypatch, tmp_path):
    """The shared App with one GOG folder on disk it does not know; settings restored after."""
    from nms_save_vault import gui

    gog = tmp_path / "NMS" / "DefaultUser"
    gog.mkdir(parents=True)
    found = appstate.Source(id="steam-DefaultUser", platform="steam", account="", path=str(gog),
                            label="GOG")
    monkeypatch.setattr(discover, "new_live_sources", lambda st: [
        s for s in [found] if st.get(s.id) is None])
    monkeypatch.setattr(gui_app, "_save_state_quietly", lambda: None)
    sources, ui = list(gui_app.state.sources), dict(gui_app.state.ui)
    backed_up, asked = [], []
    monkeypatch.setattr(gui_app, "_back_up_new_folders", lambda folders: backed_up.append(folders))
    monkeypatch.setattr(gui_app, "refresh", lambda: None)
    gui_app.answer = True

    def ask(title, text, **_k):
        asked.append(text)
        return gui_app.answer
    monkeypatch.setattr(gui, "_askyesno", ask)
    yield gui_app, found, backed_up, asked
    gui_app.state.sources, gui_app.state.ui = sources, ui


def test_a_new_folder_is_offered_and_yes_adds_it_and_backs_it_up(app):
    gui_app, found, backed_up, asked = app
    gui_app._offer_new_live_folders()
    (text,) = asked
    assert "GOG" in text and "backup" in text
    assert gui_app.state.get(found.id) is not None
    assert [[str(p) for p in f] for f in backed_up] == [[found.path]]

    gui_app._offer_new_live_folders()                 # known now: not asked again
    assert len(asked) == 1


def test_no_is_remembered_and_rescan_still_adds_it(app):
    gui_app, found, backed_up, asked = app
    gui_app.answer = False
    gui_app._offer_new_live_folders()
    assert gui_app.state.get(found.id) is None and backed_up == []
    gui_app._offer_new_live_folders()                 # the next start: not asked again
    assert len(asked) == 1


def test_the_offer_comes_after_the_news_and_before_the_update_question(app, monkeypatch):
    gui_app = app[0]
    order = []
    monkeypatch.setattr(gui_app, "_show_whats_new", lambda: order.append("news"))
    monkeypatch.setattr(gui_app, "_offer_new_live_folders", lambda: order.append("folders"))
    monkeypatch.setattr(gui_app, "_startup_update_check", lambda: order.append("updates"))
    gui_app._startup_dialogs()
    assert order == ["news", "folders", "updates"]
