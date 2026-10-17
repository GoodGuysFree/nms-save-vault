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

import sys
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

from .ui.dpi import px, pxs

SYSTEM = "system"
LIGHT = "light"
DARK = "dark"
CHOICES = (SYSTEM, LIGHT, DARK)

# What the dropdown shows, in order.
CHOICE_LABELS = {SYSTEM: "System", LIGHT: "Light", DARK: "Dark"}

# --- font scaling -------------------------------------------------------------
#
# Ctrl+/Ctrl- zoom works by resizing Tk's *named* fonts. Every ttk widget here draws with
# one of them (no style sets a font of its own), so resizing them zooms the whole UI in
# one go, and the Treeview row height follows because it is derived from the font metrics.

MIN_FONT_SCALE = 0.7
MAX_FONT_SCALE = 2.0
FONT_SCALE_STEP = 0.1

#: The Tk named fonts widgets draw with. Not every Tk build defines all of them.
_NAMED_FONTS = (
    "TkDefaultFont",
    "TkTextFont",
    "TkMenuFont",
    "TkHeadingFont",
    "TkFixedFont",
    "TkTooltipFont",
    "TkIconFont",
    "TkSmallCaptionFont",
    "TkCaptionFont",
)

#: Fonts the app defines for itself, so widgets can name one instead of hardcoding a
#: size that would not zoom. Values are (points relative to TkDefaultFont, weight, slant).
BOLD_FONT = "NmsVaultBold"
HEADING_FONT = "NmsVaultHeading"
TIP_FONT = "NmsVaultTip"
TITLE_FONT = "NmsVaultTitle"
SMALL_FONT = "NmsVaultSmall"
ITALIC_FONT = "NmsVaultItalic"
_APP_FONTS = {
    BOLD_FONT: (0, "bold", "roman"),
    HEADING_FONT: (1, "bold", "roman"),
    TIP_FONT: (0, "normal", "roman"),
    TITLE_FONT: (4, "bold", "roman"),
    SMALL_FONT: (-1, "normal", "roman"),
    ITALIC_FONT: (0, "normal", "italic"),
}

#: Unscaled size of every font above, captured once before anything is resized.
_base_sizes: dict[str, int] = {}


def clamp_font_scale(scale: float) -> float:
    """Snap a zoom factor to a whole step and hold it inside the supported range."""
    try:
        wanted = float(scale)
    except (TypeError, ValueError):
        wanted = 1.0
    stepped = round(round(wanted / FONT_SCALE_STEP) * FONT_SCALE_STEP, 2)
    return min(MAX_FONT_SCALE, max(MIN_FONT_SCALE, stepped))


def _scaled_size(base: int, scale: float) -> int:
    """Tk font sizes are points when positive and pixels when negative; keep the sign."""
    size = max(6, int(round(abs(base) * scale)))
    return -size if base < 0 else size


def init_fonts(root: tk.Misc) -> None:
    """Capture the baseline font sizes and create the app's own named fonts.

    Idempotent, and it must run before the first zoom: every scale is computed from the
    baseline, so the baseline has to be read while the fonts are still at native size.
    """
    if _base_sizes:
        return
    for name in _NAMED_FONTS:
        try:
            _base_sizes[name] = int(tkfont.nametofont(name, root).cget("size"))
        except tk.TclError:
            continue
    try:
        default = tkfont.nametofont("TkDefaultFont", root)
        family = default.cget("family")
    except tk.TclError:
        return
    base = _base_sizes.get("TkDefaultFont", 9)
    for name, (delta, weight, slant) in _APP_FONTS.items():
        size = base + (delta if base > 0 else -delta)
        spec = ("-family", family, "-size", size, "-weight", weight, "-slant", slant)
        # Created through Tcl rather than tkfont.Font: a Font object owns the font it
        # creates and deletes it again when it is garbage-collected, and nothing here
        # would hold a reference to keep it alive.
        try:
            root.tk.call("font", "create", name, *spec)
        except tk.TclError:  # already created earlier in this interpreter
            root.tk.call("font", "configure", name, *spec)
        _base_sizes[name] = size


def apply_font_scale(root: tk.Misc, scale: float) -> float:
    """Resize every named font to ``scale`` x its baseline; returns the clamped factor."""
    scale = clamp_font_scale(scale)
    init_fonts(root)
    for name, base in _base_sizes.items():
        try:
            tkfont.nametofont(name, root).configure(size=_scaled_size(base, scale))
        except tk.TclError:
            continue
    return scale


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
        # The organizer (D6 3.12): platforms, states, version rows, cards and the move
        # dialog's in (green) / out (amber) pair.
        "steam": "#1f5fa8",
        "xbox": "#147a31",
        "warn": "#a35200",
        "warn_bg": "#fff1d6",
        "ok": "#1e7b34",
        "muted": "#6b6b6b",
        "dim": "#858585",            # the other lines while one is expanded
        "danger": "#b00020",
        "pending": "#7a5b00",
        "event": "#5a4a9a",
        "group_bg": "#e8f0fa",       # an expanded line's version rows
        "zebra": "#f0f2f5",
        "hl_bg": "#dcebfb",
        "card": "#ffffff",
        "chip_on": "#cfe1f6",
        "slot_empty": "#f4f4f4",
        "callout_bg": "#fff6d6",
        "diff_add": "#d7f5dd",
        "diff_del": "#fbdcdc",
        "in_bg": "#e3f4e6",
        "in_fg": "#1e7b34",
        "out_bg": "#fff0d9",
        "out_fg": "#a35200",
        "lane0": "#0a64c8",          # history lanes: main line, then branches
        "lane1": "#7b3fb5",
        "lane2": "#c25e00",
        "lane3": "#008b8b",
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
        "steam": "#6aa8f0",
        "xbox": "#5fd58a",
        "warn": "#f0a35a",
        "warn_bg": "#3d2e14",
        "ok": "#5fd58a",
        "muted": "#9a9a9a",
        "dim": "#767676",
        "danger": "#ff7b7b",
        "pending": "#e0b34a",
        "event": "#b8a6f0",
        "group_bg": "#1f2a36",
        "zebra": "#232323",
        "hl_bg": "#24364a",
        "card": "#1b1b1b",
        "chip_on": "#2f4f73",
        "slot_empty": "#222222",
        "callout_bg": "#3a3420",
        "diff_add": "#1e3b26",
        "diff_del": "#45201f",
        "in_bg": "#1e3b26",
        "in_fg": "#5fd58a",
        "out_bg": "#3d2e14",
        "out_fg": "#f0a35a",
        "lane0": "#5a9fd4",
        "lane1": "#c39bf0",
        "lane2": "#f0a35a",
        "lane3": "#4fd1c5",
    },
}

#: The history lanes in drawing order (D6 3.1.3).
LANE_KEYS = ("lane0", "lane1", "lane2", "lane3")


def lanes(p: dict) -> list[str]:
    return [p[key] for key in LANE_KEYS]


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
        return tkfont.nametofont("TkDefaultFont").metrics("linespace") + px(8)
    except tk.TclError:
        return px(24)


def apply(root: tk.Misc, choice: str, native_ttk_theme: str) -> str:
    """Restyle the whole app for ``choice``; returns the resolved palette name."""
    name = resolve(choice)
    p = PALETTES[name]
    init_fonts(root)
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
    style.configure("Sash", sashthickness=px(6), gripcount=12, background=p["bg"])
    style.configure("Status.TLabel", background=p["status_bg"], foreground=p["fg"])
    style.configure("Banner.TLabel", background=p["banner_bg"], foreground=p["banner_fg"])
    style.configure("Banner.TFrame", background=p["banner_bg"])

    _style_button(style, "TButton", p, p["btn_bg"], p["fg"])
    _style_button(style, "Banner.TButton", p, p["banner_bg"], p["banner_fg"])
    _style_containers(style, p)
    _style_organizer(style, p)

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
        padding=pxs(6, 4),
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
        padding=px(3),
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
        padding=px(3),
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


def _style_containers(style: ttk.Style, p: dict) -> None:
    """Notebook tabs, label frames and separators: unmapped, clam drew the tabs light grey
    with light text in dark mode (unreadable) and the frames with white bevels."""
    style.configure("TNotebook", background=p["bg"], bordercolor=p["border"],
                    lightcolor=p["bg"], darkcolor=p["bg"], tabmargins=pxs(2, 4, 2, 0))
    style.configure("TNotebook.Tab", background=p["btn_bg"], foreground=p["fg"],
                    bordercolor=p["border"], lightcolor=p["btn_bg"], darkcolor=p["btn_bg"],
                    padding=pxs(10, 3))
    style.map(
        "TNotebook.Tab",
        background=[("selected", p["field"]), ("active", p["hover_bg"])],
        foreground=[("disabled", p["disabled_fg"]), ("selected", p["fg"]), ("active", p["fg"])],
        lightcolor=[("selected", p["field"])],
        darkcolor=[("selected", p["field"])],
    )
    style.configure("TLabelframe", background=p["bg"], bordercolor=p["border"],
                    lightcolor=p["bg"], darkcolor=p["bg"])
    style.configure("TLabelframe.Label", background=p["bg"], foreground=p["fg"])
    style.configure("TSeparator", background=p["border"])


def _style_organizer(style: ttk.Style, p: dict) -> None:
    """The named styles the organizer's views use (D6 3.12)."""
    style.configure("Card.TFrame", background=p["card"])
    style.configure("Card.TLabel", background=p["card"], foreground=p["fg"])
    style.configure("CardMuted.TLabel", background=p["card"], foreground=p["muted"])
    style.configure("CardTitle.TLabel", background=p["card"], foreground=p["fg"],
                    font=TITLE_FONT)
    style.configure("Muted.TLabel", foreground=p["muted"])
    style.configure("Title.TLabel", font=TITLE_FONT)
    style.configure("Head.TLabel", font=HEADING_FONT)
    style.configure("Warn.TLabel", foreground=p["warn"])
    style.configure("Toast.TFrame", background=p["banner_bg"])
    style.configure("Toast.TLabel", background=p["banner_bg"], foreground=p["banner_fg"])
    style.configure("Info.TFrame", background=p["hl_bg"])
    style.configure("Info.TLabel", background=p["hl_bg"], foreground=p["fg"])
    style.configure("Alert.TFrame", background=p["warn_bg"])
    style.configure("Alert.TLabel", background=p["warn_bg"], foreground=p["fg"])


#: DWMWA_USE_IMMERSIVE_DARK_MODE (Windows 10 20H1 and later).
_DWM_DARK_MODE = 20


def dark_title_bar(window: tk.Misc, dark: bool) -> bool:
    """Ask Windows 11 to draw ``window``'s title bar dark (or light again). Best effort: an
    older Windows, another OS or a window not yet mapped simply keeps its title bar.
    The native menu bar stays light in dark mode; Tk 8.6 cannot theme it (D6 3.12)."""
    if sys.platform != "win32":
        return False
    import ctypes

    try:
        hwnd = int(window.wm_frame(), 16)
        value = ctypes.c_int(1 if dark else 0)
        return ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, _DWM_DARK_MODE, ctypes.byref(value), ctypes.sizeof(value)) == 0
    except (AttributeError, OSError, ValueError, tk.TclError):
        return False


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
        padding=pxs(10, 5),
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
