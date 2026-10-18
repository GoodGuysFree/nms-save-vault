"""A config that lists no live folder discovers them at start-up; a rescan refreshes the
Organizer (tmp folders only, no dialog)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from nms_save_vault import gui
from nms_save_vault.core import discover, locations
from nms_save_vault.core import state as appstate
from nms_save_vault.core.organizer import Organizer

from .store_helpers import STEAM_ACCOUNT, new_vault, steam_live

pytest.importorskip("tkinter")


@pytest.fixture
def world(tmp_path, monkeypatch):
    root = tmp_path / "HelloGames" / "NMS"
    steam_live(root, cache=False)
    monkeypatch.setattr(locations, "nms_roots", lambda: [root])
    monkeypatch.setattr(locations, "find_microsoft_save_dirs", lambda: [])
    return root


def _boot(config, tmp_path):
    shell = SimpleNamespace(state_path=tmp_path / "state.json")
    return gui.App._load_or_bootstrap_state(shell)


def test_a_config_with_no_sources_discovers_the_live_folders(tmp_path, world, monkeypatch):
    path = tmp_path / "state.json"
    appstate.save(appstate.AppState(sources=[], vault=None, update_check="off"), path)
    st = _boot(path, tmp_path)
    assert [s.account for s in st.live_sources] == [STEAM_ACCOUNT[3:]]
    assert len(appstate.load(path).sources) == 1                      # saved
    vault = new_vault(tmp_path)
    model = Organizer(vault, st.live_sources, read_only=True).refresh(record=False)
    assert model.live_board().sources


def test_sources_that_exist_even_disabled_are_left_alone(tmp_path, world):
    path = tmp_path / "state.json"
    off = discover.discover_live_sources(world, ms_dirs=[])[0]
    off.enabled = False
    appstate.save(appstate.AppState(sources=[off], vault=None, update_check="off"), path)
    st = _boot(path, tmp_path)
    assert [s.id for s in st.sources] == [off.id] and not st.sources[0].enabled


def test_a_rescan_reaches_the_organizer_without_a_restart(gui_app, world, monkeypatch):
    from nms_save_vault.core.index import vaultlink

    view = gui_app.organizer_view
    old_sources = gui_app.state.sources
    old_active, old_live_dir = gui_app.active_source_id, gui_app.live_dir
    calls = []
    found = discover.discover_live_sources(world, ms_dirs=[])
    monkeypatch.setattr(gui.appstate, "save", lambda *a, **k: None)
    monkeypatch.setattr(gui, "_showinfo", lambda *a, **k: None)
    monkeypatch.setattr(vaultlink, "index_enabled", lambda *a, **k: False)
    monkeypatch.setattr(view, "organizer", view.organizer)       # restored afterwards
    monkeypatch.setattr(view, "model", view.model)
    monkeypatch.setattr(view, "rescan", lambda: calls.append(1))
    gui_app.state.sources = []
    try:
        view.organizer = None
        view._scan_job(False)                   # the organizer exists, over no live folder
        assert not view.organizer.sources
        monkeypatch.setattr(gui.discover, "merge_live_sources",
                            lambda st: [st.upsert(f) for f in found] and len(found))
        gui_app.on_rescan()
        assert calls == [1]                     # the view was told to scan again
        _, model, _ = view._scan_job(False)     # that scan, run here instead of the worker
        assert model.live_board().sources
    finally:
        gui_app.state.sources = old_sources
        gui_app.active_source_id, gui_app.live_dir = old_active, old_live_dir   # on_rescan adopted one
        monkeypatch.undo()
        gui_app.refresh()                       # leave the classic lists as they were


def test_no_backup_yet_has_its_tooltip(gui_app):
    from nms_save_vault.ui.organizer import table, words

    assert words.NO_BACKUP == "NO BACKUP YET"
    assert words.NO_BACKUP_TIP.startswith("This version exists only in the live save folder.")
    chip = next(c for c in table.CHIPS if c.name == "risk")
    assert chip.label == "No backup yet"
    view = gui_app.organizer_view
    assert view.table.risk_tip.text_for(None, "chip") == words.NO_BACKUP_TIP
    text = view.detail.summary_text
    assert text.nobackup_tip.text_for(text, "tip") == words.NO_BACKUP_TIP
