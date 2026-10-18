"""Menu hints: Tk menus have no tooltips, so the explanation of an entry goes to the status
bar while the entry is highlighted (``<<MenuSelect>>``)."""
from __future__ import annotations

import tkinter as tk
from collections.abc import Callable, Mapping


def bind_menu_hints(menu: tk.Menu, hints: Mapping[str, str],
                    show: Callable[[str], None]) -> None:
    """``hints`` maps an entry's label to its text; ``show("")`` clears it when the
    highlight leaves such an entry."""
    state = {"shown": False}

    def on_select(event=None) -> None:
        # The in-window menu bar posts a copy of the menu: the event names the posted one.
        posted = getattr(event, "widget", None)
        target = posted if isinstance(posted, tk.Menu) else menu
        try:
            active = target.index("active")
            label = target.entrycget(active, "label") if active is not None else ""
        except tk.TclError:
            label = ""
        text = hints.get(label, "")
        if text:
            state["shown"] = True
            show(text)
        elif state["shown"]:
            state["shown"] = False
            show("")

    menu.bind("<<MenuSelect>>", on_select, add="+")
    menu.hint_handler = on_select        # Tk delivers the event only to a posted menu
