"""The in-window menu bar.

A native Tk menu bar on Windows is drawn by the OS: its strip stays light in dark mode and
Tk cannot colour it. In the dark theme on Windows the window therefore shows this bar instead:
a row of ``tk.Menubutton`` widgets whose dropdowns are copies of the model menu's cascades, so
it follows the palette like every other widget. (Tk only posts a dropdown that is a
descendant of its Menubutton, hence copies rather than the model's own menus.)

Keyboard access is kept by hand: Alt + the underlined letter opens a menu, F10 (or Alt on its
own) the first one. Left / Right between open menus are done by hand too: Windows runs a posted
Tk menu as a native popup that knows nothing of its neighbours, so a message-filter hook
(``_MenuKeyHook``) ends the popup and posts the neighbour.
"""
from __future__ import annotations

import sys
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


_WH_MSGFILTER, _MSGF_MENU, _WM_KEYDOWN = -1, 2, 0x0100
_VK_LEFT, _VK_RIGHT = 0x25, 0x27


class _MenuKeyHook:
    """Left / Right inside a posted dropdown (Windows only): leave it, open the neighbour.

    The popup's modal loop passes its messages through a WH_MSGFILTER hook; a Left or a Right
    on a top-level dropdown (not in a cascade, not on a cascade entry that Right opens) is
    swallowed and handed to ``move``."""

    def __init__(self, move) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._move = move
        self._handle = None
        self.active = False       # a dropdown posted by keyboard is on screen
        self.in_submenu = False   # ... and one of its cascades is open: Left closes that
        self.on_cascade = False   # ... or its highlighted entry opens one: Right does that

        class MSG(ctypes.Structure):
            _fields_ = [("hwnd", wintypes.HWND), ("message", wintypes.UINT),
                        ("wParam", wintypes.WPARAM), ("lParam", wintypes.LPARAM),
                        ("time", wintypes.DWORD), ("pt", wintypes.POINT)]

        proto = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM,
                                   wintypes.LPARAM)
        user32 = ctypes.windll.user32
        user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, wintypes.WPARAM,
                                          wintypes.LPARAM]
        user32.CallNextHookEx.restype = ctypes.c_ssize_t
        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, proto, wintypes.HINSTANCE,
                                             wintypes.DWORD]
        user32.SetWindowsHookExW.restype = ctypes.c_void_p
        self._user32 = user32

        def hook(code, wparam, lparam):
            try:
                if code == _MSGF_MENU and self.active and not self.in_submenu:
                    msg = MSG.from_address(lparam)
                    if msg.message == _WM_KEYDOWN and (
                            msg.wParam == _VK_LEFT
                            or (msg.wParam == _VK_RIGHT and not self.on_cascade)):
                        self.active = False
                        user32.EndMenu()
                        self._move(-1 if msg.wParam == _VK_LEFT else 1)
                        return 1
            except Exception:       # a hook must never raise into the OS
                pass
            return user32.CallNextHookEx(None, code, wparam, lparam)

        self._proc = proto(hook)    # kept alive for the life of the hook
        self._handle = user32.SetWindowsHookExW(
            _WH_MSGFILTER, self._proc, None, ctypes.windll.kernel32.GetCurrentThreadId())

    def close(self) -> None:
        if self._handle:
            self._user32.UnhookWindowsHookEx(self._handle)
            self._handle = None


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
        self._hook = None
        self._posted: tk.Menubutton | None = None
        if sys.platform == "win32" and self.buttons:
            self._hook = _MenuKeyHook(self._move)
            self.bind_all("<<MenuSelect>>", self._on_select, add="+")
            self.bind("<Destroy>", lambda e: self._hook.close() if e.widget is self else None,
                      add="+")

    def post(self, button: tk.Menubutton) -> None:
        """Open ``button``'s menu as a click on it would."""
        self._posted = button
        if self._hook is not None:
            self._hook.active = True
            self._hook.in_submenu = self._hook.on_cascade = False
        # Tk refuses to post the menu it believes is still posted, and a popup ended by the OS
        # (Escape, EndMenu) never tells it.
        self.tk.eval("set ::tk::Priv(postedMb) {}")
        try:
            self.tk.call("tk::MbPost", str(button))
        finally:
            self.tk.eval("set ::tk::Priv(postedMb) {}")
            if self._hook is not None:
                self._hook.active = False

    def _on_select(self, event: tk.Event) -> None:
        """Track what the posted dropdown highlights: a cascade entry keeps Right, an open
        cascade keeps Left."""
        menu, hook = event.widget, self._hook
        if hook is None or self._posted is None or not isinstance(menu, tk.Menu):
            return
        top = self.dropdown(self._posted)
        if menu is top:
            hook.in_submenu = False
            try:
                hook.on_cascade = menu.type("active") == "cascade"
            except tk.TclError:
                hook.on_cascade = False
        elif str(menu).startswith(str(top) + "."):
            # Windows also announces cascades that are merely initialised: only one with a
            # highlighted entry is open.
            hook.in_submenu = menu.index("active") not in (None, "none")

    def _move(self, step: int) -> None:
        """Open the menu ``step`` places along from the posted one, wrapping round."""
        if self._posted is None:
            return
        i = (self.buttons.index(self._posted) + step) % len(self.buttons)
        target = self.buttons[i]
        self.after(10, lambda: self.post(target))

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
