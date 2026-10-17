"""Hover text for tree rows and canvas items.

``text_for(widget, key)`` supplies the text ('' to show nothing) and is called only when
the pointer has rested on a row or item, so the text is built on hover, not per refresh
(D6 3.1.7). ``key_at(event)`` names what is under the pointer: a tree's row by default; the
live lists name their slot. Every text is shown through the redaction filter.
"""
from __future__ import annotations

import tkinter as tk
from collections.abc import Callable

from .. import theme
from .dpi import px
from .redact import shown

# Tooltip colours; the theme swaps these so hover text stays readable in dark mode.
TOOLTIP_STYLE = {"background": "#ffffe0", "foreground": "#1a1a1a", "border": "#9a9a7a"}


class Tooltip:
    DELAY_MS = 450

    def __init__(self, widget, text_for: Callable[[tk.Misc, str], str],
                 key_at: Callable[[tk.Event], str | None] | None = None) -> None:
        self.widget = widget
        self.tree = widget          # the name the classic panes use
        self.text_for = text_for
        self.key_at = key_at or (lambda event: widget.identify_row(event.y))
        self.window: tk.Toplevel | None = None
        self.row: str | None = None
        self._after: str | None = None
        widget.bind("<Motion>", self._on_motion, add="+")
        widget.bind("<Leave>", self.hide, add="+")
        widget.bind("<Button>", self.hide, add="+")

    def _on_motion(self, event) -> None:
        row = self.key_at(event)
        if row == self.row:
            return
        self.row = row
        self._close()
        self._cancel()
        if row:
            x, y = event.x_root, event.y_root
            self._after = self.widget.after(self.DELAY_MS, lambda: self._show(row, x, y))

    def _show(self, row: str, x: int, y: int) -> None:
        self._after = None
        text = self.text_for(self.widget, row)
        if not text or row != self.row:
            return
        self.window = tk.Toplevel(self.widget)
        self.window.wm_overrideredirect(True)  # no title bar / border
        tk.Label(
            self.window,
            text=shown(self.widget, text),
            justify="left",
            relief="solid",
            borderwidth=1,
            background=TOOLTIP_STYLE["background"],
            foreground=TOOLTIP_STYLE["foreground"],
            highlightbackground=TOOLTIP_STYLE["border"],
            padx=px(8),
            pady=px(6),
            font=theme.TIP_FONT,
        ).pack()
        self.window.wm_geometry(f"+{x + px(16)}+{y + px(18)}")

    def hide(self, _event=None) -> None:
        self.row = None
        self._cancel()
        self._close()

    def _cancel(self) -> None:
        if self._after is not None:
            self.widget.after_cancel(self._after)
            self._after = None

    def _close(self) -> None:
        if self.window is not None:
            self.window.destroy()
            self.window = None
