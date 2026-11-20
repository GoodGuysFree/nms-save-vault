"""Tools > Settings...: the settings that are not a menu choice of their own. One for now: the
size at which the status bar reminds the owner to thin the vault (docs/thinning.md Q4: "B. And
we must show what we're going to delete."). The reminder only says so; nothing is deleted
without the list of Thin the vault... and the owner's click."""
from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from tkinter import ttk

from .dialogs import bind_return, focus_first, place_over, tab_last
from .dpi import px, pxs
from .organizer.ops import _Dialog
from .redact import set_label
from .themeaware import ThemeRegistry

GB = 1 << 30
REMINDER_TEXT = "Remind me to thin the vault when it holds more than"
REMINDER_HINT = ("0 or empty: no reminder. The reminder is a line in the status bar; nothing is "
                 "deleted until you choose Thin the vault..., see the list and press Delete.")
NOT_A_SIZE = "Type a number of GB, like 2 or 0.5 (0 or empty: no reminder)."


def gb_text(nbytes: int) -> str:
    """The size as the field shows it: empty when off, else GB without trailing zeros."""
    if nbytes <= 0:
        return ""
    return f"{nbytes / GB:.2f}".rstrip("0").rstrip(".")


def parse_gb(text: str) -> int | None:
    """Bytes for what the owner typed (``2``, ``0.5``, ``1,5``); 0 for empty or 0; None for
    anything that is not a size."""
    text = text.strip().replace(",", ".")
    if text.lower().endswith("gb"):
        text = text[:-2].strip()
    if not text:
        return 0
    try:
        value = float(text)
    except ValueError:
        return None
    if value != value or value < 0 or value > 1 << 20:      # NaN, negative, absurd
        return None
    return int(round(value * GB))


class SettingsWindow(_Dialog):
    """Modal. ``on_save(reminder_bytes)`` keeps the size and closes the window."""

    def __init__(self, master, *, themed: ThemeRegistry, reminder_bytes: int,
                 on_save: Callable[[int], None]) -> None:
        super().__init__(master, themed, "Settings")
        self.on_save = on_save
        self.resizable(False, False)
        pad = px(12)
        foot = ttk.Frame(self, padding=pxs(12, 0, 12, 12))
        foot.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Button(foot, text="Cancel", command=self.cancel).pack(side=tk.RIGHT)
        ttk.Button(foot, text="Save", command=self.save).pack(side=tk.RIGHT, padx=px(6))
        body = ttk.Frame(self, padding=pad)
        body.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        ttk.Label(body, text="Thinning the vault", style="Head.TLabel").pack(anchor="w")
        row = ttk.Frame(body)
        row.pack(anchor="w", pady=pxs(6, 2))
        ttk.Label(row, text=REMINDER_TEXT).pack(side=tk.LEFT)
        self.size_var = tk.StringVar(value=gb_text(reminder_bytes))
        self.entry = ttk.Entry(row, textvariable=self.size_var, width=8)
        self.entry.pack(side=tk.LEFT, padx=px(6))
        ttk.Label(row, text="GB").pack(side=tk.LEFT)
        ttk.Label(body, text=REMINDER_HINT, style="Muted.TLabel", wraplength=px(460),
                  justify="left").pack(anchor="w", pady=pxs(2, 0))
        self.error = ttk.Label(body, text="", style="Warn.TLabel", wraplength=px(460),
                               justify="left")
        self.error.pack(anchor="w", pady=pxs(4, 0))
        tab_last(foot)
        bind_return(self, self.save)              # saving a size deletes nothing
        place_over(self, master)
        focus_first(self, self.entry)
        self._ready()

    def save(self) -> None:
        nbytes = parse_gb(self.size_var.get())
        if nbytes is None:
            set_label(self.error, NOT_A_SIZE)
            return
        self.destroy()
        self.on_save(nbytes)
