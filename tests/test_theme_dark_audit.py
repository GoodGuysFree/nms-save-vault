"""Dark-theme audit: every window and dialog, walked widget by widget.

The defects this guards against all had one cause: a classic Tk widget (Toplevel, Frame,
Label, Text, Canvas, Menu ...) or a ttk style keeping the system's light colour in dark mode,
which shows as a white band, a light outline or a white check box. ``audit`` walks a widget
tree and lists every background that is light where the palette is dark (and, in the light
theme, every one that is a dark-palette surface left over).
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

import pytest

from nms_save_vault import theme
from nms_save_vault.core import safety
from nms_save_vault.ui.organizer import history as H

from .organizer_world import PIDS, build_world
from .test_organizer_readmodel import PID
from .test_ui_history import _forked, _long, game_not_running  # noqa: F401
from .test_ui_move import _open, _wait, running, threads, view, world  # noqa: F401
from .test_ui_vaultwin import isolated, legacy  # noqa: F401

LIGHT_LIMIT = 0.35          # relative luminance above which a dark-theme surface is "light"

#: Colours that are light on purpose: the green / amber outcome boxes' labels and the lanes.
INTENTIONAL = {theme.PALETTES[theme.DARK][k] for k in (
    "in_fg", "out_fg", "ok", "live", "active", "xbox", "steam", "warn", "danger", "pending",
    "event", "readonly", "lane0", "lane1", "lane2", "lane3", "accent", "sel_bg")}

CLASSIC = {"Tk", "Toplevel", "Frame", "Labelframe", "Panedwindow", "Label", "Message",
           "Checkbutton", "Radiobutton", "Text", "Listbox", "Entry", "Spinbox", "Canvas",
           "Menu", "Menubutton", "Scale"}
FIELD_STYLES = {"TEntry", "TCombobox", "Treeview", "TSpinbox"}


def _lum(colour: str) -> float:
    out = []
    for i in (1, 3, 5):
        c = int(colour[i:i + 2], 16) / 255
        out.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * out[0] + 0.7152 * out[1] + 0.0722 * out[2]


def _hex(w: tk.Misc, colour) -> str | None:
    if not colour:
        return None
    try:
        r, g, b = w.winfo_rgb(str(colour))
    except tk.TclError:
        return None
    return f"#{r >> 8:02x}{g >> 8:02x}{b >> 8:02x}"


def surfaces(root: tk.Misc):
    """(widget path, option, hex colour) for every background-like colour under ``root``."""
    style = ttk.Style(root)
    stack = [root]
    while stack:
        w = stack.pop()
        stack.extend(w.winfo_children())
        cls = w.winfo_class()
        if cls in CLASSIC:
            options = ["background"]
            try:
                if int(w.cget("highlightthickness")) > 0:
                    options.append("highlightbackground")
            except (tk.TclError, ValueError):
                pass
            for option in options:
                try:
                    yield str(w), cls, option, _hex(w, w.cget(option))
                except tk.TclError:
                    pass
        elif cls.startswith("T") or cls == "Treeview":
            name = str(w.cget("style")) or cls
            options = ["background"] + (["fieldbackground"] if cls in FIELD_STYLES else [])
            if cls in ("TScrollbar", "TProgressbar"):
                options.append("troughcolor")
            for option in options:
                yield str(w), name, option, _hex(w, style.lookup(name, option))
            if cls in ("TCheckbutton", "TRadiobutton"):
                yield str(w), name, "indicatorbackground", _hex(
                    w, style.lookup(name, "indicatorbackground"))


def audit(root: tk.Misc, name: str) -> list[str]:
    """What is wrong with the colours under ``root`` for theme ``name``."""
    dark = theme.PALETTES[theme.DARK]
    leftovers = {dark[k] for k in ("bg", "field", "card", "status_bg", "btn_bg")}
    problems = []
    for path, cls, option, colour in surfaces(root):
        if colour is None:
            continue
        if name == theme.DARK and _lum(colour) > LIGHT_LIMIT and colour not in INTENTIONAL:
            problems.append(f"{path} ({cls}) {option} = {colour} is light")
        if name == theme.LIGHT and colour in leftovers and option != "troughcolor":
            problems.append(f"{path} ({cls}) {option} = {colour} is a dark-theme surface")
    return problems


@pytest.fixture(params=[theme.DARK, theme.LIGHT])
def mode(request, gui_app, monkeypatch):
    """The whole app in one theme (restored afterwards); window dressing is not under test."""
    monkeypatch.setattr(theme, "dark_title_bar", lambda *a, **k: False)
    original = gui_app.state.theme
    gui_app.state.theme = request.param
    gui_app._apply_theme()
    yield request.param
    gui_app.state.theme = original
    gui_app._apply_theme()


def check(gui_app, window, name):
    gui_app.update()
    assert audit(window, name) == []


# --- the main window ----------------------------------------------------------------------------


def test_the_main_window_both_tabs_and_the_menu_bar(gui_app, mode):
    check(gui_app, gui_app, mode)
    strip = gui_app.menu_strip
    # Native bar by default; the in-window one only in dark on Windows.
    assert gui_app._strip_active == theme.wants_menu_strip(mode == theme.DARK)
    assert bool(gui_app.cget("menu")) != gui_app._strip_active
    assert bool(strip.winfo_manager()) == gui_app._strip_active


@pytest.fixture
def strip_app(gui_app, monkeypatch):
    """The in-window bar, whatever the platform: dark theme with the strip forced on."""
    monkeypatch.setattr(theme, "dark_title_bar", lambda *a, **k: False)
    monkeypatch.setattr(theme, "wants_menu_strip", lambda dark: dark)
    original = gui_app.state.theme
    gui_app.state.theme = theme.DARK
    gui_app._apply_theme()
    gui_app.update()
    yield gui_app
    gui_app.state.theme = original
    gui_app._apply_theme()


def test_the_in_window_bar_follows_the_palette(strip_app):
    strip = strip_app.menu_strip
    palette = theme.PALETTES[theme.DARK]
    assert strip.winfo_manager() == "pack" and not strip_app.cget("menu")
    assert _hex(strip, strip.cget("background")) == palette["bg"]
    for button in strip.buttons:
        assert _hex(button, button.cget("background")) == palette["bg"]
        dropdown = strip.dropdown(button)
        assert _hex(dropdown, dropdown.cget("background")) == palette["btn_bg"]
        assert _hex(dropdown, dropdown.cget("activebackground")) == palette["sel_bg"]
    assert audit(strip, theme.DARK) == []


def test_the_dropdowns_are_descendants_of_their_buttons_and_copy_the_model(strip_app):
    """Tk refuses to post a dropdown that is not a descendant of its Menubutton."""
    strip = strip_app.menu_strip
    model = strip_app.menubar
    for i, button in enumerate(strip.buttons):
        dropdown = strip.dropdown(button)
        assert str(dropdown).startswith(str(button) + ".")
        source = strip_app.nametowidget(model.entrycget(i, "menu"))
        types = [source.type(n) for n in range(source.index("end") + 1)]
        assert [dropdown.type(n) for n in range(dropdown.index("end") + 1)] == types
        for n, kind in enumerate(types):
            if kind != "separator":
                assert dropdown.entrycget(n, "label") == source.entrycget(n, "label")
                assert dropdown.entrycget(n, "underline") == source.entrycget(n, "underline")
                assert dropdown.entrycget(n, "accelerator") == source.entrycget(n, "accelerator")
    view = strip.dropdown(strip.buttons[2])
    theme_menu = strip_app.nametowidget(view.entrycget("Theme", "menu"))
    assert [theme_menu.entrycget(n, "label") for n in range(3)] == ["System", "Light", "Dark"]
    assert str(theme_menu).startswith(str(view) + ".")


def test_alt_letter_and_f10_open_menus_and_other_windows_do_not(strip_app, monkeypatch):
    strip = strip_app.menu_strip
    opened = []
    monkeypatch.setattr(strip, "post", lambda b: opened.append(b.cget("text")))
    strip_app.deiconify()                 # key events need a mapped window with the focus
    strip_app.update()
    strip_app.focus_force()
    strip_app.update()
    try:
        _keys(strip_app, strip, opened)
    finally:
        strip_app.withdraw()


def _keys(strip_app, strip, opened):
    for letter, button in strip.mnemonics.items():
        for key in (letter, letter.upper()):
            strip_app.event_generate(f"<Alt-Key-{key}>")
            assert opened[-1] == button.cget("text")
    opened.clear()
    strip_app.event_generate("<Key-F10>")
    assert opened == ["File"]
    opened.clear()
    strip_app.event_generate("<Key-Alt_L>")                     # an Alt tap opens the first menu
    strip_app.event_generate("<KeyRelease-Alt_L>")
    assert opened == ["File"]
    opened.clear()
    strip_app.event_generate("<KeyRelease-Alt_L>")              # a release with no press: ignored
    other = tk.Toplevel(strip_app)
    entry = tk.Entry(other)
    entry.pack()
    try:
        other.update()
        entry.focus_force()
        other.update()
        entry.event_generate("<Alt-Key-e>")
        entry.event_generate("<Key-F10>")
        assert opened == []
    finally:
        other.destroy()


def test_a_window_is_retinted_when_it_maps(gui_app, mode):
    """The <Map> path: a window that appears after the theme was applied gets its stale
    light widgets recoloured as it is shown."""
    win = tk.Toplevel(gui_app)                       # not a transient: it really maps
    stale = tk.Frame(win, background="#f0f0f0" if mode == theme.DARK else "#242424")
    stale.pack()
    try:
        win.geometry("+5000+5000")                   # off screen
        win.deiconify()
        win.update()
        assert win.winfo_ismapped()
        assert _hex(stale, stale.cget("background")) == theme.PALETTES[mode]["bg"]
    finally:
        win.destroy()


def test_the_native_bar_comes_back_with_its_keys(gui_app, monkeypatch):
    monkeypatch.setattr(theme, "dark_title_bar", lambda *a, **k: False)
    monkeypatch.setattr(theme, "wants_menu_strip", lambda dark: dark)
    original = gui_app.state.theme
    try:
        gui_app.state.theme = theme.DARK
        gui_app._apply_theme()
        assert gui_app._strip_active and not gui_app.cget("menu")
        assert gui_app.bind_all("<Alt-Key-f>")
        gui_app.state.theme = theme.LIGHT
        gui_app._apply_theme()
        assert not gui_app._strip_active and gui_app.cget("menu") == str(gui_app.menubar)
        assert not gui_app.menu_strip.winfo_manager()
        assert not gui_app.bind_all("<Alt-Key-f>")
        for seq, script in gui_app._tk_menu_keys.items():
            assert gui_app.tk.call("bind", "all", seq) == script
    finally:
        gui_app.state.theme = original
        gui_app._apply_theme()


def test_the_bar_adds_no_tab_stops(gui_app):
    assert not any(int(b.cget("takefocus") or 0) for b in gui_app.menu_strip.buttons)


def test_check_marks_are_readable_in_both_themes(gui_app, mode):
    style = ttk.Style(gui_app)
    p = theme.PALETTES[mode]
    for widget in ("TCheckbutton", "TRadiobutton"):
        assert style.lookup(widget, "indicatorbackground") == p["field"]
        assert style.lookup(widget, "indicatorforeground") == p["fg"]
        assert style.lookup(widget, "indicatorbackground", ["disabled"]) == p["disabled_bg"]
        gap = str(style.lookup(widget, "indicatormargin")).split()
        assert len(gap) == 4 and int(gap[2]) >= 4      # room between the box and its text


def test_a_stale_light_widget_is_retinted_when_the_theme_switches(gui_app, monkeypatch):
    """The path for a window that is already open when the theme changes."""
    monkeypatch.setattr(theme, "dark_title_bar", lambda *a, **k: False)
    original = gui_app.state.theme
    gui_app.state.theme = theme.LIGHT
    gui_app._apply_theme()
    win = tk.Toplevel(gui_app)
    win.withdraw()
    frame = tk.Frame(win, highlightthickness=1)
    frame.pack()
    text = tk.Text(win, highlightthickness=1)
    text.pack()
    menu = tk.Menu(win, tearoff=False)
    box = tk.Frame(win, background="#e3f4e6")           # a deliberate tint: must survive
    box.pack()
    try:
        gui_app.state.theme = theme.DARK
        gui_app._apply_theme()
        assert [x for x in audit(win, theme.DARK) if "e3f4e6" not in x] == []
        assert _hex(text, text.cget("background")) == theme.PALETTES[theme.DARK]["field"]
        assert _hex(menu, menu.cget("background")) == theme.PALETTES[theme.DARK]["btn_bg"]
        assert _hex(box, box.cget("background")) == "#e3f4e6"
        gui_app.state.theme = theme.LIGHT
        gui_app._apply_theme()
        assert audit(win, theme.LIGHT) == []
        assert _hex(frame, frame.cget("background")) == theme.PALETTES[theme.LIGHT]["bg"]
    finally:
        win.destroy()
        gui_app.state.theme = original
        gui_app._apply_theme()


def test_the_audit_catches_a_light_widget(gui_app, mode):
    """Guard the guard: a white Frame must be reported in the dark theme."""
    host = tk.Toplevel(gui_app)
    host.withdraw()
    try:
        tk.Frame(host, background="#ffffff").pack()
        found = audit(host, mode)
        assert bool(found) == (mode == theme.DARK)
    finally:
        host.destroy()


# --- the Organizer: table, detail pane, Move, plan, log, steps, recovery ------------------------


def test_the_organizer_view_and_its_context_menus(view, gui_app, mode):
    check(gui_app, view.winfo_toplevel(), mode)
    menu = view.slot_menu(next(s for s in view.model.live_board().sources[0].slots
                               if s.state in ("occupied", "partial")))
    check(gui_app, menu, mode)


def test_the_move_dialog(view, gui_app, mode):
    d = _open(view, gui_app)
    try:
        check(gui_app, d, mode)
    finally:
        d.cancel()


def test_the_move_dialog_with_a_confirmation(view, gui_app, mode):
    d = _open(view, gui_app)
    try:
        assert d._confirm_vars                         # the "also live elsewhere" checkbox
        check(gui_app, d, mode)
    finally:
        d.cancel()


def test_the_operations_log_and_the_steps_and_recovery_windows(view, gui_app, mode):
    from nms_save_vault.core.liveops import RecoveryItem

    view.open_log()
    log = view.log_window
    gui_app.update()
    item = RecoveryItem("op-20260101T000000Z-bbbb", "place", ("Steam slot 1",), "torn", "back",
                        ("forward", "back", "leave"), "The move stopped half way.",
                        "2026-01-01 00:00")
    recovery = view.open_recovery([item])
    wins = []
    original = view._steps
    view._steps = lambda guide: wins.append(original(guide))
    slot = next(s for s in view.model.live_board().sources[0].slots
                if s.target.slot == 1 and s.state in ("occupied", "partial"))
    view.slot_menu(slot).invoke(view.slot_menu(slot).index("Empty this slot..."))
    _wait(gui_app, lambda: wins)
    try:
        for window in (log, recovery, wins[0]):
            check(gui_app, window, mode)
    finally:
        for window in (log, recovery, wins[0]):
            window.destroy()


# --- notes, history, compare, correct, the vault window ----------------------------------------


@pytest.fixture
def notes_world(tmp_path, make_wgs_account, monkeypatch):
    monkeypatch.setattr(safety, "is_game_running", lambda: False)
    return build_world(tmp_path, make_wgs_account)


def test_the_notes_window(gui_app, notes_world, mode):
    from nms_save_vault.ui.organizer import notes as N

    win = N.NotesWindow(gui_app, notes=notes_world.organizer.notes, model=notes_world.model,
                        worker=gui_app.worker, themed=gui_app.themed, pid=PIDS["main"])
    win.withdraw()
    try:
        check(gui_app, win, mode)
    finally:
        win.destroy()


def test_history_compare_pick_and_correct(gui_app, tmp_path, mode):
    from nms_save_vault.ui.organizer.compare import CompareWindow
    from nms_save_vault.ui.organizer.correct import CorrectDialog

    w, (a, b, c, d, e, f), cor = _long(tmp_path)
    model = w.refresh()
    host = tk.Toplevel(gui_app)
    host.withdraw()
    windows = [H.HistoryWindow(host, model=model, themed=gui_app.themed, pid=PID, head=c)]
    compare = CompareWindow(host, model=model, themed=gui_app.themed, pid=PID, a=f, b=c)
    windows.append(compare)
    windows.append(CorrectDialog(host, model=model, corrections=cor, worker=gui_app.worker,
                                 themed=gui_app.themed, pid=PID, head=b, on_done=lambda: None))
    compare.pick()                                     # the PickDialog
    windows += [x for x in compare.winfo_children() if isinstance(x, tk.Toplevel)]
    try:
        for window in windows:
            window.withdraw()
            check(gui_app, window, mode)
    finally:
        host.destroy()


def test_the_vault_window(gui_app, legacy, mode):  # noqa: F811
    from nms_save_vault.ui.organizer import vaultwin as W

    top = tk.Toplevel(gui_app)
    top.withdraw()
    win = W.VaultWindow(top, worker=gui_app.worker, themed=gui_app.themed, env=legacy.env,
                        on_changed=None)
    _wait(gui_app, lambda: not win.busy and win.state is not None)
    try:
        check(gui_app, win, mode)
    finally:
        top.destroy()


# --- the app's own small windows ---------------------------------------------------------------


def test_help_the_wait_window_and_the_tooltip(gui_app, mode):
    from nms_save_vault.ui.tooltip import Tooltip

    gui_app.on_help()
    helps = [w for w in gui_app.winfo_children() if isinstance(w, tk.Toplevel)]
    wait = gui_app._busy("Working...")
    tree = gui_app.live_tree
    tip = Tooltip(tree, lambda *_: "A hover text")
    tip.row = "x"
    tip._show("x", 10, 10)
    try:
        for window in [*helps, wait, tip.window]:
            window.withdraw()
            check(gui_app, window, mode)
    finally:
        tip.hide()
        wait.grab_release()
        wait.destroy()
        for window in helps:
            window.destroy()
