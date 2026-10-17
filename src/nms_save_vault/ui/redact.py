"""Every string a widget shows passes through here (D6 3.13, 3.14).

Account ids appear in folder names, so any text built from a folder, an entry or a source
goes through the account-alias filter (``aliases.redact``) on its way to the screen. The
helpers replace the direct widget calls, which is what makes the guarantee structural: a
row, a label, a canvas text or a window title added later cannot leak a real id once an
alias is set. The core already returns redacted text; filtering twice is harmless.

On the Linux kit's Tk (X11 without Xft) characters outside Latin-1 draw as garbage, so
there every shown data string has them replaced by ``?`` (:func:`shown`).
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from ..core import aliases
from .worker import on_tk_thread


def _latin1(text: str) -> str:
    return "".join(ch if ord(ch) < 256 else "?" for ch in text)


def needs_latin1(widget: tk.Misc) -> bool:
    try:
        return widget.tk.call("tk", "windowingsystem") == "x11"
    except tk.TclError:
        return False


def shown(widget: tk.Misc | None, text):
    """``text`` as it may appear on screen: redacted, and Latin-1 only on X11."""
    if not isinstance(text, str):
        return text
    text = aliases.redact(text)
    if widget is not None and needs_latin1(widget):
        text = _latin1(text)
    return text


class RedactingTreeview(ttk.Treeview):
    """A tree whose row texts, cell values and headings pass through the filter -
    ``insert``, ``item``, ``set`` and ``heading`` alike (R2: only ``insert`` was)."""

    def insert(self, parent, index, iid=None, **kw):
        assert on_tk_thread()
        return super().insert(parent, index, iid, **self._filtered(kw))

    def item(self, item, option=None, **kw):
        return super().item(item, option, **self._filtered(kw))

    def set(self, item, column=None, value=None):
        if value is not None:
            value = shown(self, value)
        return super().set(item, column, value)

    def heading(self, column, option=None, **kw):
        if "text" in kw:
            kw["text"] = shown(self, kw["text"])
        return super().heading(column, option, **kw)

    def _filtered(self, kw: dict) -> dict:
        if "text" in kw:
            kw["text"] = shown(self, kw["text"])
        if "values" in kw and kw["values"] is not None:
            kw["values"] = tuple(shown(self, v) for v in kw["values"])
        return kw


def rtext(widget: tk.Text, index: str, text: str, *tags: str) -> None:
    """Insert ``text`` into a Text widget, filtered."""
    assert on_tk_thread()
    widget.insert(index, shown(widget, text), tags)


def rlabel(parent: tk.Misc, text: str = "", **kw) -> ttk.Label:
    """A ttk Label showing ``text``, filtered."""
    return ttk.Label(parent, text=shown(parent, text), **kw)


def set_label(label: tk.Misc, text: str) -> None:
    """Change a label's (or button's) text, filtered."""
    label.configure(text=shown(label, text))


def ctext(canvas: tk.Canvas, x: float, y: float, text: str = "", **kw) -> int:
    """``canvas.create_text``, filtered."""
    assert on_tk_thread()
    return canvas.create_text(x, y, text=shown(canvas, text), **kw)


def rtitle(window: tk.Wm, text: str) -> None:
    """A window title, filtered."""
    window.title(shown(window, text))
