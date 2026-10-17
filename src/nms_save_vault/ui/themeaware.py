"""The theme for classic widgets (D6 3.12).

ttk widgets follow the style ``theme.apply`` sets. Classic Tk widgets - a Canvas, a Text,
the chip checkbuttons, tinted card frames - and every image drawn in code take their
colours when they are made, so a theme switch has to reach each one. They register here;
the App calls :meth:`ThemeRegistry.apply` with the new palette and every one of them
re-colours itself (``apply_palette(p)``), regenerating its images or redrawing its canvas.
"""
from __future__ import annotations

import logging
import weakref
from collections.abc import Mapping
from typing import Protocol

log = logging.getLogger(__name__)


class ThemeAware(Protocol):
    def apply_palette(self, p: Mapping[str, str]) -> None: ...


class ThemeRegistry:
    """Weak: a widget that is gone simply drops out."""

    def __init__(self) -> None:
        self._members: weakref.WeakSet = weakref.WeakSet()
        self.palette: Mapping[str, str] | None = None

    def register(self, member: ThemeAware) -> None:
        """Add ``member`` and, once a palette is known, colour it right away."""
        self._members.add(member)
        if self.palette is not None:
            member.apply_palette(self.palette)

    def apply(self, p: Mapping[str, str]) -> None:
        self.palette = p
        for member in list(self._members):
            if not _alive(member):           # a destroyed widget the GC has not taken yet
                self._members.discard(member)
                continue
            try:
                member.apply_palette(p)
            except Exception:  # noqa: BLE001 - one widget must not keep the rest unthemed
                log.exception("a widget could not take the new theme")

    def rezoom(self, ratio: float) -> None:
        """The zoom changed by ``ratio``: members that keep pixel sizes of their own (column
        widths) scale them; the rest redraw with the next palette call anyway."""
        for member in list(self._members):
            if _alive(member) and hasattr(member, "rezoom"):
                member.rezoom(ratio)

    def __len__(self) -> int:
        return len(self._members)


def _alive(member) -> bool:
    exists = getattr(member, "winfo_exists", None)
    if exists is None:
        return True
    try:
        return bool(exists())
    except Exception:  # noqa: BLE001 - the interpreter behind it is gone
        return False
