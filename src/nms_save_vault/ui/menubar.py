"""The in-window menu bar.

A native Tk menu bar on Windows is drawn by the OS: its strip stays light in dark mode and
Tk cannot colour it. In the dark theme on Windows the window therefore shows this bar instead:
a row of ``tk.Menubutton`` widgets whose dropdowns are copies of the model menu's cascades, so
it follows the palette like every other widget. (Tk only posts a dropdown that is a
descendant of its Menubutton, hence copies rather than the model's own menus.)

Keyboard access is kept by hand: Alt + the underlined letter opens a menu, F10 (or Alt on its
own) the first one. Left / Right between open menus are done by hand too: Windows runs a posted
Tk menu as a native popup that knows nothing of its neighbours, so the platform's key hook
(``theme.MenuKeyHook``, handed in by the window) ends the popup and this bar posts the
neighbour once the popup has returned. Menus opened with the mouse keep the native keys.
"""
from __future__ import annotations

import tkinter as tk
from collections.abc import Mapping

from .. import theme
from .dpi import px

#: Entry options that name the entry itself and are not copied as they are.
_SKIP = {"menu"}


def _copy_entries(source: tk.Menu, target: tk.Menu) -> None:
    last = source.index("end")
    for i in range(0 if last is None else last + 1):
        kind = source.type(i)
        if kind == "tearoff":
            continue
        options = {}
        for key, value in source.entryconfigure(i).items():
            if key in _SKIP or len(value) < 5:
                continue
            current, default = value[4], value[3]
            if str(current) != str(default) and str(current) != "":
                options[key] = current
        if kind == "cascade":
            child = tk.Menu(target, tearoff=False)
            _copy_entries(source.nametowidget(source.entrycget(i, "menu")), child)
            options["menu"] = child
        target.add(kind, **options)


class MenuStrip(tk.Frame):
    def __init__(self, master: tk.Misc, model: tk.Menu, key_hook=None) -> None:
        super().__init__(master, borderwidth=0, highlightthickness=0)
        self.buttons: list[tk.Menubutton] = []
        self.mnemonics: dict[str, tk.Menubutton] = {}
        last = model.index("end")
        for i in range(0 if last is None else last + 1):
            if model.type(i) != "cascade":
                continue
            label = model.entrycget(i, "label")
            underline = int(model.entrycget(i, "underline"))
            source = model.nametowidget(model.entrycget(i, "menu"))
            button = tk.Menubutton(
                self, text=label, underline=underline, borderwidth=0, highlightthickness=0,
                padx=px(10), pady=px(3), takefocus=False)
            dropdown = tk.Menu(button, tearoff=False)
            _copy_entries(source, dropdown)
            handler = getattr(source, "hint_handler", None)
            if handler is not None:
                dropdown.bind("<<MenuSelect>>", handler, add="+")
            button.configure(menu=dropdown)
            button.pack(side=tk.LEFT)
            self.buttons.append(button)
            if 0 <= underline < len(label):
                self.mnemonics[label[underline].lower()] = button
        self._hook = key_hook if self.buttons else None
        self._posted: tk.Menubutton | None = None
        if self._hook is not None:
            for button in self.buttons:
                self._watch(self.dropdown(button))

    def _watch(self, menu: tk.Menu) -> None:
        menu.bind("<<MenuSelect>>", self._on_select, add="+")
        for child in menu.winfo_children():
            if isinstance(child, tk.Menu):
                self._watch(child)

    def post(self, button: tk.Menubutton) -> None:
        """Open ``button``'s menu as a click on it would. Left / Right inside it (when the
        key hook is there) close it and open the neighbour as soon as it has returned."""
        self._posted = button
        hook = self._hook
        step = 0
        try:
            if hook is not None:
                hook.install()
            # Tk refuses to post the menu it believes is still posted, and a popup ended by the
            # OS (Escape, EndMenu) never tells it.
            self.tk.eval("set ::tk::Priv(postedMb) {}")
            self.tk.call("tk::MbPost", str(button))
        finally:
            try:
                if hook is not None:
                    step = hook.remove()    # first: a failure below must not leave it on
            finally:
                self.tk.eval("set ::tk::Priv(postedMb) {}")
        if step:
            i = (self.buttons.index(button) + step) % len(self.buttons)
            self.after(1, lambda: self.post(self.buttons[i]))

    def _on_select(self, event: tk.Event) -> None:
        """Tell the key hook what the posted dropdown highlights: an entry that opens a
        cascade keeps Right, an open cascade keeps Left."""
        menu, hook = event.widget, self._hook
        if hook is None or self._posted is None or not isinstance(menu, tk.Menu):
            return
        top = self.dropdown(self._posted)
        if menu is not top and not str(menu).startswith(str(top) + "."):
            return
        try:
            active = menu.index("active")
            on_cascade = active not in (None, "none") and menu.type(active) == "cascade"
        except tk.TclError:
            active, on_cascade = None, False
        if menu is top:
            hook.in_submenu = False
        elif active not in (None, "none"):
            hook.in_submenu = True      # Windows also announces cascades merely initialised
        else:
            return
        hook.on_cascade = on_cascade

    def open_first(self) -> None:
        if self.buttons:
            self.post(self.buttons[0])

    def dropdown(self, button: tk.Menubutton) -> tk.Menu:
        return self.nametowidget(str(button.cget("menu")))

    def apply_palette(self, p: Mapping[str, str]) -> None:
        self.configure(background=p["bg"])
        for button in self.buttons:
            button.configure(
                background=p["bg"], foreground=p["fg"], activebackground=p["hover_bg"],
                activeforeground=p["fg"], disabledforeground=p["disabled_fg"])
            theme.style_menu(self.dropdown(button), p)
