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
    """Native where there is one; clam on X11, whose 'default' is a Motif-era fallback."""
    from tkinter import ttk

    original = gui_app.state.theme
    try:
        gui_app.state.theme = theme.LIGHT
        gui_app._apply_theme()
        expected = theme.ttk_theme_for(gui_app, theme.LIGHT, gui_app._native_ttk_theme)
        assert ttk.Style(gui_app).theme_use() == expected
    finally:
        gui_app.state.theme = original
        gui_app._apply_theme()


def test_x11_light_mode_does_not_keep_tk_default(gui_app):
    """The bug behind "the dark theme isn't great": on Linux both palettes need clam."""
    assert theme.ttk_theme_for(gui_app, theme.DARK, "default") == "clam"

    class FakeX11:
        tk = type("Tk", (), {"call": staticmethod(lambda *a: "x11")})()

    assert theme.ttk_theme_for(FakeX11(), theme.LIGHT, "default") == "clam"

    class FakeWindows:
        tk = type("Tk", (), {"call": staticmethod(lambda *a: "win32")})()

    assert theme.ttk_theme_for(FakeWindows(), theme.LIGHT, "vista") == "vista"


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


# --- contrast -----------------------------------------------------------------
#
# The reported complaint was that dark mode "isn't great" and the buttons do not
# highlight. Eyeballing a palette does not catch that, and neither of us can look at the
# reporter's screen, so the guarantee is arithmetic: WCAG 2.1 relative luminance.


def _luminance(hex_colour: str) -> float:
    channels = []
    for i in (1, 3, 5):
        c = int(hex_colour[i : i + 2], 16) / 255
        channels.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    r, g, b = channels
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    """WCAG contrast ratio between two colours, 1.0 (identical) to 21.0 (black on white)."""
    la, lb = _luminance(a), _luminance(b)
    lo, hi = sorted((la, lb))
    return (hi + 0.05) / (lo + 0.05)


def test_the_contrast_helper_agrees_with_known_values():
    """Guard the guard: a wrong formula would pass every palette silently."""
    assert contrast("#000000", "#ffffff") == pytest.approx(21.0, abs=0.01)
    assert contrast("#ffffff", "#ffffff") == pytest.approx(1.0, abs=0.01)


#: Text that has to be comfortably readable: WCAG AA for body text.
READABLE = [
    ("fg", "bg"),
    ("fg", "field"),
    ("fg", "btn_bg"),
    ("fg", "hover_bg"),
    ("fg", "pressed_bg"),
    ("fg", "status_bg"),
    ("sel_fg", "sel_bg"),
    ("heading_fg", "heading_bg"),
    ("tip_fg", "tip_bg"),
    ("banner_fg", "banner_bg"),
    ("live", "field"),
    ("readonly", "field"),
    ("backup", "field"),
]

#: Deliberately quieter, but still legible -- disabled text says "not now", not "nothing
#: here", and the focus ring only has to be seen, not read.
VISIBLE = [
    ("disabled_fg", "disabled_bg"),
    ("disabled_fg", "bg"),
    ("accent", "bg"),
]


@pytest.mark.parametrize("name", sorted(theme.PALETTES))
@pytest.mark.parametrize("fg, bg", READABLE, ids=lambda v: v)
def test_text_pairs_meet_wcag_aa(name, fg, bg):
    p = theme.PALETTES[name]
    ratio = contrast(p[fg], p[bg])
    assert ratio >= 4.5, f"{name}: {fg} {p[fg]} on {bg} {p[bg]} is only {ratio:.2f}:1"


@pytest.mark.parametrize("name", sorted(theme.PALETTES))
@pytest.mark.parametrize("fg, bg", VISIBLE, ids=lambda v: v)
def test_secondary_pairs_stay_visible(name, fg, bg):
    p = theme.PALETTES[name]
    ratio = contrast(p[fg], p[bg])
    assert ratio >= 3.0, f"{name}: {fg} {p[fg]} on {bg} {p[bg]} is only {ratio:.2f}:1"


def test_button_surfaces_are_distinguishable():
    """Hover has to be *seen* as a change, not merely be a different hex value."""
    for name, p in theme.PALETTES.items():
        assert contrast(p["btn_bg"], p["hover_bg"]) >= 1.10, name
        assert contrast(p["hover_bg"], p["pressed_bg"]) >= 1.10, name


# --- the state maps, which is what was actually broken -------------------------


def _apply(gui_app, choice):
    gui_app.state.theme = choice
    gui_app._apply_theme()
    from tkinter import ttk

    return ttk.Style(gui_app), theme.PALETTES[choice]


@pytest.mark.parametrize("choice", [theme.LIGHT, theme.DARK])
def test_buttons_are_mapped_for_every_state(gui_app, choice):
    """Without these maps a themed button keeps clam's light-tuned state colours: it does
    not react to the pointer, and its disabled label disappears."""
    original = gui_app.state.theme
    try:
        style, p = _apply(gui_app, choice)
        assert style.lookup("TButton", "background", ["active"]) == p["hover_bg"]
        assert style.lookup("TButton", "background", ["pressed"]) == p["pressed_bg"]
        assert style.lookup("TButton", "foreground", ["disabled"]) == p["disabled_fg"]
    finally:
        gui_app.state.theme = original
        gui_app._apply_theme()


@pytest.mark.parametrize("choice", [theme.LIGHT, theme.DARK])
def test_readonly_combobox_text_is_legible(gui_app, choice):
    """The 'Active live' and 'Theme' pickers are readonly, so they sit in that state for
    life -- unmapped, they showed dark text on a dark field."""
    original = gui_app.state.theme
    try:
        style, p = _apply(gui_app, choice)
        field = style.lookup("TCombobox", "fieldbackground", ["readonly"])
        fg = style.lookup("TCombobox", "foreground", ["readonly"])
        assert field == p["btn_bg"]
        assert fg == p["fg"]
        assert contrast(fg, field) >= 4.5
        # And no permanent selection block behind the value.
        assert style.lookup("TCombobox", "selectbackground", ["readonly"]) == p["btn_bg"]
    finally:
        gui_app.state.theme = original
        gui_app._apply_theme()


def test_borders_are_visible_without_being_heavy():
    """A hairline divider is not a state indicator, so WCAG's 3:1 for non-text elements is
    the wrong bar -- a border that strong reads as a drawn box. It only has to be seen."""
    for name, p in theme.PALETTES.items():
        ratio = contrast(p["border"], p["bg"])
        assert 1.25 <= ratio <= 3.0, f"{name}: border {p['border']} on bg is {ratio:.2f}:1"
