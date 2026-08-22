"""Light / dark colour themes for the Tkinter UI.

Three choices are offered: ``light``, ``dark``, and ``system`` (follow Windows). The
choice is persisted in ``state.json``; :func:`resolve` turns it into the concrete palette
to use right now.

Why the ttk theme changes with the palette: the native Windows ttk themes (``vista`` /
``winnative``) draw their widgets with the OS's own colours and ignore most ``configure``
calls, so they cannot be made dark. ``clam`` is fully colour-configurable, so dark mode
switches to it. Light mode keeps whatever native theme Tk picked, which is what the app
has always looked like.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

SYSTEM = "system"
LIGHT = "light"
DARK = "dark"
CHOICES = (SYSTEM, LIGHT, DARK)

# What the dropdown shows, in order.
CHOICE_LABELS = {SYSTEM: "System", LIGHT: "Light", DARK: "Dark"}

PALETTES = {
    LIGHT: {
        "bg": "#f0f0f0",
        "fg": "#1a1a1a",
        "field": "#ffffff",          # tree / entry interiors
        "sel_bg": "#0a64c8",
        "sel_fg": "#ffffff",
        "heading_bg": "#e1e1e1",
        "heading_fg": "#1a1a1a",
        "border": "#a0a0a0",
        "status_bg": "#e6e6e6",
        # Row tags
        "live": "#0a6b2f",
        "active": "#0a6b2f",
        "readonly": "#7a5b00",
        "backup": "#333333",
        # Tooltip
        "tip_bg": "#ffffe0",
        "tip_fg": "#1a1a1a",
        "tip_border": "#9a9a7a",
        # Update banner
        "banner_bg": "#fff4c2",
        "banner_fg": "#3d3000",
    },
    DARK: {
        "bg": "#242424",
        "fg": "#e6e6e6",
        "field": "#1b1b1b",
        "sel_bg": "#2f6fb0",
        "sel_fg": "#ffffff",
        "heading_bg": "#333333",
        "heading_fg": "#e6e6e6",
        "border": "#3d3d3d",
        "status_bg": "#1b1b1b",
        # Row tags: the light-mode greens/ambers are unreadable on a dark field, so these
        # are lifted to keep roughly the same meaning at usable contrast.
        "live": "#5fd58a",
        "active": "#5fd58a",
        "readonly": "#e0b34a",
        "backup": "#bdbdbd",
        "tip_bg": "#3a3a2a",
        "tip_fg": "#f0f0e0",
        "tip_border": "#6a6a50",
        "banner_bg": "#40381a",
        "banner_fg": "#ffe9a3",
    },
}


def system_prefers_dark() -> bool | None:
    """True/False from the Windows personalisation setting, or None if unknown.

    ``AppsUseLightTheme`` is 0 for dark, 1 for light. Unknown (non-Windows, key missing,
    registry unreadable) is reported as None so the caller can fall back rather than
    guess wrong.
    """
    try:
        import winreg
    except ImportError:
        return None
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            value, _kind = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return not int(value)
    except (OSError, ValueError):
        return None


def resolve(choice: str) -> str:
    """Turn a stored choice into the palette to use now: ``light`` or ``dark``."""
    if choice == DARK:
        return DARK
    if choice == LIGHT:
        return LIGHT
    dark = system_prefers_dark()
    return DARK if dark else LIGHT  # unknown -> light, the historical look


def palette(choice: str) -> dict:
    return PALETTES[resolve(choice)]


def apply(root: tk.Misc, choice: str, native_ttk_theme: str) -> str:
    """Restyle the whole app for ``choice``; returns the resolved palette name.

    ``native_ttk_theme`` is whatever ttk was using at startup, restored for light mode so
    the app keeps its native Windows appearance.
    """
    name = resolve(choice)
    p = PALETTES[name]
    style = ttk.Style(root)
    try:
        style.theme_use("clam" if name == DARK else native_ttk_theme)
    except tk.TclError:
        style.theme_use("clam")

    style.configure(".", background=p["bg"], foreground=p["fg"])
    style.configure("TFrame", background=p["bg"])
    style.configure("TLabel", background=p["bg"], foreground=p["fg"])
    style.configure("TButton", background=p["bg"], foreground=p["fg"])
    style.configure("TPanedwindow", background=p["bg"])
    style.configure("Status.TLabel", background=p["status_bg"], foreground=p["fg"])
    style.configure("Banner.TLabel", background=p["banner_bg"], foreground=p["banner_fg"])
    style.configure("Banner.TFrame", background=p["banner_bg"])
    style.configure("Banner.TButton", background=p["banner_bg"], foreground=p["banner_fg"])

    style.configure(
        "Treeview",
        background=p["field"],
        fieldbackground=p["field"],
        foreground=p["fg"],
        bordercolor=p["border"],
    )
    style.map(
        "Treeview",
        background=[("selected", p["sel_bg"])],
        foreground=[("selected", p["sel_fg"])],
    )
    style.configure("Treeview.Heading", background=p["heading_bg"], foreground=p["heading_fg"])
    style.map("Treeview.Heading", background=[("active", p["sel_bg"])])

    style.configure(
        "TCombobox",
        fieldbackground=p["field"],
        background=p["bg"],
        foreground=p["fg"],
        arrowcolor=p["fg"],
    )
    # The combobox dropdown is a classic Tk listbox, reached only through the option DB.
    root.option_add("*TCombobox*Listbox.background", p["field"])
    root.option_add("*TCombobox*Listbox.foreground", p["fg"])
    root.option_add("*TCombobox*Listbox.selectBackground", p["sel_bg"])
    root.option_add("*TCombobox*Listbox.selectForeground", p["sel_fg"])
    # Context menus and the Help window's Text are classic widgets too.
    root.option_add("*Menu.background", p["bg"])
    root.option_add("*Menu.foreground", p["fg"])
    root.option_add("*Menu.activeBackground", p["sel_bg"])
    root.option_add("*Menu.activeForeground", p["sel_fg"])

    try:
        root.configure(background=p["bg"])
    except tk.TclError:
        pass
    return name
