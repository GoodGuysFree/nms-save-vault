"""The in-window menu bar.

A native Tk menu bar on Windows is drawn by the OS: its strip stays light in dark mode and
Tk cannot colour it. This bar is a row of ``tk.Menubutton`` widgets instead, each opening the
same ``tk.Menu`` the model holds, so it follows the palette like every other widget.
Keyboard access is kept by hand: Alt + the underlined letter opens a menu, F10 the first.
"""
from __future__ import annotations

import tkinter as tk
from collections.abc import Mapping

from .. import theme
from .dpi import px


class MenuStrip(tk.Frame):
    def __init__(self, master: tk.Misc, model: tk.Menu) -> None:
        super().__init__(master, borderwidth=0, highlightthickness=0)
        self.buttons: list[tk.Menubutton] = []
        self.mnemonics: dict[str, tk.Menubutton] = {}
        last = model.index("end")
        for i in range(0 if last is None else last + 1):
            if model.type(i) != "cascade":
                continue
            label = model.entrycget(i, "label")
            underline = int(model.entrycget(i, "underline"))
            button = tk.Menubutton(
                self, text=label, underline=underline,
                menu=model.nametowidget(model.entrycget(i, "menu")),
                borderwidth=0, highlightthickness=0, padx=px(10), pady=px(3), takefocus=True)
            button.pack(side=tk.LEFT)
            self.buttons.append(button)
            if 0 <= underline < len(label):
                self.mnemonics[label[underline].lower()] = button

    def post(self, button: tk.Menubutton) -> None:
        """Open ``button``'s menu as a click on it would."""
        self.tk.call("tk::MbPost", str(button))

    def open_first(self) -> None:
        if self.buttons:
            self.post(self.buttons[0])

    def apply_palette(self, p: Mapping[str, str]) -> None:
        self.configure(background=p["bg"])
        for button in self.buttons:
            button.configure(
                background=p["bg"], foreground=p["fg"], activebackground=p["hover_bg"],
                activeforeground=p["fg"], disabledforeground=p["disabled_fg"])
            menu = self.nametowidget(str(button.cget("menu")))
            theme.style_menu(menu, p)
