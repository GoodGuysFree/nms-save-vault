"""Light / dark colour themes for the Tkinter UI.

Three choices are offered: ``light``, ``dark``, and ``system`` (follow the OS). The choice
is persisted in ``state.json``; :func:`resolve` turns it into the concrete palette to use
right now.

Two things about ttk drive the shape of this module.

**Which ttk theme is used.** The native themes (Windows ``vista``, macOS ``aqua``) draw
with the OS's own colours and ignore most ``configure`` calls, so they cannot be made dark
-- dark mode always switches to ``clam``, which is fully colour-configurable. Light mode
keeps the native theme *where one exists*. On X11 there is none: Tk falls back to
``default``, a Motif-era look that ignores half of what is set on it, so Linux uses
``clam`` for both palettes.

**``configure`` is not enough.** A ttk widget picks its colours per *state* -- active,
pressed, disabled, focus, readonly -- and ``configure`` only sets the base. Anything not
explicitly mapped keeps ``clam``'s own defaults, which were chosen for a light grey theme:
that is why buttons used not to highlight in dark mode, why a disabled button's label
vanished into its background, and why the readonly comboboxes showed dark text on a dark
field. Every interactive widget below therefore gets a ``map`` as well as a ``configure``.
``tests/test_theme.py`` checks the resulting pairs for real contrast.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

SYSTEM = "system"
LIGHT = "light"
DARK = "dark"
CHOICES = (SYSTEM, LIGHT, DARK)

# What the dropdown shows, in order.
CHOICE_LABELS = {SYSTEM: "System", LIGHT: "Light", DARK: "Dark"}

#: Windowing systems that have a real native ttk theme worth keeping in light mode.
_NATIVE_LOOK = ("win32", "aqua")

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
        # Interactive surfaces: a button at rest, hovered, and held down.
        "btn_bg": "#fbfbfb",
        "hover_bg": "#e3eefb",
        "pressed_bg": "#cfe1f6",
        # Disabled has to stay readable -- it says "not now", not "nothing here".
        "disabled_bg": "#ececec",
        "disabled_fg": "#757575",
        "accent": "#0a64c8",         # focus ring, progress bar, check marks
        "trough": "#dcdcdc",         # scrollbar / progress bar channel
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
        # Lifted off the window background so a button reads as a button without a bevel.
        "btn_bg": "#333333",
        "hover_bg": "#414141",
        "pressed_bg": "#4d4d4d",
        "disabled_bg": "#2b2b2b",
        "disabled_fg": "#8a8a8a",
        "accent": "#5a9fd4",
        "trough": "#161616",
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
    """True/False from the OS personalisation setting, or None if unknown.

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


def ttk_theme_for(root: tk.Misc, name: str, native_ttk_theme: str) -> str:
    """Which ttk theme can actually draw ``name`` on this machine.

    ``clam`` unless we are drawing the light palette on a platform whose native theme is
    worth keeping. X11's fallback (``default``) is not: it looks like Motif and ignores
    much of what is configured on it.
    """
    if name == DARK:
        return "clam"
    try:
        windowing = root.tk.call("tk", "windowingsystem")
    except tk.TclError:
        windowing = ""
    return native_ttk_theme if windowing in _NATIVE_LOOK else "clam"


def _row_height(root: tk.Misc) -> int:
    """Tree row height with a little air, derived from the font so it survives any DPI."""
    try:
        return tkfont.nametofont("TkDefaultFont").metrics("linespace") + 8
    except tk.TclError:
        return 24


def apply(root: tk.Misc, choice: str, native_ttk_theme: str) -> str:
    """Restyle the whole app for ``choice``; returns the resolved palette name."""
    name = resolve(choice)
    p = PALETTES[name]
    style = ttk.Style(root)
    try:
        style.theme_use(ttk_theme_for(root, name, native_ttk_theme))
    except tk.TclError:
        style.theme_use("clam")

    # clam derives bevels from lightcolor/darkcolor; tying them to the background is what
    # flattens the widgets instead of leaving a 1990s raised edge on everything.
    style.configure(
        ".",
        background=p["bg"],
        foreground=p["fg"],
        bordercolor=p["border"],
        lightcolor=p["bg"],
        darkcolor=p["bg"],
        troughcolor=p["trough"],
        focuscolor=p["accent"],
        selectbackground=p["sel_bg"],
        selectforeground=p["sel_fg"],
    )
    style.map(".", foreground=[("disabled", p["disabled_fg"])])

    style.configure("TFrame", background=p["bg"])
    style.configure("TLabel", background=p["bg"], foreground=p["fg"])
    style.map("TLabel", foreground=[("disabled", p["disabled_fg"])])
    style.configure("TPanedwindow", background=p["bg"])
    style.configure("Sash", sashthickness=6, gripcount=12, background=p["bg"])
    style.configure("Status.TLabel", background=p["status_bg"], foreground=p["fg"])
    style.configure("Banner.TLabel", background=p["banner_bg"], foreground=p["banner_fg"])
    style.configure("Banner.TFrame", background=p["banner_bg"])

    _style_button(style, "TButton", p, p["btn_bg"], p["fg"])
    _style_button(style, "Banner.TButton", p, p["banner_bg"], p["banner_fg"])

    style.configure(
        "Treeview",
        background=p["field"],
        fieldbackground=p["field"],
        foreground=p["fg"],
        bordercolor=p["border"],
        borderwidth=0,
        rowheight=_row_height(root),
    )
    style.map(
        "Treeview",
        background=[("selected", p["sel_bg"])],
        foreground=[("selected", p["sel_fg"])],
    )
    style.configure(
        "Treeview.Heading",
        background=p["heading_bg"],
        foreground=p["heading_fg"],
        bordercolor=p["border"],
        relief="flat",
        padding=(6, 4),
    )
    # Hovering a sortable heading used to turn it selection-blue, which read as "this
    # column is chosen". A slight lift says "clickable" without claiming state.
    style.map(
        "Treeview.Heading",
        background=[("pressed", p["pressed_bg"]), ("active", p["hover_bg"])],
        foreground=[("disabled", p["disabled_fg"]), ("active", p["fg"])],
        relief=[("pressed", "flat"), ("active", "flat")],
    )

    # A readonly combobox spends its whole life in the "readonly" state, so without these
    # maps it kept clam's light-theme colours: dark text on a dark field.
    style.configure(
        "TCombobox",
        fieldbackground=p["field"],
        background=p["btn_bg"],
        foreground=p["fg"],
        arrowcolor=p["fg"],
        bordercolor=p["border"],
        padding=3,
    )
    style.map(
        "TCombobox",
        fieldbackground=[
            ("disabled", p["disabled_bg"]),
            ("readonly", p["btn_bg"]),
            ("!disabled", p["field"]),
        ],
        foreground=[("disabled", p["disabled_fg"]), ("!disabled", p["fg"])],
        background=[("active", p["hover_bg"]), ("!disabled", p["btn_bg"])],
        arrowcolor=[("disabled", p["disabled_fg"]), ("!disabled", p["fg"])],
        bordercolor=[("focus", p["accent"])],
        # Otherwise the whole readonly value sits in a permanent selection highlight.
        selectbackground=[("readonly", p["btn_bg"]), ("!focus", p["field"])],
        selectforeground=[("readonly", p["fg"]), ("!focus", p["fg"])],
    )

    style.configure(
        "TEntry",
        fieldbackground=p["field"],
        foreground=p["fg"],
        insertcolor=p["fg"],
        bordercolor=p["border"],
        padding=3,
    )
    style.map(
        "TEntry",
        fieldbackground=[("disabled", p["disabled_bg"]), ("readonly", p["disabled_bg"])],
        foreground=[("disabled", p["disabled_fg"])],
        bordercolor=[("focus", p["accent"])],
    )

    style.configure(
        "TScrollbar",
        background=p["btn_bg"],
        troughcolor=p["trough"],
        bordercolor=p["border"],
        arrowcolor=p["fg"],
        relief="flat",
    )
    style.map(
        "TScrollbar",
        background=[("pressed", p["pressed_bg"]), ("active", p["hover_bg"])],
        arrowcolor=[("disabled", p["disabled_fg"])],
    )

    for widget in ("TCheckbutton", "TRadiobutton"):
        style.configure(widget, background=p["bg"], foreground=p["fg"], focuscolor=p["accent"])
        style.map(
            widget,
            background=[("active", p["bg"])],
            foreground=[("disabled", p["disabled_fg"])],
            indicatorcolor=[("selected", p["accent"]), ("!selected", p["field"])],
        )

    style.configure(
        "TProgressbar",
        background=p["accent"],
        troughcolor=p["trough"],
        bordercolor=p["border"],
        lightcolor=p["accent"],
        darkcolor=p["accent"],
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
    root.option_add("*Menu.disabledForeground", p["disabled_fg"])

    try:
        root.configure(background=p["bg"])
    except tk.TclError:
        pass
    return name


def _style_button(style: ttk.Style, widget: str, p: dict, base: str, fg: str) -> None:
    """One flat, fully-mapped button style.

    The hover and pressed entries are the whole point: without them a themed button is
    inert under the pointer, which is what "the buttons don't highlight" meant.
    """
    style.configure(
        widget,
        background=base,
        foreground=fg,
        bordercolor=p["border"],
        lightcolor=base,
        darkcolor=base,
        focuscolor=p["accent"],
        relief="flat",
        padding=(10, 5),
    )
    style.map(
        widget,
        background=[
            ("disabled", p["disabled_bg"]),
            ("pressed", p["pressed_bg"]),
            ("active", p["hover_bg"]),
        ],
        foreground=[("disabled", p["disabled_fg"])],
        lightcolor=[("pressed", p["pressed_bg"]), ("active", p["hover_bg"])],
        darkcolor=[("pressed", p["pressed_bg"]), ("active", p["hover_bg"])],
        bordercolor=[("focus", p["accent"]), ("active", p["accent"])],
        relief=[("pressed", "flat"), ("active", "flat")],
    )
