"""Light / dark / system theming."""
from __future__ import annotations

import pytest

from nms_save_vault import theme
from nms_save_vault.core import state as appstate


# --- choosing ----------------------------------------------------------------


def test_explicit_choices_ignore_the_system_setting(monkeypatch):
    monkeypatch.setattr(theme, "system_prefers_dark", lambda: True)
    assert theme.resolve(theme.LIGHT) == theme.LIGHT
    monkeypatch.setattr(theme, "system_prefers_dark", lambda: False)
    assert theme.resolve(theme.DARK) == theme.DARK


def test_system_follows_windows(monkeypatch):
    monkeypatch.setattr(theme, "system_prefers_dark", lambda: True)
    assert theme.resolve(theme.SYSTEM) == theme.DARK
    monkeypatch.setattr(theme, "system_prefers_dark", lambda: False)
    assert theme.resolve(theme.SYSTEM) == theme.LIGHT


def test_unknown_system_setting_falls_back_to_light(monkeypatch):
    """Better the look the app has always had than a guess at dark."""
    monkeypatch.setattr(theme, "system_prefers_dark", lambda: None)
    assert theme.resolve(theme.SYSTEM) == theme.LIGHT


def test_unrecognised_stored_choice_is_treated_as_system(monkeypatch):
    """A hand-edited state.json must not crash the app."""
    monkeypatch.setattr(theme, "system_prefers_dark", lambda: True)
    assert theme.resolve("chartreuse") == theme.DARK


def test_system_detection_returns_a_tristate():
    assert theme.system_prefers_dark() in (True, False, None)


# --- palettes ----------------------------------------------------------------


def test_both_palettes_define_the_same_keys():
    """A key present in one and missing in the other is a KeyError at theme-switch time."""
    assert set(theme.PALETTES[theme.LIGHT]) == set(theme.PALETTES[theme.DARK])


def test_every_palette_value_is_a_colour():
    for name, palette in theme.PALETTES.items():
        for key, value in palette.items():
            assert isinstance(value, str) and value.startswith("#"), f"{name}.{key} = {value!r}"
            assert len(value) == 7, f"{name}.{key} = {value!r}"


def test_dark_and_light_actually_differ():
    light, dark = theme.PALETTES[theme.LIGHT], theme.PALETTES[theme.DARK]
    assert light["field"] != dark["field"]
    assert light["fg"] != dark["fg"]


def test_every_choice_has_a_dropdown_label():
    assert set(theme.CHOICE_LABELS) == set(theme.CHOICES)


# --- persistence -------------------------------------------------------------


def test_theme_round_trips_through_state(tmp_path):
    p = tmp_path / "state.json"
    st = appstate.AppState(sources=[], vault=None, theme=theme.DARK)
    appstate.save(st, p)
    assert appstate.load(p).theme == theme.DARK


def test_config_written_before_theming_existed_defaults_to_system(tmp_path):
    p = tmp_path / "state.json"
    p.write_text('{"version": 2, "vault": null, "sources": []}', "utf-8")
    assert appstate.load(p).theme == theme.SYSTEM


# --- applying ----------------------------------------------------------------


def test_applying_dark_switches_to_a_colourable_ttk_theme(gui_app, monkeypatch):
    """The native Windows ttk themes ignore colour settings, so dark must use clam."""
    from tkinter import ttk

    original = gui_app.state.theme
    try:
        gui_app.state.theme = theme.DARK
        gui_app._apply_theme()
        style = ttk.Style(gui_app)
        assert style.theme_use() == "clam"
        assert style.lookup("Treeview", "background") == theme.PALETTES[theme.DARK]["field"]
    finally:
        gui_app.state.theme = original
        gui_app._apply_theme()


def test_applying_light_restores_the_native_look(gui_app):
    from tkinter import ttk

    original = gui_app.state.theme
    try:
        gui_app.state.theme = theme.LIGHT
        gui_app._apply_theme()
        assert ttk.Style(gui_app).theme_use() == gui_app._native_ttk_theme
    finally:
        gui_app.state.theme = original
        gui_app._apply_theme()


def test_row_tags_and_tooltip_follow_the_palette(gui_app):
    """Tag colours live outside the ttk style, so they need re-applying by hand -- the
    light-mode green is unreadable on a dark tree."""
    from nms_save_vault import gui

    original = gui_app.state.theme
    try:
        gui_app.state.theme = theme.LIGHT
        gui_app._apply_theme()
        light_tag = gui_app.live_tree.tag_configure("live", "foreground")
        light_tip = gui.TOOLTIP_STYLE["background"]

        gui_app.state.theme = theme.DARK
        gui_app._apply_theme()
        assert gui_app.live_tree.tag_configure("live", "foreground") != light_tag
        assert gui.TOOLTIP_STYLE["background"] != light_tip
        assert gui.TOOLTIP_STYLE["background"] == theme.PALETTES[theme.DARK]["tip_bg"]
    finally:
        gui_app.state.theme = original
        gui_app._apply_theme()


def test_choosing_from_the_dropdown_updates_the_stored_choice(gui_app, monkeypatch):
    saved = {}
    monkeypatch.setattr(appstate, "save", lambda st, *a, **k: saved.setdefault("theme", st.theme))

    original = gui_app.state.theme
    try:
        gui_app.state.theme = theme.LIGHT
        gui_app.theme_var.set(theme.CHOICE_LABELS[theme.DARK])
        gui_app._on_theme_changed()
        assert gui_app.state.theme == theme.DARK
        assert saved["theme"] == theme.DARK
    finally:
        gui_app.state.theme = original
        gui_app.theme_var.set(theme.CHOICE_LABELS.get(original, "System"))
        gui_app._apply_theme()
