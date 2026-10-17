"""Tk building blocks shared by the app's views (docs/organizer/designs/D6-ui-tk.md).

``px`` is re-exported here because every widget module needs it: ``from .ui import px``.
"""
from __future__ import annotations

from .dpi import px, pxs

__all__ = ["px", "pxs"]
