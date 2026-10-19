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
        # Tk runs only the most specific binding of a tag, so a widget's own <Button-1>
        # would hide this one: name the buttons.
        for number in (1, 2, 3):
            widget.bind(f"<ButtonPress-{number}>", self.hide, add="+")
        widget.bind("<Destroy>", self._on_destroy, add="+")

    def _on_motion(self, event) -> None:
        row = self.key_at(event)
        if row == self.row:
            return
        self.row = row
        self._close()
        self._cancel()
        if row:
            x, y = event.x_root, event.y_root
            try:
                self._after = self.widget.after(self.DELAY_MS, lambda: self._show(row, x, y))
            except tk.TclError:                     # the widget is going away
                self._after = None

    def _show(self, row: str, x: int, y: int) -> None:
        self._after = None
        try:
            if not self.widget.winfo_exists():
                return
            text = self.text_for(self.widget, row)
            if not text or row != self.row:
                return
            self._build(text, x, y)
        except tk.TclError:                         # destroyed between the timer and now
            self.window = None

    def _build(self, text: str, x: int, y: int) -> None:
        self.window = tk.Toplevel(self.widget)
        self.window.wm_overrideredirect(True)  # no title bar / border
        self.window.configure(background=TOOLTIP_STYLE["border"])
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

    def _on_destroy(self, event=None) -> None:
        if event is not None and event.widget is not self.widget:
            return                                   # a child's Destroy bubbles up the tags
        self.hide()

    def _cancel(self) -> None:
        after, self._after = self._after, None
        if after is not None:
            try:
                self.widget.after_cancel(after)
            except tk.TclError:
                pass

    def _close(self) -> None:
        window, self.window = self.window, None
        if window is not None:
            try:
                window.destroy()
            except tk.TclError:
                pass
