"""The live area (D6 U8): one or two live folders as vertical lists, any number of Steam
accounts plus Xbox, never a column for a platform the user does not have, and the
warning that Steam Cloud / Xbox sync may fill an empty slot again."""
from __future__ import annotations

import tkinter as tk

import pytest

from nms_save_vault.core import safety
from nms_save_vault.core.catalog import Vault
from nms_save_vault.core.organizer import Organizer
from nms_save_vault.core.organizer.cache import ScanCache
from nms_save_vault.core.state import Source
from nms_save_vault.ui.organizer import live as L

from .conftest import steam_save
from .organizer_world import PIDS, T0, build_world


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


def _area(host, gui_app, model, prefs=None, **kw):
    prefs = {} if prefs is None else prefs
    area = L.LiveArea(host, themed=gui_app.themed, prefs=prefs, save_prefs=lambda: None, **kw)
    area.pack(fill=tk.BOTH, expand=True)
    area.set_model(model)
    return area


def _model(tmp_path, sources):
    vault = Vault(tmp_path / "vault2")
    vault.ensure()
    return Organizer(vault.root, sources, read_only=True, cache=ScanCache(None, None),
                     game_running=lambda: False, clock=lambda: T0).refresh(record=False)


def _texts(area) -> list[str]:
    c = area.canvas
    return [c.itemcget(i, "text") for i in c.find_all() if c.type(i) == "text"]


class _Event:
    def __init__(self, x, y):
        self.x, self.y = x, y


def _packed(widget) -> bool:
    return bool(widget.winfo_manager())


def test_two_folders_side_by_side_by_default(host, gui_app, world):
    area = _area(host, gui_app, world.model)
    assert area.mode == L.TWO and [s.platform for s in area.shown_sources()] == ["steam", "xbox"]
    assert len(area.drawn) == 30
    assert all(_packed(w) for w in (*area.mode_buttons, *area.pickers))
    assert list(area.pickers[0].cget("values")) == ["Steam", "Xbox"]
    texts = _texts(area)
    assert "Steam live" in texts and "Xbox live" in texts
    assert "3 of 15 used" in texts and "2 of 15 used" in texts


def test_one_folder_with_a_selector_shows_the_last_played_and_the_warning(host, gui_app, world):
    prefs = {}
    area = _area(host, gui_app, world.model, prefs)
    area.set_mode(L.ONE)
    assert prefs["live_mode"] == L.ONE
    assert len(area.shown_sources()) == 1 and len(area.drawn) == 15
    assert _packed(area.pickers[0]) and not _packed(area.pickers[1])
    texts = " ".join(_texts(area))
    assert "Steam Cloud may put a save back here at the next game start" in texts
    assert "2026-" in texts or "20" in texts                   # the last played column


def test_only_configured_folders_never_an_empty_column(host, gui_app, tmp_path,
                                                      make_wgs_account):
    xbox = make_wgs_account(tmp_path / "x", [("Slot1Auto", '{"x":1}', "Only Xbox", "", 60,
                                              T0, 0x0102030405060708)])
    model = _model(tmp_path, [Source(id="x", platform="xbox", account=xbox.name,
                                     path=str(xbox), label="Xbox")])
    area = _area(host, gui_app, model)
    assert [s.platform for s in area.shown_sources()] == ["xbox"]
    assert not any(_packed(w) for w in (*area.mode_buttons, *area.pickers))  # no switch
    assert "Steam live" not in _texts(area) and len(area.drawn) == 15
    assert any("Xbox sync may put a save back" in L.slot_tip(d.slot, d.source, {})
               for d in area.drawn.values() if d.slot.state == "empty")


def test_several_steam_accounts_and_xbox_any_two(host, gui_app, tmp_path, make_wgs_account):
    a = tmp_path / "NMS" / "st_76561198000000001"
    b = tmp_path / "NMS2" / "st_76561198000000002"
    steam_save(a, 1, 0, "First", 60, 0x1111, T0, extra="a")
    steam_save(b, 2, 0, "Second", 60, 0x2222, T0, extra="b")
    xbox = make_wgs_account(tmp_path / "x", [("Slot1Auto", '{"x":1}', "X", "", 60, T0,
                                              0x3333)])
    sources = [Source(id="a", platform="steam", account=a.name, path=str(a),
                      label="Steam (76561198000000001)"),
               Source(id="b", platform="steam", account=b.name, path=str(b),
                      label="Steam (76561198000000002)"),
               Source(id="x", platform="xbox", account=xbox.name, path=str(xbox), label="Xbox")]
    prefs = {}
    area = _area(host, gui_app, _model(tmp_path, sources), prefs)
    labels = list(area.pickers[0].cget("values"))
    assert labels == ["Steam 1", "Steam 2", "Xbox"]
    keys = [L.source_key(s) for s in area.sources]
    area.set_source(1, keys[2])                                   # Steam 1 + Xbox
    assert [s.label for s in area.shown_sources()] == ["Steam 1", "Xbox"]
    area.set_source(0, keys[2])                                   # the same one: they swap
    assert [s.label for s in area.shown_sources()] == ["Xbox", "Steam 1"]
    assert prefs["live_sources"][:2] == [keys[2], keys[0]]
    again = _area(host, gui_app, _model(tmp_path, sources), dict(prefs, live_mode=L.ONE))
    assert [s.label for s in again.shown_sources()] == ["Xbox"]   # the choice comes back
    assert "76561198000000002" not in " ".join(_texts(area))


def test_markers_follow_the_slots(host, gui_app, world):
    area = _area(host, gui_app, world.model)
    by_label = {d.slot.label: d for d in area.drawn.values()}
    marks = {label: L.slot_markers(d.slot, d.source) for label, d in by_label.items()}
    assert marks["S3"] == ["risk"]                 # Other Run: never backed up
    assert marks["S9"] == ["pin"]                  # Main Save: in the vault, pinned
    assert marks["S1"] == ["empty"]                # empty: the cloud may fill it again
    assert marks["X1"] == ["risk"] and marks["X2"] == ["risk"]
    c = area.canvas
    assert c.find_withtag("marker:pin") and c.find_withtag("marker:risk")
    legend = " ".join(c.itemcget(i, "text") for i in c.find_withtag("legend")
                      if c.type(i) == "text")
    assert L.MARKERS["empty"] in legend and L.MARKERS["pin"] in legend
    assert L.MARKERS["pending"] not in legend      # only the markers in use


def test_the_other_platform_is_marked_on_both_lists(host, gui_app, world):
    area = _area(host, gui_app, world.model)
    texts = _texts(area)
    assert "+X1" in texts and "+S5" in texts


def test_no_cloud_no_refill_warning(host, gui_app, world):
    (world.steam / "steam_autocloud.vdf").unlink()
    area = _area(host, gui_app, world.refresh())
    slot1 = next(d for d in area.drawn.values() if d.slot.label == "S1")
    assert L.slot_markers(slot1.slot, slot1.source) == []
    tip = L.slot_tip(slot1.slot, slot1.source, {})
    assert "may put a save back" not in tip and L.NEVER_EMPTIES in tip


def test_the_empty_slot_tooltip_says_so(host, gui_app, world):
    area = _area(host, gui_app, world.model)
    token = next(t for t, d in area.drawn.items() if d.slot.label == "S1")
    tip = area._tip(token)
    assert "Steam slot 1 - empty" in tip and "Steam Cloud may put a save back" in tip
    assert L.NEVER_EMPTIES in tip


def test_hit_test_click_and_keys(host, gui_app, world):
    seen = []
    area = _area(host, gui_app, world.model, on_slot=lambda token, slot: seen.append(slot))
    token = next(t for t, d in area.drawn.items() if d.slot.label == "S9")
    x0, y0, x1, y1 = area.drawn[token].box
    assert area.token_at((x0 + x1) / 2, (y0 + y1) / 2) == token
    assert area.token_at(-5, -5) is None
    area._on_click(_Event((x0 + x1) / 2, (y0 + y1) / 2))     # the window is never shown
    assert seen[-1].pid == PIDS["main"] and area.focus_token == token
    area.move_focus(1)
    assert seen[-1].label == "S10"
    area.move_focus(15)
    assert seen[-1].label == "X10"


def test_the_selected_save_files_slots_are_outlined(host, gui_app, world):
    area = _area(host, gui_app, world.model)
    area.highlight(PIDS["cross"])
    c = area.canvas
    outlined = {area.drawn[t].slot.label for t in area.drawn
                if c.itemcget(c.find_withtag(f"slot:{t}")[0], "outline") == area._p["accent"]}
    assert outlined == {"S5", "X1"}


def test_enter_and_right_click_reach_the_seams(host, gui_app, world):
    activated, menus = [], []
    area = _area(host, gui_app, world.model,
                 on_activate=lambda token, slot: activated.append(slot.label),
                 on_context=lambda token, slot, event: menus.append(slot.label))
    token = next(t for t, d in area.drawn.items() if d.slot.label == "S3")
    area.select_slot(token)
    area._activate(area.focus_token)
    x0, y0, x1, y1 = area.drawn[token].box
    area._on_right_click(_Event(x0 + 5, y0 + 5))
    assert activated == ["S3"] and menus == ["S3"]


def test_no_live_folders_says_where_to_add_them(host, gui_app, tmp_path):
    area = _area(host, gui_app, _model(tmp_path, []))
    assert _packed(area.message) and "Live > Accounts" in area.message.cget("text")
    assert area.drawn == {}


def test_two_folders_with_one_label_stay_apart_in_the_selector(host, gui_app, tmp_path):
    a = tmp_path / "A" / "st_76561198000000001"
    b = tmp_path / "B" / "st_76561198000000002"
    steam_save(a, 1, 0, "First", 60, 0x1111, T0, extra="a")
    steam_save(b, 1, 0, "Second", 60, 0x2222, T0, extra="b")
    sources = [Source(id="a", platform="steam", account=a.name, path=str(a), label="Mine"),
               Source(id="b", platform="steam", account=b.name, path=str(b), label="Mine")]
    area = _area(host, gui_app, _model(tmp_path, sources))
    assert list(area.labels().values()) == ["Mine", "Mine #2"]
    area.pick_vars[0].set("Mine #2")
    area._picked(0)
    assert [s.ref.account_key for s in area.shown_sources()] == [b.name, a.name]
