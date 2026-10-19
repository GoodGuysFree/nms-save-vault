"""Keyboard and sizing basics every dialog shares: Escape cancels (never while a write runs),
Return presses the safe default (only where that is not destructive), the main list or field
has the focus from the start, Tab visits the content before the footer, and a minimum size is
never bigger than the screen can show.
"""
from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from tkinter import ttk

from .dpi import px

#: A dialog's minimum size never exceeds this share of the screen: the footer stays reachable.
SCREEN_SHARE = 0.9

#: Widgets that use Return themselves (a new line).
_OWN_RETURN = (tk.Text,)


def grab_owner(widget: tk.Misc) -> tk.Misc | None:
    """The window holding the grab, or None. Tk's own dialogs (the message box on Linux and
    macOS is ``.__tk__messagebox``) hold a grab tkinter has no widget for, and
    ``grab_current()`` raises KeyError for them; such a grab answers ``widget`` itself, so a
    caller still sees "something holds the grab"."""
    try:
        name = widget.tk.call("grab", "current")
    except tk.TclError:
        return None
    if not name:
        return None
    try:
        return widget.nametowidget(str(name))
    except (KeyError, tk.TclError):
        return widget


def grab_held(widget: tk.Misc) -> bool:
    """Whether any window of the app holds the grab now (a dialog or a Tk message box)."""
    try:
        return bool(widget.tk.call("grab", "current"))
    except tk.TclError:
        return False


def bind_escape(win: tk.Misc, cancel: Callable[[], None],
                busy: Callable[[], bool] | None = None) -> Callable[[object], str | None]:
    """Escape runs ``cancel`` unless ``busy()`` (a write is under way). Returns the handler."""
    def handler(_event=None):
        if busy is not None and busy():
            return "break"
        cancel()
        return "break"

    win.bind("<Escape>", handler)
    win._escape_handler = handler
    return handler


def bind_return(win: tk.Misc, default: Callable[[], None],
                busy: Callable[[], bool] | None = None) -> Callable[[object], str | None]:
    """Return runs ``default`` - only ever pass a command that cannot destroy anything. A
    focused push button is pressed instead; a text box keeps its new line."""
    def handler(event=None):
        widget = getattr(event, "widget", None) if event is not None else None
        if isinstance(widget, (ttk.Button, tk.Button)):
            widget.invoke()
            return "break"
        if isinstance(widget, _OWN_RETURN):
            return None
        if busy is not None and busy():
            return "break"
        default()
        return "break"

    win.bind("<Return>", handler)
    win._return_handler = handler
    return handler


def focus_first(win: tk.Misc, widget: tk.Misc) -> None:
    """The widget the user came for gets the focus once the window is up."""
    def go() -> None:
        try:
            if win.winfo_exists() and widget.winfo_exists():
                widget.focus_set()
        except tk.TclError:
            pass

    win.after_idle(go)


def tab_last(*widgets: tk.Misc) -> None:
    """Tab order is stacking order among siblings: raise a footer that was built first so
    the content comes before it."""
    for widget in widgets:
        try:
            widget.lift()
        except tk.TclError:
            pass


def cap_minsize(win: tk.Misc, width: float, height: float, share: float = SCREEN_SHARE) -> None:
    """``minsize(px(width), px(height))``, but at most ``share`` of the screen each way (Tk
    keeps a minimum larger than the screen, which pushes the footer off it)."""
    w = min(px(width), int(win.winfo_screenwidth() * share))
    h = min(px(height), int(win.winfo_screenheight() * share))
    win.minsize(w, h)


def keep_on_screen(win: tk.Misc, share: float = SCREEN_SHARE) -> None:
    """Shrink a window that was sized larger than ``share`` of the screen."""
    win.update_idletasks()
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    w, h = win.winfo_reqwidth(), win.winfo_reqheight()
    cw, ch = min(w, int(sw * share)), min(h, int(sh * share))
    if (cw, ch) != (w, h):
        win.geometry(f"{cw}x{ch}")
