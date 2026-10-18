"""The in-window menu bar's Left / Right between menus (the logic; a posted menu is a modal
loop on Windows, so nothing here posts one)."""
from __future__ import annotations

import sys
import tkinter as tk
from types import SimpleNamespace

import pytest

from nms_save_vault.ui.menubar import MenuStrip

pytest.importorskip("tkinter")


@pytest.fixture
def strip(gui_app):
    model = tk.Menu(gui_app, tearoff=False)
    for label in ("One", "Two", "Three"):
        sub = tk.Menu(model, tearoff=False)
        sub.add_command(label="Item", command=lambda: None)
        inner = tk.Menu(sub, tearoff=False)
        inner.add_command(label="Deep", command=lambda: None)
        sub.add_cascade(label="More", menu=inner)
        model.add_cascade(label=label, menu=sub, underline=0)
    bar = MenuStrip(gui_app, model)
    yield bar
    bar.destroy()
    model.destroy()


def test_move_wraps_round_the_strip(strip):
    posted = []
    strip.post = posted.append
    strip._posted = strip.buttons[0]
    strip._move(-1)
    strip.update()
    strip.after(30, strip.quit)
    strip.mainloop()
    assert posted == [strip.buttons[2]]
    strip._posted = strip.buttons[2]
    strip._move(1)
    strip.after(30, strip.quit)
    strip.mainloop()
    assert posted[-1] == strip.buttons[0]


@pytest.mark.skipif(sys.platform != "win32", reason="the hook is Windows only")
def test_submenu_state_follows_the_menu_events(strip):
    hook = strip._hook
    top = strip.dropdown(strip.buttons[0])
    inner = top.nametowidget(top.entrycget(1, "menu"))
    strip._posted = strip.buttons[0]
    # a cascade that is only initialised (nothing highlighted) is not "open"
    strip._on_select(SimpleNamespace(widget=inner))
    assert not hook.in_submenu
    inner.activate(0)
    strip._on_select(SimpleNamespace(widget=inner))
    assert hook.in_submenu
    top.activate(1)                          # the cascade entry: Right opens it, Left is native
    strip._on_select(SimpleNamespace(widget=top))
    assert not hook.in_submenu and hook.on_cascade
    top.activate(0)
    strip._on_select(SimpleNamespace(widget=top))
    assert not hook.on_cascade
